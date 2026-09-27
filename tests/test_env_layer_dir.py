"""XCRUNNER_ENV_LAYER_DIR: env folder layers kept on another disk. See spec section 16.5."""

import hashlib
import os
from pathlib import Path

import pytest

from xcodon_runtime import envrecord
from xcodon_runtime.api import Runtime
from xcodon_runtime.envdir import ENV_LAYER_DIR_ENV, env_layer_dir, env_layer_root
from xcodon_runtime.errors import XcodonError


def _key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def test_the_setting_name():
    assert ENV_LAYER_DIR_ENV == "XCRUNNER_ENV_LAYER_DIR"


def test_unset_keeps_layers_in_the_env_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_LAYER_DIR_ENV, raising=False)
    env = tmp_path / "proj" / ".xcrunner-env"
    assert env_layer_root(env) == env
    assert env_layer_dir(str(env), "ab" * 32) == env / ("ab" * 32)


def test_set_moves_layers_under_a_key_of_the_env_folder(tmp_path, monkeypatch):
    layers = tmp_path / "layers"
    monkeypatch.setenv(ENV_LAYER_DIR_ENV, str(layers))
    env = tmp_path / "proj" / ".xcrunner-env"
    env.mkdir(parents=True)
    assert env_layer_root(env) == layers.resolve() / _key(env)
    assert env_layer_dir(str(env), "cd" * 32) == layers.resolve() / _key(env) / ("cd" * 32)


def test_a_linked_env_folder_shares_its_targets_layers(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LAYER_DIR_ENV, str(tmp_path / "layers"))
    real = tmp_path / "real-env"
    real.mkdir()
    link = tmp_path / "link-env"
    link.symlink_to(real)
    assert env_layer_root(link) == env_layer_root(real)


@pytest.mark.parametrize("bad", ["a,b", "a:b"])
def test_overlay_separators_are_refused(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(ENV_LAYER_DIR_ENV, str(tmp_path / bad))
    with pytest.raises(XcodonError, match=ENV_LAYER_DIR_ENV):
        env_layer_root(tmp_path / "env")


def test_env_layers_live_in_the_layer_dir(home, busybox_image, engine_name, tmp_path, monkeypatch):
    layers = tmp_path / "layers"
    monkeypatch.setenv(ENV_LAYER_DIR_ENV, str(layers))
    env = tmp_path / "proj" / ".xcrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    ref = "xcodon-test/busybox"
    image = rt.images.get(ref)
    devnull = open(os.devnull, "w")
    assert rt.run(ref, ["/bin/sh", "-c", "echo kept > /kept"], rm=True, env_dir=env, stdout=devnull) == 0
    out = tmp_path / "out"
    with open(out, "wb") as f:
        assert rt.run(ref, ["/bin/cat", "/kept"], rm=True, env_dir=env, stdout=f) == 0
    assert out.read_bytes() == b"kept\n"

    root = layers.resolve() / _key(env)
    layer = root / image.id
    assert (layer / ("upper" if engine_name == "ns" else "rootfs")).is_dir()
    assert (root / "source").read_text().strip() == str(env.resolve())
    assert not (env / image.id).exists()
    assert image.id in envrecord.load(env)["layers"]

    committed = rt.commit(None, "xcodon-test/from-env", env_dir=env, image=ref)
    with open(out, "wb") as f:
        assert rt.run("xcodon-test/from-env", ["/bin/cat", "/kept"], rm=True, stdout=f) == 0
    assert out.read_bytes() == b"kept\n"
    assert committed.id != image.id


def test_record_rebuild_finds_layers_in_the_layer_dir(home, busybox_image, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_LAYER_DIR_ENV, str(tmp_path / "layers"))
    env = tmp_path / "proj" / ".xcrunner-env"
    rt = Runtime(home.path, engine="proot")
    from xcodon_runtime.engine_proot import find_proot

    if find_proot() is None:
        pytest.skip("no proot binary")
    ref = "xcodon-test/busybox"
    assert rt.run(ref, ["/bin/sh", "-c", "true"], rm=True, env_dir=env, stdout=open(os.devnull, "w")) == 0
    (env / envrecord.RECORD_NAME).unlink()
    envrecord.record_all(env, rt.images)
    assert rt.images.get(ref).id in envrecord.load(env)["layers"]
