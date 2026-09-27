import json

import pytest

from tests.fake_registry import FakeRegistry, digest_of
from tests.test_registry import config_for, layer_bytes
from xcodon_runtime.api import Runtime
from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import RegistryClient


@pytest.fixture
def reg():
    with FakeRegistry() as r:
        yield r


def test_registry_fetch_reports_the_tags_manifest_digest(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    client = RegistryClient(home, scheme="http")
    f1 = client.fetch(Reference(reg.host, "lib/hello", "latest"), Platform("linux", "amd64"))
    assert f1.repo_digests == [f"{reg.host}/lib/hello@{digest_of(reg.manifests[('lib/hello', 'latest')][0])}"]
    f2 = client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "amd64"))
    index_digest = digest_of(reg.manifests[("lib/multi", "v1")][0])
    assert f2.repo_digests == [f"{reg.host}/lib/multi@{index_digest}"], "the tag's own (index) digest, as docker records it"


def test_pull_keeps_repo_digests_in_the_manifest(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    st = ImageStore(home, sources=[RegistryClient(home, scheme="http")])
    img = st.pull(f"{reg.host}/lib/hello:latest")
    assert st.manifest(img)["repo_digests"] == [f"{reg.host}/lib/hello@{digest_of(reg.manifests[('lib/hello', 'latest')][0])}"]


def test_annotate_merges_repo_digests_and_replaces_other_fields(home, busybox_image):
    st = ImageStore(home, sources=[])
    st.annotate(busybox_image.id, repo_digests=["r/x@sha256:" + "b" * 64])
    st.annotate(busybox_image.id, repo_digests=["r/x@sha256:" + "a" * 64, "r/x@sha256:" + "b" * 64], dockerfile="FROM x\n")
    st.annotate(busybox_image.id, dockerfile="FROM y\n")
    m = st.manifest(busybox_image)
    assert m["repo_digests"] == ["r/x@sha256:" + "a" * 64, "r/x@sha256:" + "b" * 64]
    assert m["dockerfile"] == "FROM y\n"
    assert m["config"].endswith(busybox_image.id), "existing fields are kept"


def test_daemon_repo_digests(home, monkeypatch):
    src = DaemonSource(home)
    monkeypatch.setattr(src, "_exe", lambda: "/usr/bin/docker")

    class R:
        returncode = 0
        stdout = '["docker.io/hubentu/coala-runtime-python@sha256:' + "c" * 64 + '"]\n'

    monkeypatch.setattr("xcodon_runtime.daemon.subprocess.run", lambda *a, **k: R())
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == \
        ["docker.io/hubentu/coala-runtime-python@sha256:" + "c" * 64]
    R.stdout = "null\n"
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == []
    R.returncode = 1
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == []


def test_build_records_its_dockerfile(home, busybox_image, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    text = "FROM xcodon-test/busybox\nENV A=1\n"
    (ctx / "Dockerfile").write_text(text)
    img = rt.build(ctx, tags=["xcodon-test/rec:1"])
    m = rt.images.manifest(img)
    assert m["dockerfile"] == text and m["source"] == "commit"


def test_build_with_only_from_does_not_annotate_the_base_image(home, busybox_image, engine_name, tmp_path):
    """FROM alone (or FROM plus only ignored lines) ends on the base image itself:
    that image must not gain a dockerfile field, whether it was pulled or was
    itself the final image of an earlier `xcrunner build`."""
    rt = Runtime(home.path, engine=engine_name)
    assert "dockerfile" not in rt.images.manifest(busybox_image)

    ctx = tmp_path / "ctx-from-only"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\n")
    img = rt.build(ctx)
    assert img.id == busybox_image.id
    assert "dockerfile" not in rt.images.manifest(busybox_image)

    ctx2 = tmp_path / "ctx-from-expose"
    ctx2.mkdir()
    (ctx2 / "Dockerfile").write_text("FROM xcodon-test/busybox\nEXPOSE 80\n")
    img2 = rt.build(ctx2)
    assert img2.id == busybox_image.id
    assert "dockerfile" not in rt.images.manifest(busybox_image)


def test_package_inventory_treats_a_non_object_cache_as_a_miss(home, tmp_path):
    """packages.json with valid but non-dict JSON (e.g. `[]`) must not crash; it
    is treated like any other unusable cache and the rootfs is rescanned."""
    from tests.conftest import pack_rootfs_as_image

    root = tmp_path / "rootfs-nonobj"
    meta = root / "usr/lib/python3/site-packages/demo2-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Name: demo2\nVersion: 1.0\n")
    img = pack_rootfs_as_image(home, root, "xcodon-test/inv-nonobj:1")
    (img.dir / "packages.json").write_text("[]")
    st = ImageStore(home, sources=[])
    pkgs = st.package_inventory(img)
    assert [(p.manager, p.name, p.version) for p in pkgs] == [("pip", "demo2", "1.0")]


def test_package_inventory_is_cached(home, tmp_path):
    from tests.conftest import pack_rootfs_as_image

    root = tmp_path / "rootfs"
    meta = root / "usr/lib/python3/site-packages/demo-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Name: demo\nVersion: 1.0\n")
    img = pack_rootfs_as_image(home, root, "xcodon-test/inv:1")
    st = ImageStore(home, sources=[])
    first = st.package_inventory(img)
    assert [(p.manager, p.name, p.version) for p in first] == [("pip", "demo", "1.0")]
    cache = json.loads((img.dir / "packages.json").read_text())
    assert cache["version"] == 1 and cache["packages"][0]["path"].endswith("demo-1.0.dist-info")
    (img.rootfs / "usr/lib/python3/site-packages/demo-1.0.dist-info/METADATA").unlink()
    assert st.package_inventory(img) == first, "the cached inventory is used; the rootfs is not scanned again"
