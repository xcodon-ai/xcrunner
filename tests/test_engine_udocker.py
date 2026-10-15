"""The udocker engine (spec section 17), against a fake udocker; one network test uses the real one."""

import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from tests.fake_udocker import make_fake_udocker, read_log
from xcodon_runtime import engine as eng
from xcodon_runtime import envrecord
from xcodon_runtime.api import UDOCKER_COMMIT_REFUSED, Runtime
from xcodon_runtime.engine import Bind
from xcodon_runtime.engine_udocker import exec_mode, find_udocker
from xcodon_runtime.errors import EngineUnavailable, XcodonError
from xcodon_runtime.probe import PTRACE_PROBE

REF = "xcodon-test/busybox"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    udocker = make_fake_udocker(tmp_path / "fakebin")
    log = tmp_path / "udocker.log"
    monkeypatch.setenv("XCRUNNER_UDOCKER", str(udocker))
    monkeypatch.setenv("FAKE_UDOCKER_LOG", str(log))
    monkeypatch.setenv("UDOCKER_DIR", str(tmp_path / "udocker-home"))
    return udocker, log


def _calls(log, verb):
    return [c for c in read_log(log) if verb in c["argv"][:2]]


@pytest.fixture
def rt(home, busybox_image, fake):
    return Runtime(home.path, engine="udocker")


# -- finding udocker and its mode ---------------------------------------------------------


def test_find_udocker_order(tmp_path, monkeypatch):
    monkeypatch.delenv("XCRUNNER_UDOCKER", raising=False)
    on_path = make_fake_udocker(tmp_path / "pathbin")
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nopython" / "python"))
    assert find_udocker() == str(on_path)
    beside = make_fake_udocker(tmp_path / "venvbin")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venvbin" / "python"))
    assert find_udocker() == str(beside)
    monkeypatch.setenv("XCRUNNER_UDOCKER", str(on_path))
    assert find_udocker() == str(on_path)
    monkeypatch.setenv("XCRUNNER_UDOCKER", str(tmp_path / "missing"))
    assert find_udocker() is None


def test_exec_mode(monkeypatch):
    assert exec_mode() == "F3"
    monkeypatch.setenv("XCRUNNER_UDOCKER_MODE", "F4")
    assert exec_mode() == "F4"
    monkeypatch.setenv("XCRUNNER_UDOCKER_MODE", "P1")
    with pytest.raises(EngineUnavailable, match="F1, F2, F3, F4"):
        exec_mode()


# -- lifecycle --------------------------------------------------------------------------------


def test_first_start_installs_imports_and_patches_once(rt, fake, tmp_path):
    _, log = fake
    c = rt.create(REF, command=["/bin/sh"])
    rt.start(c)
    cdir = c.dir / "udocker"
    assert [x["argv"][1] for x in read_log(log)] == ["install", "import", "setup"]
    assert all(x["containers"] == str(cdir) for x in read_log(log) if x["argv"][1] != "install")
    (setup,) = _calls(log, "setup")
    assert "--execmode=F3" in setup["argv"] and setup["argv"][-1] == "c"
    assert (cdir / "c" / "ROOT" / "bin" / "sh").exists()
    assert (cdir / "c" / "execmode").read_text() == "F3"
    rt.stop(c)
    rt.start(c)
    assert len(_calls(log, "import")) == 1 and len(_calls(log, "install")) == 1, "a restart reuses the copy"
    rt.stop(c)
    rt.remove(c)
    assert not cdir.exists()


def test_installed_tools_are_not_installed_again(rt, fake, tmp_path):
    _, log = fake
    lib = tmp_path / "udocker-home" / "lib"
    lib.mkdir(parents=True)
    (lib / "libfakechroot-Debian-12-x86_64.so").write_text("")
    rt.start(rt.create(REF, command=["/bin/sh"]))
    assert _calls(log, "install") == []


def test_exec_passes_workdir_env_binds_and_exit_code(rt, fake, tmp_path, monkeypatch):
    _, log = fake
    data = tmp_path / "data"
    data.mkdir()
    c = rt.create(REF, command=["/bin/sh"], binds=[Bind(str(data), "/data")], env={"FOO": "bar"},
                  workdir="/data")
    rt.start(c)
    monkeypatch.setenv("FAKE_UDOCKER_EXIT", "3")
    r = rt.exec(c, ["/bin/sh", "-c", "true"])
    assert r.code == 3 and b"fake udocker ran" in r.stdout
    (run,) = _calls(log, "run")
    argv = run["argv"]
    assert argv[:4] == ["--quiet", "run", "--nobanner", "--nometa"]
    assert "--workdir=/data" in argv and "--env=FOO=bar" in argv and f"--volume={data}:/data" in argv
    assert argv[-4:] == ["c", "/bin/sh", "-c", "true"]
    assert run["containers"] == str(c.dir / "udocker")


def test_a_missing_command_is_127_without_running_udocker(rt, fake):
    _, log = fake
    c = rt.create(REF, command=["/bin/sh"])
    rt.start(c)
    assert rt.exec(c, ["no-such-tool"]).code == 127
    assert _calls(log, "run") == []


def test_read_only_binds_warn(rt, fake, tmp_path, caplog):
    c = rt.create(REF, command=["/bin/sh"], binds=[Bind(str(tmp_path), "/ro", readonly=True)])
    rt.start(c)
    rt.exec(c, ["/bin/sh", "-c", "true"])
    assert "cannot enforce read-only binds" in caplog.text


def test_env_folder_containers_share_one_copy(rt, fake, tmp_path):
    _, log = fake
    env = tmp_path / "proj" / ".xcrunner-env"
    for _ in range(2):
        assert rt.run(REF, ["/bin/sh", "-c", "true"], rm=True, env_dir=env, stdout=open(os.devnull, "w")) == 0
    image = rt.images.get(REF)
    assert (env / image.id / "udocker" / "c" / "ROOT" / "bin" / "sh").exists()
    assert len(_calls(log, "import")) == 1
    assert image.id in envrecord.load(env)["layers"]
    assert envrecord.load(env)["layers"][image.id]["engine"] == "udocker"


def test_a_failed_setup_is_reported(rt, fake, monkeypatch):
    monkeypatch.setenv("FAKE_UDOCKER_SETUP_EXIT", "1")
    with pytest.raises(EngineUnavailable, match="setup --execmode=F3 failed"):
        rt.start(rt.create(REF, command=["/bin/sh"]))


def test_commit_is_refused(rt, fake, tmp_path):
    c = rt.create(REF, command=["/bin/sh"])
    rt.start(c)
    rt.stop(c)
    with pytest.raises(XcodonError, match="not supported on the udocker engine"):
        rt.commit(c, "xcodon-test/x")
    env = tmp_path / "e"
    rt.run(REF, ["/bin/sh", "-c", "true"], rm=True, env_dir=env, stdout=open(os.devnull, "w"))
    with pytest.raises(XcodonError, match="not supported on the udocker engine"):
        rt.commit(None, "xcodon-test/y", env_dir=env, image=REF)
    assert UDOCKER_COMMIT_REFUSED.startswith("commit is not supported")


# -- selection --------------------------------------------------------------------------------


def _blocked():
    p = {n: {"ok": False, "error": f"{n}: denied"} for n in eng.PROBE_NAMES}
    p[PTRACE_PROBE] = {"ok": False, "error": "ptrace: blocked"}
    return p


def test_udocker_is_picked_when_ns_and_ptrace_are_blocked(home, fake, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: _blocked())
    choice = eng.select_engine(home)
    assert choice.name == "udocker" and "Fakechroot" in choice.reason


def test_without_udocker_the_message_says_how_to_get_it(home, monkeypatch, tmp_path):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setenv("XCRUNNER_UDOCKER", str(tmp_path / "missing"))
    monkeypatch.setattr(eng, "run_probes", lambda *a: _blocked())
    choice = eng.select_engine(home)
    assert choice.name == eng.NO_ENGINE and "pip install 'xcrunner[udocker]'" in choice.reason


def test_xcodon_engine_can_force_udocker(home, monkeypatch):
    monkeypatch.setenv("XCODON_ENGINE", "udocker")
    assert eng.select_engine(home).name == "udocker"
    assert eng.get_engine("udocker").name == "udocker"


# -- groupexec ------------------------------------------------------------------------------


def test_groupexec_signals_the_whole_group(tmp_path):
    marker = tmp_path / "grandchild"
    grandchild = textwrap.dedent(f"""
        import signal, sys, time
        signal.signal(signal.SIGTERM, lambda *a: (open({str(marker)!r}, "w").write("term"), sys.exit(0)))
        open({str(marker) + ".ready"!r}, "w").write("1")
        time.sleep(60)
    """)
    middle = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {grandchild!r}])"
    p = subprocess.Popen([sys.executable, "-m", "xcodon_runtime.groupexec", "--", sys.executable, "-c", middle])
    deadline = time.time() + 15
    while not (tmp_path / "grandchild.ready").exists() and time.time() < deadline:
        time.sleep(0.05)
    p.terminate()
    p.wait(timeout=15)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "term"


def test_groupexec_passes_exit_codes():
    assert subprocess.run([sys.executable, "-m", "xcodon_runtime.groupexec", "--", sys.executable, "-c",
                           "raise SystemExit(5)"]).returncode == 5
    assert subprocess.run([sys.executable, "-m", "xcodon_runtime.groupexec"]).returncode == 2


# -- the real udocker -------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(shutil.which("udocker") is None and not os.access(
    os.path.join(os.path.dirname(sys.executable), "udocker"), os.X_OK), reason="udocker is not installed")
def test_real_udocker_runs_a_debian_image(home, tmp_path, monkeypatch):
    monkeypatch.delenv("XCRUNNER_UDOCKER", raising=False)
    monkeypatch.setenv("UDOCKER_DIR", str(tmp_path / "udocker-home"))
    rt = Runtime(home.path, engine="udocker")
    rt.pull("debian:bookworm-slim")
    data = tmp_path / "data"
    data.mkdir()
    out = tmp_path / "out"
    with open(out, "wb") as f:
        code = rt.run("debian:bookworm-slim", ["/bin/sh", "-c", "cat /etc/debian_version; echo $FOO > /data/x; exit 3"],
                      env={"FOO": "bar"}, binds=[Bind(str(data), "/data")], rm=True, stdout=f)
    assert code == 3
    assert out.read_text().strip().split(".")[0] == "12"
    assert (data / "x").read_text() == "bar\n"


def test_hdf5_uses_the_stdio_driver_unless_the_caller_chooses(rt, fake, tmp_path):
    """Fakechroot misses the lstat HDF5's default driver makes after opening a file: every
    h5py.File on a bind-mounted path failed with "can't retrieve stat info for file"."""
    _, log = fake
    c = rt.create(REF, command=["/bin/sh"])
    rt.start(c)
    rt.exec(c, ["/bin/sh", "-c", "true"])
    assert "--env=HDF5_DRIVER=stdio" in _calls(log, "run")[-1]["argv"]
    mine = rt.create(REF, command=["/bin/sh"], env={"HDF5_DRIVER": "sec2"})
    rt.start(mine)
    rt.exec(mine, ["/bin/sh", "-c", "true"])
    argv = _calls(log, "run")[-1]["argv"]
    assert "--env=HDF5_DRIVER=sec2" in argv and "--env=HDF5_DRIVER=stdio" not in argv
