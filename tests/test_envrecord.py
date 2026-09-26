import hashlib
import io
import json
import shutil
import threading
from pathlib import Path

import pytest

from tests.conftest import pack_rootfs_as_image
from xcodon_runtime import envrecord
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.imagestore import ImageStore

SP = "usr/lib/python3/site-packages"


def pip_meta(root: Path, name: str, version: str) -> None:
    d = root / SP / f"{name}-{version}.dist-info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")


@pytest.fixture
def img(home, tmp_path):
    root = tmp_path / "rootfs"
    pip_meta(root, "base", "1.0")
    return pack_rootfs_as_image(home, root, "xcodon-test/rec:latest")


@pytest.fixture
def env_dir(tmp_path):
    d = tmp_path / "proj" / ".xrunner-env"
    d.mkdir(parents=True)
    return d


def make_layer(env_dir: Path, image, ref: str, engine: str = "ns") -> Path:
    layer = env_dir / image.id
    (layer / ("upper" if engine == "ns" else "rootfs")).mkdir(parents=True)
    (layer / "image.json").write_text(json.dumps({"image_ref": ref, "image_id": image.id, "engine": engine,
                                                  "first_used": "2026-09-25T00:00:00+00:00"}))
    return layer


def test_empty_and_load_tolerate_missing_or_corrupt(env_dir):
    assert envrecord.load(env_dir) == envrecord.empty() == {"version": 1, "images": {}, "layers": {}, "conda": {}}
    (env_dir / "environment.json").write_text("{broken")
    assert envrecord.load(env_dir) == envrecord.empty()


def test_update_writes_sorted_json_atomically(env_dir):
    path = envrecord.update(env_dir, lambda doc: doc["conda"].update({"name:b": {"x": 1}, "name:a": {"x": 2}}))
    text = path.read_text()
    assert path == env_dir / "environment.json" and text.endswith("\n")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    assert not list(env_dir.glob("*.tmp")) and not list(env_dir.glob(".environment-*"))


def test_concurrent_updates_do_not_lose_writes(env_dir):
    def add(i):
        envrecord.update(env_dir, lambda doc: doc["conda"].__setitem__(f"name:e{i}", {"n": i}))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(envrecord.load(env_dir)["conda"]) == 8


def test_note_image_and_tag_move(home, img, env_dir, tmp_path):
    st = ImageStore(home, sources=[])
    st.annotate(img.id, repo_digests=["docker.io/x/rec@sha256:" + "d" * 64])
    err = io.StringIO()
    envrecord.note_image(env_dir, "rec:latest", img, st, err=err)
    e = envrecord.load(env_dir)["images"]["rec:latest"]
    assert e["id"] == "sha256:" + img.id and e["source"] == "test"
    assert e["repo_digests"] == ["docker.io/x/rec@sha256:" + "d" * 64]
    assert e["previous_ids"] == [] and e["first_used"] <= e["last_used"] and err.getvalue() == ""
    root2 = tmp_path / "rootfs2"
    pip_meta(root2, "base", "2.0")
    img2 = pack_rootfs_as_image(home, root2, "xcodon-test/rec:latest")
    envrecord.note_image(env_dir, "rec:latest", img2, st, err=err)
    e = envrecord.load(env_dir)["images"]["rec:latest"]
    assert e["id"] == "sha256:" + img2.id and e["previous_ids"] == ["sha256:" + img.id]
    assert err.getvalue() == (f"xrunner: rec:latest now points to {img2.id[:12]}; installs made on "
                              f"{img.id[:12]} stay in .xrunner-env/{img.id}\n")


def test_image_entry_maps_commit_to_build(home, img):
    st = ImageStore(home, sources=[])
    st.annotate(img.id, source="commit", parent="ab" * 32, dockerfile="FROM x\n")
    e = envrecord.image_entry(st, img)
    assert (e["source"], e["parent"], e["dockerfile"]) == ("build", "sha256:" + "ab" * 32, "FROM x\n")


def test_record_layer_ns(home, img, env_dir):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    envrecord.record_layer(env_dir, img.id, st)
    entry = envrecord.load(env_dir)["layers"][img.id]
    assert entry["ref"] == "rec:latest" and entry["engine"] == "ns"
    assert entry["packages"] == [{"manager": "pip", "name": "added", "version": "3.0", "change": "added",
                                  "location": "/" + SP}]


def test_record_layer_proot(home, img, env_dir):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "proot")
    pip_meta(layer / "rootfs", "base", "1.0")
    pip_meta(layer / "rootfs", "extra", "0.1")
    envrecord.record_layer(env_dir, img.id, st)
    assert [(p["name"], p["change"]) for p in envrecord.load(env_dir)["layers"][img.id]["packages"]] == [("extra", "added")]


def test_record_layer_with_a_missing_image_warns_and_keeps_going(home, env_dir, caplog):
    st = ImageStore(home, sources=[])
    fake_id = "f" * 64
    (env_dir / fake_id / "upper").mkdir(parents=True)
    envrecord.record_layer(env_dir, fake_id, st)
    assert fake_id not in envrecord.load(env_dir)["layers"]
    assert "not in the image store" in caplog.text


def explicit(prefix: Path, urls: list[str]) -> str:
    (prefix / "conda-meta").mkdir(parents=True, exist_ok=True)
    text = "# platform: linux-64\n@EXPLICIT\n" + "".join(u + "\n" for u in urls)
    (prefix / "conda-explicit.txt").write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def test_record_conda(env_dir, tmp_path):
    proj = env_dir.parent
    root = env_dir / "conda"
    outside = tmp_path / "outside-env"
    s1 = explicit(root / "envs" / "bwa_env", ["https://c/a.conda#1", "https://c/b.conda#2"])
    s2 = explicit(proj / "workspace" / "conda_env", ["https://c/a.conda#1"])
    explicit(outside, ["https://c/z.conda#9"])
    (root / ".home" / ".conda").mkdir(parents=True)
    (root / ".home" / ".conda" / "environments.txt").write_text(
        f"{proj / 'workspace' / 'conda_env'}\n{outside}\n")
    envrecord.record_conda(env_dir)
    assert envrecord.load(env_dir)["conda"] == {
        "name:bwa_env": {"explicit": ".xrunner-env/conda/envs/bwa_env/conda-explicit.txt", "sha256": s1, "packages": 2},
        "path:workspace/conda_env": {"explicit": "workspace/conda_env/conda-explicit.txt", "sha256": s2, "packages": 1},
    }


def test_record_all_rebuilds_from_disk_and_show(home, img, env_dir, tmp_path):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    explicit(env_dir / "conda" / "envs" / "tools", ["https://c/a.conda#1"])
    path = envrecord.record_all(env_dir, st)
    doc = json.loads(path.read_text())
    assert set(doc["images"]) == {"rec:latest"} and set(doc["layers"]) == {img.id} and set(doc["conda"]) == {"name:tools"}
    text = envrecord.show(env_dir)
    assert "rec:latest" in text and img.id[:12] in text and "added 3.0" in text and "name:tools" in text


# -- Fix round 1 ---------------------------------------------------------------


def test_record_all_rebuild_drops_deleted_layers_and_refs(home, img, env_dir):
    """Item 1: record_all rebuilds `layers` completely and drops orphaned refs."""
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    envrecord.record_all(env_dir, st)
    doc = envrecord.load(env_dir)
    assert img.id in doc["layers"] and "rec:latest" in doc["images"]

    shutil.rmtree(layer)
    envrecord.record_all(env_dir, st)
    doc = envrecord.load(env_dir)
    assert img.id not in doc["layers"] and "rec:latest" not in doc["images"]


def test_record_layer_removes_entry_when_upper_and_rootfs_are_both_gone(home, img, env_dir):
    """Item 1: record_layer itself drops the entry once neither upper nor rootfs remain."""
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    envrecord.record_layer(env_dir, img.id, st)
    assert img.id in envrecord.load(env_dir)["layers"]

    shutil.rmtree(layer / "upper")
    envrecord.record_layer(env_dir, img.id, st)
    assert img.id not in envrecord.load(env_dir)["layers"]


def test_previous_ids_excludes_current_and_record_all_keeps_recorded_id(home, env_dir, tmp_path):
    """Item 2: the current id never appears in its own previous_ids, in note_image or record_all."""
    st = ImageStore(home, sources=[])
    rootA = tmp_path / "rootfsA"
    pip_meta(rootA, "base", "1.0")
    imgA = pack_rootfs_as_image(home, rootA, "xcodon-test/rec2:latest")
    rootB = tmp_path / "rootfsB"
    pip_meta(rootB, "base", "2.0")
    imgB = pack_rootfs_as_image(home, rootB, "xcodon-test/rec2:latest")

    envrecord.note_image(env_dir, "rec2:latest", imgA, st)
    envrecord.note_image(env_dir, "rec2:latest", imgB, st)
    envrecord.note_image(env_dir, "rec2:latest", imgA, st)
    e = envrecord.load(env_dir)["images"]["rec2:latest"]
    assert e["id"] == "sha256:" + imgA.id and e["previous_ids"] == ["sha256:" + imgB.id]

    make_layer(env_dir, imgA, "rec2:latest", "ns")
    make_layer(env_dir, imgB, "rec2:latest", "ns")
    envrecord.record_all(env_dir, st)
    e = envrecord.load(env_dir)["images"]["rec2:latest"]
    assert e["id"] == "sha256:" + imgA.id and imgB.id[:12] in [p[7:19] for p in e["previous_ids"]]


def test_record_all_falls_back_to_older_present_image(home, env_dir, tmp_path):
    """Item 8: when the newest image is missing from the store, fall back to an older present one."""
    st = ImageStore(home, sources=[])
    root1 = tmp_path / "rootfs1"
    pip_meta(root1, "base", "1.0")
    img1 = pack_rootfs_as_image(home, root1, "xcodon-test/rec3:latest")
    make_layer(env_dir, img1, "rec3:latest", "ns")

    root2 = tmp_path / "rootfs2"
    pip_meta(root2, "base", "2.0")
    img2 = pack_rootfs_as_image(home, root2, "xcodon-test/rec3:latest")
    layer2 = env_dir / img2.id
    (layer2 / "upper").mkdir(parents=True)
    (layer2 / "image.json").write_text(json.dumps({"image_ref": "rec3:latest", "image_id": img2.id, "engine": "ns",
                                                    "first_used": "2026-09-26T00:00:00+00:00"}))
    shutil.rmtree(home.images / img2.id)  # img2 is newest on disk but no longer in the store

    envrecord.record_all(env_dir, st)
    e = envrecord.load(env_dir)["images"]["rec3:latest"]
    assert e["id"] == "sha256:" + img1.id


def test_record_all_switch_sets_last_used_from_new_first_used(home, env_dir, tmp_path):
    """Item 7: switching a ref to a different id takes last_used from the new entry's first_used."""
    st = ImageStore(home, sources=[])
    rootX = tmp_path / "rootfsX"
    pip_meta(rootX, "base", "1.0")
    imgX = pack_rootfs_as_image(home, rootX, "xcodon-test/rec4:latest")
    make_layer(env_dir, imgX, "rec4:latest", "ns")
    envrecord.record_all(env_dir, st)
    envrecord.update(env_dir, lambda doc: doc["images"]["rec4:latest"].update(
        {"last_used": "2020-01-01T00:00:00+00:00"}))

    shutil.rmtree(env_dir / imgX.id)
    rootY = tmp_path / "rootfsY"
    pip_meta(rootY, "base", "2.0")
    imgY = pack_rootfs_as_image(home, rootY, "xcodon-test/rec4:latest")
    make_layer(env_dir, imgY, "rec4:latest", "ns")

    envrecord.record_all(env_dir, st)
    e = envrecord.load(env_dir)["images"]["rec4:latest"]
    assert e["id"] == "sha256:" + imgY.id
    assert e["last_used"] == e["first_used"] != "2020-01-01T00:00:00+00:00"


def test_record_conda_resolves_symlinks_for_relative_keys(tmp_path):
    """Item 3: paths are resolved before relpath, so a symlinked project gives a clean key."""
    real_proj = tmp_path / "real" / "proj"
    (real_proj / ".xrunner-env").mkdir(parents=True)
    (real_proj / "ws" / "e1").mkdir(parents=True)
    link = tmp_path / "proj"
    link.symlink_to(real_proj)
    env_dir = link / ".xrunner-env"
    s = explicit(real_proj / "ws" / "e1", ["https://c/a.conda#1"])
    home_conda = env_dir / "conda" / ".home" / ".conda"
    home_conda.mkdir(parents=True)
    (home_conda / "environments.txt").write_text(f"{real_proj / 'ws' / 'e1'}\n")

    envrecord.record_conda(env_dir)
    assert envrecord.load(env_dir)["conda"] == {
        "path:ws/e1": {"explicit": "ws/e1/conda-explicit.txt", "sha256": s, "packages": 1},
    }


def test_record_conda_skips_relative_lines_in_environments_txt(env_dir, monkeypatch):
    """Item 4: a relative line in environments.txt is skipped, not resolved against cwd."""
    proj = env_dir.parent
    root = env_dir / "conda"
    explicit(proj / "workspace" / "rel_env", ["https://c/a.conda#1"])
    (root / ".home" / ".conda").mkdir(parents=True)
    (root / ".home" / ".conda" / "environments.txt").write_text("workspace/rel_env\n")
    monkeypatch.chdir(proj)

    envrecord.record_conda(env_dir)
    assert envrecord.load(env_dir)["conda"] == {}


def test_update_keeps_unknown_top_level_keys(env_dir):
    """Item 5: update() round-trips unknown top-level keys instead of dropping them."""
    (env_dir / "environment.json").write_text(
        json.dumps({"version": 1, "images": {}, "layers": {}, "conda": {}, "extra": {"x": 1}}))
    envrecord.update(env_dir, lambda doc: doc["conda"].__setitem__("name:a", {"n": 1}))
    doc = json.loads((env_dir / "environment.json").read_text())
    assert doc["extra"] == {"x": 1} and doc["conda"] == {"name:a": {"n": 1}}


def test_update_rejects_a_newer_version(env_dir):
    """Item 5: update() refuses a stored version newer than what this xrunner writes."""
    (env_dir / "environment.json").write_text(
        json.dumps({"version": 2, "images": {}, "layers": {}, "conda": {}}))
    with pytest.raises(XcodonError, match="version 2"):
        envrecord.update(env_dir, lambda doc: None)
    # load() still tolerates it, for display.
    assert envrecord.load(env_dir) == envrecord.empty()


def test_non_dict_image_entry_is_ignored_by_note_image_and_show(env_dir, home, img):
    """Item 6: a non-dict images entry does not crash note_image or show; both tolerate it."""
    (env_dir / "environment.json").write_text(
        json.dumps({"version": 1, "images": {"r:1": "oops"}, "layers": {}, "conda": {}}))
    text = envrecord.show(env_dir)
    assert "oops" not in text

    st = ImageStore(home, sources=[])
    envrecord.note_image(env_dir, "r:1", img, st)
    e = envrecord.load(env_dir)["images"]["r:1"]
    assert e["id"] == "sha256:" + img.id and e["previous_ids"] == []
