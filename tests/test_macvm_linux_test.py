"""XCRUNNER_MACHINE_LINUX_TEST: the Mac side driving a real Lima QEMU VM from Linux, for CI."""

import io
import os
import sys
from pathlib import Path

import pytest

from xcodon_runtime import cli, forwarded, macvm
from xcodon_runtime.macvm import LINUX_TEST_ENV, MachineSettings, lima_yaml, linux_test_mode, mac_path


def test_the_switch_name_and_platform(monkeypatch):
    assert LINUX_TEST_ENV == "XCRUNNER_MACHINE_LINUX_TEST"
    assert not linux_test_mode({})
    assert linux_test_mode({LINUX_TEST_ENV: "1"}) is sys.platform.startswith("linux")
    assert not linux_test_mode({LINUX_TEST_ENV: "yes"})
    monkeypatch.setattr(macvm.sys, "platform", "darwin")
    assert not linux_test_mode({LINUX_TEST_ENV: "1"})


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_the_yaml_is_a_qemu_vm_with_only_the_home_shared():
    text = lima_yaml(MachineSettings(mounts=("/srv/data",)), Path("/home/runner"), "/opt/src", linux_test=True)
    assert 'vmType: "qemu"' in text and 'mountType: "9p"' in text
    assert "vz" not in text and "rosetta" not in text and "arch:" not in text
    assert "location: \"/private" not in text
    assert '- location: "/home/runner"' in text and '- location: "/srv/data"' in text
    assert '- location: "/opt/src"' in text and "writable: false" in text
    assert '    cache: "mmap"' in text
    assert 'base: "template:ubuntu-24.04"' in text and "apparmor_restrict_unprivileged_userns=0" in text


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_tmp_is_not_rewritten_and_the_temp_folders_are_not_shared(monkeypatch):
    monkeypatch.setenv(LINUX_TEST_ENV, "1")
    assert mac_path("/tmp/x") == "/tmp/x"
    assert macvm.shared_folders(MachineSettings(), Path("/home/runner"), linux_test=True) == ["/home/runner"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_cli_hands_off_and_machine_commands_run(monkeypatch):
    monkeypatch.setenv(LINUX_TEST_ENV, "1")
    seen = []
    monkeypatch.setattr(macvm, "mac_main", lambda argv, local: seen.append(argv) or 5)
    assert cli.main(["ps"]) == 5 and seen == [["ps"]]
    assert macvm.uses_machine()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_machine_from_env_skips_the_mac_host_check(mac, monkeypatch):
    monkeypatch.setattr(macvm, "check_host", lambda: pytest.fail("the Mac host check must not run"))
    m = macvm.Machine.from_env(dict(os.environ, **{LINUX_TEST_ENV: "1"}))
    assert m.linux_test and m.shared() == [str(mac["mac_home"])]
    m.create()
    assert 'vmType: "qemu"' in (mac["lima_home"] / m.name / "lima.yaml").read_text()


def test_the_vm_side_never_forwards_again(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(forwarded, "_run", lambda cmd, env, **kw: seen.append(env) or 0)
    monkeypatch.setenv(LINUX_TEST_ENV, "1")
    err = io.StringIO()
    assert forwarded.main(["--cwd", str(tmp_path), "--", "ps"]) == 0, err.getvalue()
    assert LINUX_TEST_ENV not in seen[0]
