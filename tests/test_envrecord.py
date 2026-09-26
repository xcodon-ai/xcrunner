import hashlib
import io
import json
import threading
from pathlib import Path

import pytest

from tests.conftest import pack_rootfs_as_image
from xcodon_runtime import envrecord
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
    assert e["id"] == "sha256:" + img.id and e["source"] in ("daemon", "registry", "build", "test")
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
