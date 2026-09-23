"""Integration tests for the ns engine. All marked `ns`; skipped where namespaces are off."""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind
from xcodon_runtime.engine_ns import KEEPER_PID, NsEngine
from xcodon_runtime.errors import ContainerNotRunning

pytestmark = pytest.mark.ns


def make_container(home, rootfs: Path, uid=0, gid=0, binds=(), workdir="/") -> Container:
    cid = os.urandom(32).hex()
    cdir = home.containers / cid
    cdir.mkdir()
    c = Container(
        id=cid, image_id="img", image_ref="test/bb:latest", image_rootfs=str(rootfs), engine="ns",
        argv=["/bin/sh"], env={"PATH": "/bin", "HOME": "/root"}, workdir=workdir, uid=uid, gid=gid,
        binds=list(binds), created=datetime.now(timezone.utc).isoformat(), dir=cdir,
    )
    c.save()
    return c


def sh(engine, c, script, **kw) -> subprocess.CompletedProcess:
    p = engine.popen(c, ["/bin/sh", "-c", script], c.env, c.workdir,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    out, err = p.communicate(timeout=30)
    return subprocess.CompletedProcess(p.args, p.returncode, out.decode(), err.decode())


@pytest.fixture
def engine():
    return NsEngine()


@pytest.fixture
def running(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    engine.start(c)
    yield engine, c
    engine.stop(c)


def test_start_records_live_keeper(running):
    engine, c = running
    assert engine.is_running(c)
    pidinfo = json.loads((c.dir / KEEPER_PID).read_text())
    assert Path(f"/proc/{pidinfo['pid']}").exists()


def test_sandbox_identity(running):
    engine, c = running
    r = sh(engine, c, "echo host=$(hostname) uid=$(id -u) pid=$$ home=$HOME")
    assert r.returncode == 0, r.stderr
    assert f"host={c.short_id}" in r.stdout
    assert "uid=0" in r.stdout
    assert "home=/root" in r.stdout
    pid = int(r.stdout.split("pid=")[1].split()[0])
    assert pid < 100, "must be inside the new pid namespace"


def test_dev_is_minimal_and_proc_is_ours(running):
    engine, c = running
    r = sh(engine, c, "ls /dev | tr '\\n' ' '; echo; ls /proc | head -3 | tr '\\n' ' '")
    devs = set(r.stdout.splitlines()[0].split())
    assert {"null", "zero", "urandom", "pts", "ptmx", "shm", "stdin", "stdout", "stderr"} <= devs
    assert "sda" not in devs and "nvme0" not in devs
    assert "1" in r.stdout.splitlines()[1].split()


def test_writes_persist_in_upper_and_across_restart(running, engine):
    engine, c = running
    assert sh(engine, c, "echo hello > /persist.txt").returncode == 0
    assert (c.dir / "upper" / "persist.txt").read_text() == "hello\n"
    engine.stop(c)
    assert not engine.is_running(c)
    engine.start(c)
    assert sh(engine, c, "cat /persist.txt").stdout == "hello\n"


def test_exit_code_and_stdin_passthrough(running):
    engine, c = running
    assert sh(engine, c, "exit 7").returncode == 7
    p = engine.popen(c, ["/bin/cat"], c.env, "/", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(b"from stdin", timeout=30)
    assert out == b"from stdin"


def test_command_not_found_is_127(running):
    engine, c = running
    p = engine.popen(c, ["/no/such/binary"], c.env, "/", stderr=subprocess.PIPE)
    _, err = p.communicate(timeout=30)
    assert p.returncode == 127
    assert b"not found" in err


def test_sys_is_read_only_and_hosts_visible(running):
    engine, c = running
    r = sh(engine, c, "touch /sys/x 2>&1; head -c 9 /etc/hosts")
    assert "Read-only" in r.stdout
    assert "127.0.0.1" in r.stdout


def test_binds_rw_and_ro(home, busybox_rootfs, engine, tmp_path):
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    (ro / "f").write_text("ro-content")
    c = make_container(home, busybox_rootfs, binds=[Bind(str(rw), "/data"), Bind(str(ro), "/rodata", readonly=True)])
    engine.start(c)
    try:
        r = sh(engine, c, "echo w > /data/out; cat /rodata/f; touch /rodata/x 2>&1")
        assert (rw / "out").read_text() == "w\n"
        assert "ro-content" in r.stdout
        assert "Read-only" in r.stdout
        assert os.stat(rw / "out").st_uid == os.getuid()
    finally:
        engine.stop(c)


def test_non_root_uid_mapping(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs, uid=1000, gid=1000)
    engine.start(c)
    try:
        r = sh(engine, c, "id -u; id -g; echo ok > /home/user/f && echo wrote")
        assert r.stdout.splitlines() == ["1000", "1000", "wrote"]
    finally:
        engine.stop(c)


def test_workdir_created_and_used(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs, workdir="/made/up/dir")
    engine.start(c)
    try:
        assert sh(engine, c, "pwd").stdout.strip() == "/made/up/dir"
        assert (c.dir / "upper" / "made/up/dir").is_dir()
    finally:
        engine.stop(c)


def test_zombies_are_reaped_and_host_mounts_untouched(running):
    engine, c = running
    before = Path("/proc/self/mountinfo").read_text().count(str(c.dir))
    assert before == 0
    sh(engine, c, "(sleep 0.2 &) ; true")
    time.sleep(0.6)
    pid = json.loads((c.dir / KEEPER_PID).read_text())["pid"]
    ps = subprocess.run(["ps", "-o", "stat=", "--ppid", str(pid)], capture_output=True, text=True).stdout
    assert "Z" not in ps
    assert Path("/proc/self/mountinfo").read_text().count(str(c.dir)) == 0


def test_stop_kills_everything_and_exec_after_stop_fails(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    engine.start(c)
    p = engine.popen(c, ["/bin/sleep", "30"], c.env, "/")
    time.sleep(0.3)
    pid = json.loads((c.dir / KEEPER_PID).read_text())["pid"]
    engine.stop(c)
    assert p.wait(timeout=5) != 0
    assert not Path(f"/proc/{pid}").exists()
    with pytest.raises(ContainerNotRunning):
        engine.popen(c, ["/bin/true"], c.env, "/")


def test_stale_pid_file_is_not_running(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    (c.dir / KEEPER_PID).write_text(json.dumps({"pid": 1, "starttime": "0"}))
    assert not engine.is_running(c)


def test_stop_leaves_a_removable_container_directory(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    engine.start(c)
    assert sh(engine, c, "echo hi > /f").returncode == 0
    engine.stop(c)
    shutil.rmtree(c.dir)
    assert not c.dir.exists()
