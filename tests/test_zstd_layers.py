"""zstd-compressed layers work by default: stdlib compression.zstd on Python 3.14+, else zstandard."""

import builtins
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.fake_registry import FakeRegistry, digest_of
from tests.test_tarlayer import make_tar, ti
from xcodon_runtime.errors import UnsupportedLayer
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.registry import RegistryClient
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream

HAS_STDLIB_ZSTD = importlib.util.find_spec("compression") is not None and \
    importlib.util.find_spec("compression.zstd") is not None
HAS_ZSTANDARD = importlib.util.find_spec("zstandard") is not None


def zstd_compress(raw: bytes) -> bytes:
    if HAS_STDLIB_ZSTD:
        from compression import zstd

        return zstd.compress(raw)
    import zstandard

    return zstandard.ZstdCompressor().compress(raw)


def _block_imports(monkeypatch, *blocked: str) -> None:
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            raise ImportError(f"blocked {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _extract(blob: Path, dest: Path) -> None:
    with open_layer_stream(blob) as s:
        extract_layer(s, dest)


def test_a_default_install_can_read_zstd():
    assert HAS_STDLIB_ZSTD or HAS_ZSTANDARD, "zstandard is a dependency on Python before 3.14"


def test_a_zstd_layer_extracts(tmp_path):
    blob = tmp_path / "layer"
    blob.write_bytes(zstd_compress(make_tar([(ti("etc/hello"), b"from zstd")])))
    dest = tmp_path / "out"
    dest.mkdir()
    _extract(blob, dest)
    assert (dest / "etc" / "hello").read_bytes() == b"from zstd"


@pytest.mark.skipif(not HAS_STDLIB_ZSTD, reason="needs Python 3.14+ built with zstd")
def test_the_stdlib_is_used_when_present(tmp_path, monkeypatch):
    _block_imports(monkeypatch, "zstandard")
    blob = tmp_path / "layer"
    blob.write_bytes(zstd_compress(make_tar([(ti("f"), b"1")])))
    with open_layer_stream(blob) as s:
        from compression import zstd

        assert isinstance(s, zstd.ZstdFile)
    dest = tmp_path / "out"
    dest.mkdir()
    _extract(blob, dest)
    assert (dest / "f").read_bytes() == b"1"


@pytest.mark.skipif(not HAS_ZSTANDARD, reason="zstandard is not installed")
def test_zstandard_is_the_fallback(tmp_path, monkeypatch):
    import zstandard

    blob = tmp_path / "layer"
    blob.write_bytes(zstandard.ZstdCompressor().compress(make_tar([(ti("f"), b"2")])))
    _block_imports(monkeypatch, "compression")
    dest = tmp_path / "out"
    dest.mkdir()
    _extract(blob, dest)
    assert (dest / "f").read_bytes() == b"2"


def test_neither_gives_a_clear_error(tmp_path, monkeypatch):
    _block_imports(monkeypatch, "compression", "zstandard")
    (tmp_path / "z").write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 16)
    with pytest.raises(UnsupportedLayer, match=r"zstd-compressed.*pip install zstandard"):
        open_layer_stream(tmp_path / "z")


def test_zstandard_is_a_dependency_only_before_python_3_14():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["project"]
    assert 'zstandard>=0.21; python_version < "3.14"' in project["dependencies"]
    assert project["optional-dependencies"]["zstd"] == [], "the extra stays as an empty alias"


def test_a_zstd_image_pulls(home):
    raw = make_tar([(ti("bin/tool"), b"#!/bin/sh\necho zstd\n")])
    layer = zstd_compress(raw)
    config = {"architecture": "amd64", "os": "linux", "config": {"Cmd": ["/bin/tool"]},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(raw).hexdigest()]}}
    with FakeRegistry() as reg:
        reg.add_image("lib/zstd", "v1", config, [layer])
        manifest = json.loads(reg.manifests[("lib/zstd", "v1")][0])
        manifest["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+zstd"
        body = json.dumps(manifest).encode()
        reg.manifests[("lib/zstd", "v1")] = (body, manifest["mediaType"])
        reg.manifests[("lib/zstd", digest_of(body))] = (body, manifest["mediaType"])
        img = ImageStore(home, sources=[RegistryClient(home, scheme="http")]).pull(f"{reg.host}/lib/zstd:v1")
    assert (img.rootfs / "bin" / "tool").read_bytes() == b"#!/bin/sh\necho zstd\n"
