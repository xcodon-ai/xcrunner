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
