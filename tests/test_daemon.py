import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from xcodon_runtime.daemon import DaemonSource, load_oci_layout_tar
from xcodon_runtime.errors import PullError
from xcodon_runtime.reference import Platform, Reference


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def layer_tar(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        i = tarfile.TarInfo(name)
        i.size = len(content)
        t.addfile(i, io.BytesIO(content))
    return buf.getvalue()


def oci_layout_tar(nested_index: bool = True) -> tuple[bytes, dict]:
    """Build what `docker save` emits: index.json, oci-layout, manifest.json, blobs/sha256/*."""
    layers = [layer_tar("a", b"A"), layer_tar("b", b"B")]
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Cmd": ["sh"], "Env": ["PATH=/bin"]},
        "rootfs": {"type": "layers", "diff_ids": [f"sha256:{sha(l)}" for l in layers]},
    }
    cbytes = json.dumps(config).encode()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": f"sha256:{sha(cbytes)}", "size": len(cbytes)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": f"sha256:{sha(l)}", "size": len(l)} for l in layers],
    }
    mbytes = json.dumps(manifest).encode()
    blobs = {sha(cbytes): cbytes, sha(mbytes): mbytes}
    for l in layers:
        blobs[sha(l)] = l
    entry = {"mediaType": manifest["mediaType"], "digest": f"sha256:{sha(mbytes)}", "size": len(mbytes)}
    if nested_index:
        inner = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {**entry, "platform": {"os": "linux", "architecture": "amd64"}},
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "f" * 64, "size": 1, "platform": {"os": "unknown", "architecture": "unknown"}},
            ],
        }
        ibytes = json.dumps(inner).encode()
        blobs[sha(ibytes)] = ibytes
        entry = {"mediaType": inner["mediaType"], "digest": f"sha256:{sha(ibytes)}", "size": len(ibytes)}
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [entry]}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        def add(name, data):
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
        add("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add("manifest.json", b"[]")
        for hexd, data in blobs.items():
            add(f"blobs/sha256/{hexd}", data)
        add("index.json", json.dumps(index).encode())
    return buf.getvalue(), config


@pytest.mark.parametrize("nested", [True, False])
def test_load_oci_layout(home, nested):
    data, config = oci_layout_tar(nested_index=nested)
    fetched = load_oci_layout_tar(io.BytesIO(data), home, Platform("linux", "amd64"))
    assert fetched.source == "daemon"
    assert fetched.config == config
    assert len(fetched.layers) == 2
    assert fetched.layers[0].media_type == "application/vnd.oci.image.layer.v1.tar"
    for l in fetched.layers:
        assert l.blob_path.exists()
        assert sha(l.blob_path.read_bytes()) == l.digest.split(":")[1]


def test_load_rejects_corrupt_blob(home):
    data, _ = oci_layout_tar()
    # flip a byte inside the first blob's content
    buf = io.BytesIO(data)
    out = io.BytesIO()
    with tarfile.open(fileobj=buf) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src:
            content = src.extractfile(m).read() if m.isfile() else None
            if m.name.startswith("blobs/") and content and content.startswith(b"{") is False and m.size > 100:
                content = b"X" + content[1:]
            if content is not None:
                m.size = len(content)
                dst.addfile(m, io.BytesIO(content))
            else:
                dst.addfile(m)
    with pytest.raises(PullError, match="digest mismatch"):
        load_oci_layout_tar(io.BytesIO(out.getvalue()), home, Platform())


def test_daemon_source_uses_fake_docker(home, tmp_path, monkeypatch):
    data, config = oci_layout_tar()
    tarfile_path = tmp_path / "img.tar"
    tarfile_path.write_bytes(data)
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  version) echo 29.0.0; exit 0;;\n"
        "  image) [ \"$3\" = docker.io/library/present:latest ] && exit 0 || exit 1;;\n"
        f"  save) cat {tarfile_path}; exit 0;;\n"
        "esac\nexit 2\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    src = DaemonSource(home, docker=str(fake))
    assert src.available()
    assert src.has_image(Reference("docker.io", "library/present", "latest"))
    assert not src.has_image(Reference("docker.io", "library/absent", "latest"))
    fetched = src.fetch(Reference("docker.io", "library/present", "latest"), Platform())
    assert fetched.config == config


def test_daemon_source_unavailable_when_missing(home):
    assert not DaemonSource(home, docker="/nonexistent/docker").available()
