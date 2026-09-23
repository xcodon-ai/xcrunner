# tests/test_containers_api.py
import os
import subprocess
import threading
import time

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import (
    ContainerNotFound,
    ContainerNotRunning,
    EngineUnavailable,
    ImageNotFound,
    XcodonError,
)


@pytest.fixture
def rt(home, busybox_image, engine_name) -> Runtime:
    return Runtime(home.path, engine=engine_name)


def test_create_start_exec_stop_remove(rt):
    c = rt.create("xcodon-test/busybox", command=["/bin/sh"], name="one")
    assert c.state == "created"
    assert c.workdir == "/workspace"
    assert c.env["HOSTNAME"] == c.short_id
    rt.start(c)
    assert rt.get_container("one").state == "running"
    r = rt.exec(c, "echo hi; echo err >&2; exit 4")
    assert (r.code, r.stdout, r.stderr) == (4, b"hi\n", b"err\n")
    assert rt.exec(c, ["/bin/pwd"]).stdout == b"/workspace\n"
    assert rt.exec(c, "echo x > /f && cat /f").stdout == b"x\n"
    rt.stop(c)
    assert rt.get_container(c.id[:8]).state == "exited"
    rt.start(c)
    assert rt.exec(c, ["/bin/cat", "/f"]).stdout == b"x\n"
    rt.stop(c)
    rt.remove(c)
    with pytest.raises(ContainerNotFound):
        rt.get_container("one")


def test_exec_env_and_workdir_overrides(rt):
    c = rt.create("xcodon-test/busybox", env={"A": "1"})
    rt.start(c)
    try:
        r = rt.exec(c, "echo $A $B; pwd", workdir="/tmp", env={"B": "2"})
        assert r.stdout == b"1 2\n/tmp\n"
    finally:
        rt.stop(c)


def test_exec_before_start_raises(rt):
    c = rt.create("xcodon-test/busybox")
    with pytest.raises(ContainerNotRunning):
        rt.exec(c, "true")


def test_remove_running_requires_force(rt):
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    with pytest.raises(XcodonError, match="running"):
        rt.remove(c)
    rt.remove(c, force=True)
    assert rt.containers(all=True) == []


def test_run_returns_exit_code_and_rm(rt, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    code = rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "echo ran > /o/f; exit 5"],
                  binds=[Bind(str(out), "/o")], rm=True)
    assert code == 5
    assert (out / "f").read_text() == "ran\n"
    assert rt.containers(all=True) == []


def test_run_cidfile_error_cleans_up_when_rm(rt):
    with pytest.raises(XcodonError, match="cidfile"):
        rt.run("xcodon-test/busybox", command=["/bin/true"], rm=True, cidfile="/nonexistent/dir/cid")
    assert rt.containers(all=True) == []


def test_run_without_rm_leaves_exited_container(rt):
    code = rt.run("xcodon-test/busybox", command=["/bin/true"], name="kept")
    assert code == 0
    c = rt.get_container("kept")
    assert c.state == "exited"
    rt.remove(c)


def test_run_streams_stdout(rt, tmp_path):
    log = tmp_path / "log"
    with open(log, "wb") as f:
        rt.run("xcodon-test/busybox", command=["/bin/echo", "streamed"], rm=True, stdout=f)
    assert log.read_bytes() == b"streamed\n"


def test_unknown_image_without_pull_source(rt):
    with pytest.raises((ImageNotFound, XcodonError)):
        rt.create("nonexistent/never:latest")


def test_duplicate_name_rejected(rt):
    rt.create("xcodon-test/busybox", name="dup")
    with pytest.raises(XcodonError, match="dup"):
        rt.create("xcodon-test/busybox", name="dup")


def test_info_reports_engine(rt, engine_name):
    info = rt.info()
    assert info["engine"] == engine_name
    assert "home" in info and "python" in info


def test_remove_tolerates_mode_000_overlay_work_dir(home):
    """A keeper that died without ``stop`` leaves ``work/work`` at mode 000.

    ``ContainerStore.remove`` must delete the container directory anyway.
    """
    from xcodon_runtime.containers import ContainerStore

    cdir = home.containers / "deadkeeper"
    work_work = cdir / "work" / "work"
    work_work.mkdir(parents=True)
    (work_work / "leftover").write_text("x")
    os.chmod(work_work, 0o000)

    class FakeContainer:
        dir = cdir

    try:
        ContainerStore(home).remove(FakeContainer())
    finally:
        # In case removal failed, don't leave an unreadable directory behind.
        if work_work.exists():
            os.chmod(work_work, 0o700)
    assert not cdir.exists()


def test_exec_timeout_kills_the_guest_process(rt):
    """A timed-out exec must not leave the guest process running.

    nsexec can only forward catchable signals (not SIGKILL) to the guest, and
    killing PRoot merely detaches its tracee, so exec's timeout handling must
    escalate from terminate to a real kill and rely on PDEATHSIG / PRoot's
    --kill-on-exit to actually take the guest down.
    """
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            rt.exec(c, ["/bin/sleep", "31.7"], timeout=0.5)
        deadline = time.monotonic() + 3
        gone = False
        while time.monotonic() < deadline:
            if subprocess.run(["pgrep", "-f", "sleep 31.7"], capture_output=True).returncode != 0:
                gone = True
                break
            time.sleep(0.1)
        assert gone, "sleep 31.7 is still running after the exec timeout"
    finally:
        rt.stop(c)


def test_concurrent_create_same_name_only_one_succeeds(rt):
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            rt.create("xcodon-test/busybox", name="same")
            outcome = "ok"
        except XcodonError:
            outcome = "rejected"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["ok", "rejected"]


def test_run_from_non_main_thread_returns_exit_code(rt):
    """Signal forwarding needs the main thread; run() must still work without it."""
    result = {}

    def worker():
        result["code"] = rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "exit 7"], rm=True)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["code"] == 7


def test_run_start_failure_does_not_stop_a_never_started_container(rt, monkeypatch):
    def fail_start(self, c):
        raise EngineUnavailable("boom")

    monkeypatch.setattr(Runtime, "start", fail_start)

    with pytest.raises(EngineUnavailable):
        rt.run("xcodon-test/busybox", name="never-started")
    c = rt.get_container("never-started")
    assert c.state == "created"
    rt.remove(c)

    with pytest.raises(EngineUnavailable):
        rt.run("xcodon-test/busybox", rm=True)
    assert rt.containers(all=True) == []


def test_stopped_container_is_not_running_and_restarts(rt):
    """A stopped container must not be listed, must refuse exec, and must restart.

    The proot engine has no keeper process, so "running" is the started
    marker; before, stop() left it in place and the container stayed
    "running" forever.
    """
    c = rt.create("xcodon-test/busybox", name="cycle")
    rt.start(c)
    assert rt.exec(c, "echo kept > /state").code == 0
    rt.stop(c)
    assert rt.containers() == []
    assert [x.id for x in rt.containers(all=True)] == [c.id]
    with pytest.raises(ContainerNotRunning):
        rt.exec(c, "true")
    rt.start(c)
    assert rt.exec(c, ["/bin/cat", "/state"]).stdout == b"kept\n"
    rt.stop(c)
    rt.remove(c)
