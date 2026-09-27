"""The Mac side's VM management, run on Linux against a fake Lima. See spec section 16.3."""

import io
import json
import platform as std_platform
from pathlib import Path

import pytest

from tests.fake_lima import instances, log
from xcodon_runtime import __version__, macvm
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.macvm import Machine, MachineSettings, lima_yaml, machine_main


def _machine(env=None):
    import os

    return Machine.from_env(dict(os.environ, **(env or {})))


# -- settings and host ---------------------------------------------------------------------


def test_settings_defaults_and_parsing():
    assert MachineSettings.from_env({}) == MachineSettings("xrunner", 4, "4GiB", "100GiB", ())
    s = MachineSettings.from_env({"XRUNNER_MACHINE_NAME": "dev", "XRUNNER_MACHINE_CPUS": "8",
                                  "XRUNNER_MACHINE_MEMORY": "16GiB", "XRUNNER_MACHINE_DISK": "200GiB",
                                  "XRUNNER_MACHINE_MOUNTS": "/Volumes/data:/opt/ref/"})
    assert s == MachineSettings("dev", 8, "16GiB", "200GiB", ("/Volumes/data", "/opt/ref"))


@pytest.mark.parametrize("env,match", [
    ({"XRUNNER_MACHINE_CPUS": "0"}, "XRUNNER_MACHINE_CPUS"),
    ({"XRUNNER_MACHINE_CPUS": "many"}, "XRUNNER_MACHINE_CPUS"),
    ({"XRUNNER_MACHINE_MOUNTS": "relative/dir"}, "absolute"),
    ({"XRUNNER_MACHINE_NAME": "a b"}, "XRUNNER_MACHINE_NAME"),
])
def test_bad_settings_are_refused(env, match):
    with pytest.raises(XcodonError, match=match):
        MachineSettings.from_env(env)


@pytest.mark.parametrize("machine,version", [("x86_64", "26.0"), ("arm64", "15.5"), ("arm64", "")])
def test_old_or_intel_macs_are_refused(monkeypatch, machine, version):
    monkeypatch.setattr(std_platform, "machine", lambda: machine)
    monkeypatch.setattr(std_platform, "mac_ver", lambda: (version, ("", "", ""), ""))
    with pytest.raises(XcodonError, match="Apple silicon with macOS 26 or newer"):
        macvm.check_host()


def test_apple_silicon_on_macos_26_passes(monkeypatch):
    monkeypatch.setattr(std_platform, "machine", lambda: "arm64")
    monkeypatch.setattr(std_platform, "mac_ver", lambda: ("26.1", ("", "", ""), ""))
    macvm.check_host()


def test_missing_lima(mac, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(XcodonError, match="brew install lima"):
        _machine()


def test_old_lima(mac, monkeypatch):
    monkeypatch.setenv("FAKE_LIMA_VERSION", "1.2.1")
    with pytest.raises(XcodonError, match="found Lima 1.2.1"):
        _machine()


# -- the Lima config -----------------------------------------------------------------------


def test_lima_yaml_content():
    text = lima_yaml(MachineSettings(cpus=6, mounts=("/Volumes/data",)), Path("/Users/me"), None)
    for line in ('base: "template:ubuntu-24.04"', 'vmType: "vz"', 'arch: "aarch64"', "cpus: 6",
                 'memory: "4GiB"', 'disk: "100GiB"', 'mountType: "virtiofs"', "      enabled: true",
                 "      binfmt: true", "  system: false", "  user: false",
                 '- location: "/Users/me"', '- location: "/private/var/folders"', '- location: "/private/tmp"',
                 '- location: "/Volumes/data"'):
        assert line in text, line
    assert text.count("writable: true") == 4 and "writable: false" not in text
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in text
    assert "ln -s /private/var/folders /var/folders" in text
    assert "python3 -m venv" in text


def test_a_checkout_outside_the_home_is_mounted_read_only():
    text = lima_yaml(MachineSettings(), Path("/Users/me"), "/opt/src/xcodon-runtime")
    assert '- location: "/opt/src/xcodon-runtime"' in text and "writable: false" in text
    inside = lima_yaml(MachineSettings(), Path("/Users/me"), "/Users/me/src/xcodon-runtime")
    assert "/Users/me/src" not in inside


def test_lima_yaml_parses():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(lima_yaml(MachineSettings(), Path("/Users/me"), "/opt/x"))
    assert doc["vmOpts"]["vz"]["rosetta"] == {"enabled": True, "binfmt": True}
    assert doc["containerd"] == {"system": False, "user": False}
    assert [m["location"] for m in doc["mounts"]] == ["/Users/me", "/private/var/folders", "/private/tmp", "/opt/x"]
    assert [p["mode"] for p in doc["provision"]] == ["system", "user"]


# -- lifecycle -------------------------------------------------------------------------------


def test_first_use_creates_starts_and_installs(mac):
    m = _machine()
    info = m.ensure_ready(io.StringIO())
    assert info["status"] == "Running"
    verbs = [a[0] for a in log(mac["state"], "limactl") if a != ["--version"]]
    assert verbs == ["list", "create", "list", "start", "list"]
    installs = [a for a in log(mac["state"], "ssh") if "pip install" in a[-1]]
    assert len(installs) == 1
    source = macvm.install_source()
    assert (f"{source}[zstd]" if source else f"xc-xrunner[zstd]=={__version__}") in installs[0][-1]
    record = json.loads((mac["lima_home"] / "xrunner" / macvm.INSTALL_RECORD).read_text())
    assert record == {"version": __version__, "source": source or "pypi"}
    yaml_text = (mac["lima_home"] / "xrunner" / "lima.yaml").read_text()
    assert f'- location: "{mac["mac_home"]}"' in yaml_text


def test_a_ready_vm_costs_no_install(mac):
    _machine().ensure_ready(io.StringIO())
    before = len(log(mac["state"], "ssh"))
    _machine().ensure_ready(io.StringIO())
    assert len(log(mac["state"], "ssh")) == before
    assert [a[0] for a in log(mac["state"], "limactl")].count("create") == 1


def test_a_changed_record_reinstalls(mac):
    m = _machine()
    m.ensure_ready(io.StringIO())
    (mac["lima_home"] / "xrunner" / macvm.INSTALL_RECORD).write_text(json.dumps({"version": "0.0.1"}))
    m.ensure_ready(io.StringIO())
    assert len([a for a in log(mac["state"], "ssh") if "pip install" in a[-1]]) == 2


def test_machine_start_checks_the_vm_version(mac, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    assert machine_main(["start"], dict(__import__("os").environ), out, err) == 0
    monkeypatch.setenv("FAKE_VM_VERSION", "0.0.1")
    assert machine_main(["start"], dict(__import__("os").environ), out, err) == 0
    assert len([a for a in log(mac["state"], "ssh") if "pip install" in a[-1]]) == 2
    assert "is running" in err.getvalue()


def test_a_failed_start_shows_limas_log_and_the_rosetta_hint(mac, monkeypatch):
    monkeypatch.setenv("FAKE_LIMA_START_EXIT", "1")
    with pytest.raises(XcodonError) as e:
        _machine().ensure_ready(io.StringIO())
    msg = str(e.value)
    assert "`limactl start xrunner` failed with exit code 1" in msg
    assert "rosetta is not installed" in msg and "softwareupdate --install-rosetta" in msg


def test_a_failed_install_shows_pip_output(mac, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_PIP_EXIT", "1")
    with pytest.raises(XcodonError, match="could not install xrunner in the VM"):
        _machine().ensure_ready(io.StringIO())
    assert not (mac["lima_home"] / "xrunner" / macvm.INSTALL_RECORD).exists()


def test_status_absent_and_running(mac):
    import os

    out = io.StringIO()
    assert machine_main(["status"], dict(os.environ), out, io.StringIO()) == 0
    doc = json.loads(out.getvalue())
    assert doc["status"] == "absent" and doc["vm_xrunner"] is None and doc["mac_xrunner"] == __version__
    machine_main(["start"], dict(os.environ), io.StringIO(), io.StringIO())
    out = io.StringIO()
    machine_main(["status"], dict(os.environ), out, io.StringIO())
    doc = json.loads(out.getvalue())
    assert doc["status"] == "Running" and doc["vm_xrunner"] == __version__ and doc["cpus"] == 4
    assert doc["shared_folders"] == [str(mac["mac_home"]), "/private/var/folders", "/private/tmp"]


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_stop_and_rm(mac):
    import os

    env = dict(os.environ)
    machine_main(["start"], env, io.StringIO(), io.StringIO())
    assert machine_main(["stop"], env, io.StringIO(), io.StringIO()) == 0
    assert instances(mac["state"])["xrunner"]["status"] == "Stopped"
    with pytest.raises(XcodonError, match="pass -f"):
        machine_main(["rm"], env, io.StringIO(), io.StringIO(), stdin=io.StringIO(""))
    assert machine_main(["rm"], env, io.StringIO(), io.StringIO(), stdin=_Tty("n\n")) == 1
    assert "xrunner" in instances(mac["state"])
    assert machine_main(["rm"], env, io.StringIO(), io.StringIO(), stdin=_Tty("y\n")) == 0
    assert "xrunner" not in instances(mac["state"])
    machine_main(["start"], env, io.StringIO(), io.StringIO())
    assert machine_main(["rm", "-f"], env, io.StringIO(), io.StringIO()) == 0
    assert instances(mac["state"]) == {}


def test_usage_and_unknown_verbs(mac):
    import os

    err = io.StringIO()
    assert machine_main([], dict(os.environ), io.StringIO(), err) == 2 and "usage" in err.getvalue()
    assert machine_main(["reboot"], dict(os.environ), io.StringIO(), io.StringIO()) == 2


def test_machine_commands_are_for_macos(monkeypatch):
    monkeypatch.setattr(macvm, "is_macos", lambda: False)
    with pytest.raises(XcodonError, match="machine commands are for macOS"):
        machine_main(["status"], {}, io.StringIO(), io.StringIO())
