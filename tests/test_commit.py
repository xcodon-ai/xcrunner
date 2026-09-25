import json

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.imagestore import ImageStore, apply_config_changes


def test_apply_config_changes_merges_env_and_labels():
    cfg = {"config": {"Env": ["A=1", "PATH=/bin"], "Labels": {"x": "1"}}}
    apply_config_changes(cfg, {"Env": ["A=2", "B=3"], "Labels": {"y": "2"}, "WorkingDir": "/w", "Cmd": ["sh"]})
    assert cfg["config"]["Env"] == ["A=2", "PATH=/bin", "B=3"]
    assert cfg["config"]["Labels"] == {"x": "1", "y": "2"}
    assert cfg["config"]["WorkingDir"] == "/w" and cfg["config"]["Cmd"] == ["sh"]


def test_apply_config_changes_replaces_shell():
    cfg = {"config": {"Shell": ["/bin/sh", "-c"]}}
    apply_config_changes(cfg, {"Shell": ["/bin/bash", "-c"]})
    assert cfg["config"]["Shell"] == ["/bin/bash", "-c"]


def test_store_commit_layer_and_config(home, busybox_image, tmp_path):
    st = ImageStore(home, sources=[])
    layer = tmp_path / "layer"
    (layer / "opt").mkdir(parents=True)
    (layer / "opt" / "tool").write_text("t")
    (layer / "bin").mkdir()
    (layer / "bin" / ".wh.ls").touch()
    img = st.commit(busybox_image, layer, changes={"Env": ["TOOL=1"]}, ref="xcodon-test/committed:v1", created_by="test")
    assert img.id != busybox_image.id and len(img.id) == 64
    assert (img.rootfs / "opt" / "tool").read_text() == "t"
    assert not (img.rootfs / "bin" / "ls").exists(), "whiteout applied"
    assert (img.rootfs / "bin" / "busybox").exists(), "base layers kept"
    assert "TOOL=1" in img.config["config"]["Env"]
    assert img.config["rootfs"]["diff_ids"][:-1] == busybox_image.config["rootfs"]["diff_ids"]
    m = json.loads((img.dir / "manifest.json").read_text())
    assert m["source"] == "commit" and m["parent"] == busybox_image.id
    assert img.config["history"][-1]["created_by"] == "test"
    assert st.get("xcodon-test/committed:v1").id == img.id
    assert st.layer_dirs(img)[-1].exists()


def test_store_commit_is_deterministic_and_config_only(home, busybox_image, tmp_path):
    st = ImageStore(home, sources=[])
    a = st.commit(busybox_image, None, changes={"Cmd": ["/bin/true"]}, created_by="x")
    assert a.config["rootfs"]["diff_ids"] == busybox_image.config["rootfs"]["diff_ids"]
    assert a.config["config"]["Cmd"] == ["/bin/true"]
    assert a.refs == []
    assert a.id in {i.id for i in st.untagged()}
    st.tag(a.id, "xcodon-test/cfg:latest")
    assert st.get("xcodon-test/cfg:latest").id == a.id
    assert a.id not in {i.id for i in st.untagged()}


def test_prune_all_removes_untagged_images(home, busybox_image):
    st = ImageStore(home, sources=[])
    a = st.commit(busybox_image, None, changes={"Cmd": ["/bin/true"]}, created_by="x")
    removed = st.prune(all=True)
    assert any(p.name == a.id for p in removed)
    assert st.get(a.id) is None and st.get("xcodon-test/busybox") is not None


def test_runtime_commit_container(home, busybox_image, engine_name):
    rt = Runtime(home.path, engine=engine_name)
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    try:
        assert rt.exec(c, ["/bin/sh", "-c", "mkdir -p /opt/t && echo v > /opt/t/f && rm /bin/ls"]).code == 0
        with pytest.raises(XcodonError, match="running"):
            rt.commit(c, "xcodon-test/snap:latest")
        rt.stop(c)
        img = rt.commit(c, "xcodon-test/snap:latest", changes={"Env": ["SNAP=1"]}, message="snap")
    finally:
        # force=True: a failed assertion above may leave the container running,
        # and it must never be left behind as a leaked keeper.
        rt.remove(c, force=True)
    with open(home.path / "out", "wb") as out:
        code = rt.run(
            "xcodon-test/snap:latest",
            command=["/bin/sh", "-c", "cat /opt/t/f; echo $SNAP; ls /bin/ls 2>/dev/null || echo gone"],
            rm=True,
            stdout=out,
        )
    assert code == 0
    assert (home.path / "out").read_bytes() == b"v\n1\ngone\n"
    assert img.id is not None


def test_runtime_commit_env_dir(home, busybox_image, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    env = tmp_path / "env"
    assert rt.run(
        "xcodon-test/busybox",
        command=["/bin/sh", "-c", "mkdir -p /usr/local/x && echo tool > /usr/local/x/t"],
        rm=True,
        env_dir=env,
    ) == 0
    img = rt.commit(None, "xcodon-test/fromenv:latest", env_dir=env, image="xcodon-test/busybox")
    assert (img.rootfs / "usr/local/x/t").read_text() == "tool\n"
    # a locked layer is refused
    c = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c)
    try:
        if engine_name == "ns":
            with pytest.raises(XcodonError, match="in use"):
                rt.commit(None, "xcodon-test/fromenv:v2", env_dir=env, image="xcodon-test/busybox")
    finally:
        rt.stop(c)
        rt.remove(c)


@pytest.mark.ns
def test_runtime_commit_container_env_layer_in_use_is_refused(home, busybox_image, tmp_path):
    """Two containers sharing one env_dir: committing the stopped one must not touch a running peer's layer.

    ns-only: the lock this relies on is only ever taken by the ns engine's keeper.
    """
    rt = Runtime(home.path, engine="ns")
    env = tmp_path / "env"
    a = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(a)
    rt.stop(a)
    b = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(b)
    try:
        with pytest.raises(XcodonError, match="in use"):
            rt.commit(a, "xcodon-test/shared-env-snap:latest")
    finally:
        rt.stop(b)
        rt.remove(b)
        rt.remove(a)


def test_commit_env_dir_snapshot_does_not_share_inodes_with_the_live_layer(home, busybox_image, engine_name, tmp_path):
    """A committed image must not mutate later when the live env layer is written to again."""
    rt = Runtime(home.path, engine=engine_name)
    env = tmp_path / "env"
    assert rt.run(
        "xcodon-test/busybox",
        command=["/bin/sh", "-c", "mkdir -p /usr/local/x && echo one > /usr/local/x/t"],
        rm=True,
        env_dir=env,
    ) == 0
    img = rt.commit(None, "xcodon-test/fromenv-inode:latest", env_dir=env, image="xcodon-test/busybox")
    assert (img.rootfs / "usr/local/x/t").read_text() == "one\n"
    assert rt.run(
        "xcodon-test/busybox",
        command=["/bin/sh", "-c", "echo two >> /usr/local/x/t"],
        rm=True,
        env_dir=env,
    ) == 0
    assert (img.rootfs / "usr/local/x/t").read_text() == "one\n", \
        "the committed image must not change when the live env layer is written to again"


def test_resolve_reimports_when_daemon_id_changed(home, busybox_image, monkeypatch, tmp_path):
    """A daemon-sourced image whose tag now points at a different id is pulled again."""
    from xcodon_runtime.daemon import DaemonSource

    m = json.loads((busybox_image.dir / "manifest.json").read_text())
    m["source"] = "daemon"
    m["daemon_id"] = "sha256:" + "d" * 64
    (busybox_image.dir / "manifest.json").write_text(json.dumps(m))
    rt = Runtime(home.path, engine="ns")
    calls = []
    monkeypatch.setattr(DaemonSource, "available", lambda self: True)
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + "f" * 64)
    monkeypatch.setattr(ImageStore, "pull", lambda self, ref, platform=None: calls.append(ref) or busybox_image)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"]
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + "d" * 64)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"], "same id: no second pull"
    m["source"] = "commit"
    (busybox_image.dir / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + "e" * 64)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"], "locally built images are never replaced"
