"""Persistent env folder: the writable layer lives in a host folder keyed by image id."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.envdir import ENV_INFO_NAME, ENV_LOCK_NAME, env_layer_dir


@pytest.fixture
def rt(home, busybox_image, engine_name) -> Runtime:
    return Runtime(home.path, engine=engine_name)


def test_install_persists_across_containers(rt, busybox_image, tmp_path):
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    assert c1.env_dir == str(env)
    rt.start(c1)
    assert rt.exec(c1, "mkdir -p /usr/local/tool && echo v1 > /usr/local/tool/VERSION").code == 0
    rt.stop(c1)
    rt.remove(c1)
    layer = env_layer_dir(str(env), busybox_image.id)
    assert layer.is_dir(), "env layer keyed by image id"
    info = json.loads((layer / ENV_INFO_NAME).read_text())
    assert info["image_id"] == busybox_image.id
    assert not c1.dir.exists(), "container dir removed"
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c2)
    assert rt.exec(c2, ["/bin/cat", "/usr/local/tool/VERSION"]).stdout == b"v1\n"
    rt.stop(c2)
    rt.remove(c2)
    assert layer.is_dir(), "rm never deletes the env folder"


def test_env_is_keyed_by_image(rt, home, busybox_rootfs, tmp_path):
    from tests.conftest import pack_rootfs_as_image

    other = pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/other:latest", config={"Env": ["PATH=/bin", "OTHER=1"]})
    env = tmp_path / "env"
    a = rt.create("xcodon-test/busybox", env_dir=env)
    b = rt.create("xcodon-test/other", env_dir=env)
    rt.start(a)
    rt.exec(a, "echo a > /marker")
    rt.stop(a)
    rt.start(b)
    assert rt.exec(b, ["/bin/cat", "/marker"]).code != 0, "different image, different layer"
    rt.stop(b)
    assert (env / a.image_id).is_dir() and (env / other.id).is_dir()


def test_relative_env_dir_is_rejected(rt):
    from xcodon_runtime.errors import XcodonError

    with pytest.raises(XcodonError, match="absolute"):
        rt.create("xcodon-test/busybox", env_dir="relative/env")


def test_second_start_waits_for_first_to_stop(rt, engine_name, tmp_path):
    if engine_name != "ns":
        pytest.skip("only the ns engine locks the layer")
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c1)
    started = threading.Event()
    err: list[BaseException] = []

    def second():
        try:
            rt.start(c2)
            started.set()
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=second, daemon=True)
    t.start()
    time.sleep(0.8)
    assert not started.is_set(), "second start must block while the first holds the layer"
    lock = env_layer_dir(str(env), c1.image_id) / ENV_LOCK_NAME
    assert lock.exists()
    rt.stop(c1)
    t.join(timeout=30)
    assert not err, err
    assert started.is_set()
    assert rt.exec(c2, ["/bin/true"]).code == 0
    rt.stop(c2)


def test_proot_reuses_rootfs_copy(rt, engine_name, tmp_path):
    if engine_name != "proot":
        pytest.skip("proot only")
    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c1)
    rootfs = env_layer_dir(str(env), c1.image_id) / "rootfs"
    assert (rootfs / "bin" / "busybox").exists()
    stamp = os.stat(rootfs / "bin" / "busybox").st_mtime_ns
    rt.exec(c1, "echo p > /persist")
    rt.stop(c1)
    rt.remove(c1)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c2)
    assert os.stat(rootfs / "bin" / "busybox").st_mtime_ns == stamp, "no second copy"
    assert rt.exec(c2, ["/bin/cat", "/persist"]).stdout == b"p\n"
    rt.stop(c2)


def test_run_with_env_dir(rt, tmp_path):
    env = tmp_path / "env"
    assert rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "echo r > /from-run"], rm=True, env_dir=env) == 0
    assert rt.run("xcodon-test/busybox", command=["/bin/cat", "/from-run"], rm=True, env_dir=env) == 0


def test_prepare_env_layer_is_race_safe(rt, busybox_image, tmp_path):
    from xcodon_runtime.envdir import prepare_env_layer

    env = tmp_path / "env"
    c = rt.create("xcodon-test/busybox", env_dir=env)
    results: list[Path] = []

    def call():
        results.append(prepare_env_layer(c))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    layer = results[0]
    assert all(r == layer for r in results)
    info_path = layer / ENV_INFO_NAME
    info = json.loads(info_path.read_text())
    assert info["image_id"] == busybox_image.id
    first_used = info["first_used"]

    prepare_env_layer(c)  # a later call must not touch the record
    again = json.loads(info_path.read_text())
    assert again["first_used"] == first_used


def test_env_dir_not_created_on_duplicate_name(rt, tmp_path):
    from xcodon_runtime.errors import XcodonError

    rt.create("xcodon-test/busybox", name="dup")
    fresh = tmp_path / "fresh-env"
    with pytest.raises(XcodonError):
        rt.create("xcodon-test/busybox", name="dup", env_dir=fresh)
    assert not fresh.exists(), "env_dir must not be created when the store.create fails"


def test_clear_overlay_work_skips_while_the_layer_is_locked(rt, engine_name, tmp_path):
    """A blocked second start may already own work/work, so the stopper must leave it alone."""
    if engine_name != "ns":
        pytest.skip("only the ns engine has an overlay work directory")
    import fcntl

    from xcodon_runtime.engine_ns import NsEngine

    env = tmp_path / "env"
    c = rt.create("xcodon-test/busybox", env_dir=env)
    layer = env_layer_dir(str(env), c.image_id)
    (layer / "work" / "work").mkdir(parents=True)
    fd = os.open(layer / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        NsEngine()._clear_overlay_work(c)
        assert (layer / "work" / "work").is_dir(), "work/work belongs to the lock holder"
    finally:
        os.close(fd)
    NsEngine()._clear_overlay_work(c)
    assert not (layer / "work" / "work").exists()


def test_env_lock_wait_names_the_holder(tmp_path, caplog):
    import fcntl
    import logging

    from xcodon_runtime.envdir import ENV_HOLDER_NAME, acquire_env_lock

    layer = tmp_path / "layer"
    layer.mkdir()
    (layer / ENV_HOLDER_NAME).write_text("abc123def456\n")
    held = os.open(layer / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    got: list[int] = []
    caplog.set_level(logging.WARNING, logger="xcodon_runtime.envdir")
    t = threading.Thread(target=lambda: got.append(acquire_env_lock(layer, "fedcba654321")), daemon=True)
    t.start()
    time.sleep(0.5)
    os.close(held)
    t.join(timeout=10)
    assert got, "acquire_env_lock never returned"
    os.close(got[0])
    waits = [r for r in caplog.records if r.levelno == logging.WARNING and "waiting for env layer" in r.getMessage()]
    assert len(waits) == 1
    assert "container abc123def456 holds it" in waits[0].getMessage()
    assert "xcrunner stop abc123def456" in waits[0].getMessage()
    assert (layer / ENV_HOLDER_NAME).read_text().strip() == "fedcba654321"


def test_overlay_failure_hint_only_with_env_dir():
    from types import SimpleNamespace

    from xcodon_runtime.engine_ns import _overlay_failure_hint

    hint = _overlay_failure_hint(SimpleNamespace(env_dir="/some/env"))
    assert "local filesystem that supports overlay upper layers" in hint
    assert "not NFS" in hint
    assert _overlay_failure_hint(SimpleNamespace(env_dir=None)) == ""


def test_overlay_mount_error_names_the_three_paths():
    import errno

    from xcodon_runtime import keeper

    def failing_mount(*args):
        raise OSError(errno.EINVAL, "Invalid argument")

    plan = {"lower": "/img/rootfs", "upper": "/env/abc/upper", "work": "/env/abc/work", "merged": "/c/merged"}
    with pytest.raises(OSError) as e:
        keeper._mount_overlay(plan, mount=failing_mount)
    assert e.value.errno == errno.EINVAL
    msg = str(e.value)
    assert "overlay mount failed" in msg
    assert "lower=/img/rootfs" in msg and "upper=/env/abc/upper" in msg and "work=/env/abc/work" in msg


def test_proot_interrupted_copy_is_redone(rt, engine_name, tmp_path):
    if engine_name != "proot":
        pytest.skip("proot only")
    env = tmp_path / "env"
    c = rt.create("xcodon-test/busybox", env_dir=env)
    layer = env_layer_dir(str(env), c.image_id)
    (layer / "rootfs.tmp").mkdir(parents=True)
    (layer / "rootfs.tmp" / "stray").write_text("half a copy\n")
    rt.start(c)
    try:
        assert (layer / "rootfs" / "bin" / "busybox").exists()
        assert not (layer / "rootfs" / "stray").exists()
        assert not (layer / "rootfs.tmp").exists()
    finally:
        rt.stop(c)


def test_read_paths_do_not_recreate_a_deleted_env_folder(rt, engine_name, tmp_path):
    import shutil

    env = tmp_path / "env"
    c = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c)
    rt.stop(c)
    shutil.rmtree(env)
    engine = rt._engine(c)
    if engine_name == "proot":
        from xcodon_runtime.engine_proot import STARTED_MARKER

        (c.dir / STARTED_MARKER).write_text("stale marker")
    assert not engine.is_running(c)
    engine.stop(c)
    assert not env.exists(), "read paths must not create the env folder"


def test_keeper_failing_before_ready_releases_the_env_lock(rt, engine_name, tmp_path, monkeypatch):
    if engine_name != "ns":
        pytest.skip("only the ns engine locks the layer")
    import sys

    from xcodon_runtime import engine_ns
    from xcodon_runtime.errors import EngineUnavailable

    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    real_argv = engine_ns.KEEPER_ARGV
    monkeypatch.setattr(engine_ns, "KEEPER_ARGV", [sys.executable, "-c", "raise SystemExit(1)"])
    with pytest.raises(EngineUnavailable, match="not NFS"):
        rt.start(c1)
    monkeypatch.setattr(engine_ns, "KEEPER_ARGV", real_argv)

    started = threading.Event()
    err: list[BaseException] = []

    def second():
        try:
            rt.start(c2)
            started.set()
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=second, daemon=True)
    t.start()
    t.join(timeout=10)
    try:
        assert not err, err
        assert started.is_set(), "the failed keeper must not keep the env lock"
        assert rt.exec(c2, ["/bin/true"]).code == 0
    finally:
        t.join(timeout=30)
        rt.stop(c2)


def test_env_is_reusable_after_the_keeper_is_killed(rt, engine_name, tmp_path):
    if engine_name != "ns":
        pytest.skip("ns only: the keeper holds the layer")
    import signal

    from xcodon_runtime.engine_ns import KEEPER_PID, process_start_time

    env = tmp_path / "env"
    c1 = rt.create("xcodon-test/busybox", env_dir=env)
    c2 = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c1)
    try:
        assert rt.exec(c1, "echo before > /kept").code == 0
        pid = json.loads((c1.dir / KEEPER_PID).read_text())["pid"]
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while process_start_time(pid) is not None:
            assert time.monotonic() < deadline, "the killed keeper never went away"
            time.sleep(0.02)
        rt.start(c2)
        try:
            assert rt.exec(c2, "echo after > /written").code == 0
            assert rt.exec(c2, ["/bin/cat", "/written"]).stdout == b"after\n"
            assert rt.exec(c2, ["/bin/cat", "/kept"]).stdout == b"before\n"
        finally:
            rt.stop(c2)
    finally:
        rt.stop(c1)


def test_stop_leaves_a_layer_that_can_be_deleted(rt, engine_name, tmp_path):
    import shutil

    env = tmp_path / "env"
    c = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c)
    assert rt.exec(c, "echo x > /file").code == 0
    rt.stop(c)
    layer = env_layer_dir(str(env), c.image_id)
    assert not (layer / "work" / "work").exists()
    shutil.rmtree(layer)
    assert not layer.exists()
