import gzip
import hashlib
import io
import json
import tarfile
import threading
import time
from pathlib import Path

import pytest

import xcodon_runtime.imagestore as imagestore
from xcodon_runtime.errors import ImageNotFound, PullError
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform
from xcodon_runtime.registry import FetchedImage, FetchedLayer


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def layer(home, files: dict[str, bytes]) -> tuple[FetchedLayer, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, content in files.items():
            i = tarfile.TarInfo(name)
            i.size = len(content)
            t.addfile(i, io.BytesIO(content))
    raw = buf.getvalue()
    gz = gzip.compress(raw)
    path = home.blobs / sha(gz).split(":")[1]
    path.write_bytes(gz)
    return FetchedLayer(sha(gz), "application/vnd.oci.image.layer.v1.tar+gzip", len(gz), path), sha(raw)


class FakeSource:
    name = "fake"

    def __init__(self, home, layers_spec):
        self.home = home
        self.layers_spec = layers_spec
        self.calls = 0

    def fetch(self, ref, platform):
        self.calls += 1
        layers, diff_ids = [], []
        for spec in self.layers_spec:
            l, diff = layer(self.home, spec)
            layers.append(l)
            diff_ids.append(diff)
        config = {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Env": ["PATH=/bin"], "Cmd": ["/bin/sh"], "WorkingDir": "/w"},
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }
        cbytes = json.dumps(config).encode()
        (self.home.blobs / sha(cbytes).split(":")[1]).write_bytes(cbytes)
        return FetchedImage(sha(cbytes), config, layers, source="fake")


@pytest.fixture
def store(home):
    src = FakeSource(home, [{"a": b"A", "dir/x": b"x"}, {".wh.a": b"", "b": b"B"}])
    return ImageStore(home, sources=[src]), src


def test_pull_builds_rootfs_and_ref(store):
    st, src = store
    img = st.pull("example/app:v1")
    assert img.refs == ["docker.io/example/app:v1"]
    assert (img.rootfs / "b").read_bytes() == b"B"
    assert not (img.rootfs / "a").exists()
    assert (img.rootfs / "dir/x").read_bytes() == b"x"
    assert img.config["config"]["WorkingDir"] == "/w"
    assert len(list(st.home.layers.iterdir())) == 2
    assert not list(st.home.blobs.iterdir()), "blobs are deleted after import"


def test_second_pull_of_same_image_hits_store_only_on_get(store):
    st, src = store
    st.pull("example/app:v1")
    assert st.get("example/app:v1") is not None
    assert src.calls == 1
    st.pull("example/app:v1")
    assert src.calls == 2, "pull always fetches; get is the cache"


def test_get_by_id_prefix(store):
    st, _ = store
    img = st.pull("example/app:v1")
    assert st.get(img.id[:12]).id == img.id
    assert st.get("nonexistent:latest") is None
    with pytest.raises(ImageNotFound):
        st.require("nonexistent:latest")


def test_inspect_shape(store):
    st, _ = store
    img = st.pull("example/app:v1")
    doc = st.inspect("example/app:v1")
    assert isinstance(doc, list) and len(doc) == 1
    assert doc[0]["Id"] == f"sha256:{img.id}"
    assert doc[0]["RepoTags"] == ["docker.io/example/app:v1"]
    assert doc[0]["Config"]["Cmd"] == ["/bin/sh"]
    assert doc[0]["RootFS"]["Type"] == "layers"
    assert doc[0]["Architecture"] == "amd64"


def test_remove_and_prune(store):
    st, _ = store
    img = st.pull("example/app:v1")
    st.remove("example/app:v1")
    assert st.get("example/app:v1") is None
    assert not img.dir.exists()
    assert len(list(st.home.layers.iterdir())) == 2
    pruned = st.prune()
    assert len(pruned) == 2
    assert not list(st.home.layers.iterdir())


def test_two_tags_share_one_image(store):
    st, _ = store
    a = st.pull("example/app:v1")
    b = st.pull("example/app:latest")
    assert a.id == b.id
    assert sorted(st.get(a.id).refs) == ["docker.io/example/app:latest", "docker.io/example/app:v1"]
    st.remove("example/app:v1")
    assert st.get("example/app:latest") is not None
    assert a.dir.exists()


def test_all_sources_fail(home):
    class Failing:
        name = "failing"

        def fetch(self, ref, platform):
            raise PullError("nope")

    st = ImageStore(home, sources=[Failing()])
    with pytest.raises(PullError, match="failing: nope"):
        st.pull("x/y")


def test_layer_count_mismatch_is_error(home):
    class Bad(FakeSource):
        def fetch(self, ref, platform):
            f = super().fetch(ref, platform)
            f.config["rootfs"]["diff_ids"].append("sha256:" + "0" * 64)
            return f

    st = ImageStore(home, sources=[Bad(home, [{"a": b"A"}])])
    with pytest.raises(PullError, match="diff_ids"):
        st.pull("x/y")


def test_second_pull_of_same_image_skips_layer_extraction(store, monkeypatch):
    st, src = store
    real_extract_layer = imagestore.extract_layer
    calls = {"n": 0}

    def counting_extract_layer(stream, dest):
        calls["n"] += 1
        return real_extract_layer(stream, dest)

    monkeypatch.setattr(imagestore, "extract_layer", counting_extract_layer)

    st.pull("example/app:v1")
    after_first = calls["n"]
    assert after_first == 2  # one call per layer in the fake source

    st.pull("example/app:v1")
    assert calls["n"] == after_first, "second pull of the same image must not re-extract already-stored layers"


def test_prune_waits_for_in_progress_import(store):
    st, _ = store
    st.pull("example/app:v1")  # populate at least one layer so prune has work either way

    result: dict = {}

    def run_prune():
        result["pruned"] = st.prune()

    with st.home.lock("store", shared=True):
        t = threading.Thread(target=run_prune)
        t.start()
        time.sleep(0.1)
        assert t.is_alive(), "prune should block while a shared (import) holder has the store lock"

    t.join()
    assert "pruned" in result
