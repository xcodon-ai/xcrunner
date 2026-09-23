# tests/test_engine_proot.py
"""Integration tests for the proot engine. Marked `proot`; skipped without a binary."""

import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind
from xcodon_runtime.engine_proot import STARTED_MARKER, ProotEngine, find_proot
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable

pytestmark = pytest.mark.proot


def make_container(home, rootfs, uid=0, gid=0, binds=(), workdir="/"):
    cid = os.urandom(32).hex()
    cdir = home.containers / cid
    cdir.mkdir()
    c = Container(
        id=cid, image_id="img", image_ref="test/bb:latest", image_rootfs=str(rootfs), engine="proot",
        argv=["/bin/sh"], env={"PATH": "/bin", "HOME": "/root"}, workdir=workdir, uid=uid, gid=gid,
        binds=list(binds), created=datetime.now(timezone.utc).isoformat(), dir=cdir,
    )
    c.save()
    return c


def run(engine, c, argv):
    p = engine.popen(c, argv, c.env, c.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(timeout=60)
    return subprocess.CompletedProcess(p.args, p.returncode, out.decode(), err.decode())


def sh(engine, c, script):
    return run(engine, c, ["/bin/sh", "-c", script])


def test_find_proot_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "proot"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("XCODON_PROOT", str(fake))
    assert find_proot() == str(fake)
    monkeypatch.delenv("XCODON_PROOT")
    assert find_proot() is not None


def test_start_copies_rootfs_and_marks(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    assert not e.is_running(c)
    e.start(c)
    assert (c.dir / "rootfs" / "bin" / "busybox").exists()
    assert (c.dir / STARTED_MARKER).exists()
    assert e.is_running(c)
    assert os.stat(c.dir / "rootfs/bin/busybox").st_ino != os.stat(busybox_rootfs / "bin/busybox").st_ino


def test_exec_identity_exit_code_and_persistence(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, workdir="/work/dir")
    e.start(c)
    r = sh(e, c, "id -u; pwd; echo hi > /persist; exit 3")
    assert r.returncode == 3, r.stderr
    assert r.stdout.splitlines()[:2] == ["0", "/work/dir"]
    assert (c.dir / "rootfs" / "persist").read_text() == "hi\n"
    e.stop(c)
    e.start(c)
    assert sh(e, c, "cat /persist").stdout == "hi\n"


def test_non_root_uid(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, uid=1000, gid=1000)
    e.start(c)
    assert sh(e, c, "id -u; id -g").stdout.splitlines() == ["1000", "1000"]


def test_binds_and_command_not_found(home, busybox_rootfs, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, binds=[Bind(str(data), "/data")])
    e.start(c)
    assert sh(e, c, "echo w > /data/out").returncode == 0
    assert (data / "out").read_text() == "w\n"
    p = e.popen(c, ["/no/such"], c.env, "/", stderr=subprocess.PIPE)
    p.communicate(timeout=60)
    assert p.returncode == 127


def test_popen_before_start_fails(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    with pytest.raises(ContainerNotRunning):
        e.popen(c, ["/bin/true"], c.env, "/")


def test_start_recovers_deleted_rootfs(home, busybox_rootfs):
    """A stale marker with a missing rootfs must not make is_running lie."""
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    e.start(c)
    assert (c.dir / STARTED_MARKER).exists()
    shutil.rmtree(c.dir / "rootfs")
    assert not e.is_running(c)
    e.start(c)
    assert (c.dir / "rootfs" / "bin" / "busybox").exists()
    assert e.is_running(c)
    assert sh(e, c, "echo ok").stdout == "ok\n"


def test_non_executable_command_gives_126(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    e.start(c)
    target = c.dir / "rootfs" / "not-exec"
    target.write_text("nope\n")
    target.chmod(0o644)
    r = run(e, c, ["/not-exec"])
    assert r.returncode == 126


def test_command_found_only_in_bind_target_runs(home, busybox_rootfs, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    tool = data / "tool.sh"
    tool.write_text("#!/bin/sh\necho from-bind\n")
    tool.chmod(0o755)
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, binds=[Bind(str(data), "/data")])
    e.start(c)
    r = run(e, c, ["/data/tool.sh"])
    assert r.returncode == 0
    assert r.stdout == "from-bind\n"


def test_readonly_bind_warns_but_still_mounts(home, busybox_rootfs, tmp_path, caplog):
    data = tmp_path / "d"
    data.mkdir()
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, binds=[Bind(str(data), "/data", readonly=True)])
    e.start(c)
    with caplog.at_level(logging.WARNING, logger="xcodon_runtime.engine_proot"):
        r = sh(e, c, "echo w > /data/out")
    assert r.returncode == 0
    assert (data / "out").read_text() == "w\n"
    assert any("read-only" in rec.message for rec in caplog.records)


def test_copy_rootfs_failure_raises_engine_unavailable_and_cleans_up(home, busybox_rootfs, monkeypatch):
    import xcodon_runtime.engine_proot as ep

    real_run = ep.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "cp":
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return real_run(cmd, **kwargs)

    def fake_copytree(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ep.subprocess, "run", fake_run)
    monkeypatch.setattr(ep.shutil, "copytree", fake_copytree)

    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    with pytest.raises(EngineUnavailable):
        e.start(c)
    assert not (c.dir / "rootfs").exists()
