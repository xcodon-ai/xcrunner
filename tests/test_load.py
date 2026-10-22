"""`xcrunner load`: import a `docker save` file on a host without docker."""

from __future__ import annotations

import gzip
import io
import json

import pytest

from tests.test_daemon import oci_layout_tar
from xcodon_runtime import cli
from xcodon_runtime.daemon import read_image_archive
from xcodon_runtime.errors import PullError
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform

AMD64 = Platform("linux", "amd64")


def _rewrite(data: bytes, *, index_name: str | None = None, repo_tags: list[str] | None = None,
             extra_index_entry: dict | None = None) -> bytes:
    """``oci_layout_tar`` with the name docker records: an index annotation, manifest.json RepoTags, or none."""
    import tarfile

    src = tarfile.open(fileobj=io.BytesIO(data))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        for m in src.getmembers():
            body = src.extractfile(m).read()
            if m.name == "index.json":
                index = json.loads(body)
                if index_name:
                    index["manifests"][0]["annotations"] = {"io.containerd.image.name": index_name}
                if extra_index_entry:
                    index["manifests"].append(extra_index_entry)
                body = json.dumps(index).encode()
            elif m.name == "manifest.json" and repo_tags is not None:
                body = json.dumps([{"RepoTags": repo_tags}]).encode()
            info = tarfile.TarInfo(m.name)
            info.size = len(body)
            t.addfile(info, io.BytesIO(body))
    return out.getvalue()


def test_names_come_from_the_index_annotation_or_repo_tags(home):
    data, _ = oci_layout_tar(nested_index=False)
    _, names = read_image_archive(io.BytesIO(_rewrite(data, index_name="docker.io/library/sbx:v1")), home, AMD64)
    assert names == ["docker.io/library/sbx:v1"]
    _, names = read_image_archive(io.BytesIO(_rewrite(data, repo_tags=["sbx:v1"])), home, AMD64)
    assert names == ["sbx:v1"]


def test_an_archive_of_several_images_is_refused(home):
    data, _ = oci_layout_tar(nested_index=False)
    other = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "e" * 64,
             "size": 1, "annotations": {"io.containerd.image.name": "docker.io/library/other:1"}}
    data = _rewrite(data, index_name="docker.io/library/sbx:v1", extra_index_entry=other)
    with pytest.raises(PullError, match="holds 2 images"):
        read_image_archive(io.BytesIO(data), home, AMD64)


def test_load_imports_the_image_under_its_recorded_name(home):
    data, config = oci_layout_tar(nested_index=True)
    store = ImageStore(home)
    img, refs = store.load_archive(io.BytesIO(_rewrite(data, index_name="docker.io/library/sbx:v1")))
    assert refs == ["docker.io/library/sbx:v1"]
    assert store.get("sbx:v1").id == img.id
    assert (img.rootfs / "a").read_bytes() == b"A" and (img.rootfs / "b").read_bytes() == b"B"
    assert store.manifest(img)["source"] == "archive"
    left = {p.name for p in home.blobs.iterdir()}
    layer_digests = {l["digest"].split(":", 1)[1] for l in store.manifest(img)["layers"]}
    assert not left & layer_digests, "the layer blobs are removed once the layers are extracted"


def test_load_reads_a_gzipped_archive_and_adds_a_tag(home):
    data, _ = oci_layout_tar(nested_index=False)
    img, refs = ImageStore(home).load_archive(io.BytesIO(gzip.compress(data)), tag="bixbench3-sandbox:test")
    assert refs == ["docker.io/library/bixbench3-sandbox:test"]


def test_an_unnamed_archive_needs_a_tag(home):
    data, _ = oci_layout_tar(nested_index=False)
    with pytest.raises(PullError, match="--tag"):
        ImageStore(home).load_archive(io.BytesIO(data))


def test_cli_load_from_a_file_and_through_the_docker_verb(home, tmp_path, capsys):
    data, _ = oci_layout_tar(nested_index=False)
    archive = tmp_path / "sbx.tar"
    archive.write_bytes(_rewrite(data, repo_tags=["sbx:v1"]))
    assert cli.main(["load", "-i", str(archive)]) == 0
    assert "Loaded image: docker.io/library/sbx:v1" in capsys.readouterr().out
    assert cli.main(["docker", "load", "-q", "-i", str(archive), "-t", "sbx:v2"]) == 0
    assert "docker.io/library/sbx:v2" in capsys.readouterr().out
    assert cli.translate_docker_argv(["image", "load", "-i", "x.tar"])[0] == "load"
    assert cli.main(["load", "-i", str(tmp_path / "missing.tar")]) != 0
