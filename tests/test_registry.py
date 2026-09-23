import gzip
import io
import json
import tarfile

import pytest

from tests.fake_registry import FakeRegistry, digest_of
from xcodon_runtime.errors import PullError
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import RegistryClient, select_platform


def layer_bytes(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    return gzip.compress(buf.getvalue())


def config_for(layers: list[bytes]) -> dict:
    diff_ids = []
    for gz in layers:
        diff_ids.append(digest_of(gzip.decompress(gz)))
    return {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/bin"], "Cmd": ["sh"]},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
    }


@pytest.fixture
def reg():
    with FakeRegistry() as r:
        yield r


def test_fetch_single_arch_image(home, reg):
    layers = [layer_bytes("a", b"A"), layer_bytes("b", b"B")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    client = RegistryClient(home, scheme="http")
    ref = Reference(reg.host, "lib/hello", "latest")
    fetched = client.fetch(ref, Platform("linux", "amd64"))
    assert fetched.source == "registry"
    assert fetched.config["os"] == "linux"
    assert [l.digest for l in fetched.layers] == [digest_of(x) for x in layers]
    for l in fetched.layers:
        assert l.blob_path.read_bytes() == reg.blobs[l.digest]
        assert l.blob_path.parent == home.blobs
    assert not list(home.blobs.glob("*.part"))


def test_token_challenge_is_followed_once_per_repo(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    client = RegistryClient(home, scheme="http")
    client.fetch(Reference(reg.host, "lib/hello", "latest"), Platform())
    token_calls = [r for r in reg.requests if r[0] == "api" and r[1].startswith("/token")]
    assert len(token_calls) == 1


def test_blob_redirect_drops_authorization(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/hello", "latest"), Platform())
    blob_reqs = [r for r in reg.requests if r[0] == "blob"]
    assert blob_reqs, "blob server was never hit"
    assert all("Authorization" not in r[2] for r in blob_reqs)


def test_multi_arch_index_selects_platform(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    client = RegistryClient(home, scheme="http")
    fetched = client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "amd64"))
    assert fetched.config["architecture"] == "amd64"
    with pytest.raises(PullError, match="no manifest for linux/riscv64"):
        client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "riscv64"))


def test_digest_mismatch_is_rejected(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/bad", "latest", config_for(layers), layers)
    real = digest_of(layers[0])
    reg.blobs[real] = b"tampered"
    with pytest.raises(PullError, match="digest mismatch"):
        RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/bad", "latest"), Platform())
    assert not list(home.blobs.glob("*.part"))


def test_missing_manifest_is_pull_error(home, reg):
    with pytest.raises(PullError, match="404"):
        RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/none", "latest"), Platform())


def test_select_platform_variant_rules():
    manifests = [
        {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
    ]
    assert select_platform(manifests, Platform("linux", "arm64")) == "sha256:" + "a" * 64
    assert select_platform(manifests, Platform("linux", "arm64", "v8")) == "sha256:" + "a" * 64
    assert select_platform(manifests, Platform("linux", "amd64")) == "sha256:" + "b" * 64
