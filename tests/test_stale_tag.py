"""The stale-tag check for images imported from a local docker daemon (spec 11.5).

A fake ``docker`` script plays the daemon. Its ``.Id`` is a manifest-style
digest that differs from the image's config digest, as with docker's
containerd image store.
"""

import json
import logging
import stat
import subprocess

import pytest

from tests.test_daemon import oci_layout_tar
from xcodon_runtime.api import Runtime
from xcodon_runtime.daemon import DaemonSource

REF = "present:latest"
MANIFEST_ID = "sha256:" + "a" * 64


@pytest.fixture
def fake_daemon(tmp_path, monkeypatch):
    """A fake docker on PATH. Returns (runtime, id_file, save_log, fail_flag)."""
    data, _ = oci_layout_tar()
    tar = tmp_path / "img.tar"
    tar.write_bytes(data)
    id_file = tmp_path / "id"
    id_file.write_text(MANIFEST_ID + "\n")
    save_log = tmp_path / "saves"
    save_log.write_text("")
    fail_flag = tmp_path / "fail"
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  version) echo 29.0.0; exit 0;;\n"
        f'  image) if [ "$3" = "--format" ]; then cat {id_file}; fi; exit 0;;\n'
        f"  save) echo save >> {save_log}\n"
        f"        if [ -e {fail_flag} ]; then echo 'save broke' >&2; exit 3; fi\n"
        f"        cat {tar}; exit 0;;\n"
        "esac\nexit 2\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin")
    rt = Runtime(tmp_path / "home", engine="proot")
    # The daemon only: a registry fallback would reach the network.
    rt.images.sources = [DaemonSource(rt.home)]
    return rt, id_file, save_log, fail_flag


def _saves(save_log) -> int:
    return len(save_log.read_text().split())


def _manifest(img) -> dict:
    return json.loads((img.dir / "manifest.json").read_text())


def test_import_records_the_daemon_id(fake_daemon):
    rt, _, save_log, _ = fake_daemon
    img = rt.pull(REF)
    assert _manifest(img)["daemon_id"] == MANIFEST_ID
    assert MANIFEST_ID != f"sha256:{img.id}", "the fake daemon must use a manifest-style id"
    assert _saves(save_log) == 1


def test_manifest_style_daemon_id_does_not_reimport(fake_daemon):
    rt, _, save_log, _ = fake_daemon
    first = rt.pull(REF)
    for _ in range(3):
        assert rt.resolve_image(REF).id == first.id
    assert _saves(save_log) == 1, "an unchanged tag must never run docker save again"


def test_moved_daemon_tag_reimports(fake_daemon):
    rt, id_file, save_log, _ = fake_daemon
    rt.pull(REF)
    id_file.write_text("sha256:" + "b" * 64 + "\n")
    img = rt.resolve_image(REF)
    assert _saves(save_log) == 2
    assert _manifest(img)["daemon_id"] == "sha256:" + "b" * 64
    rt.resolve_image(REF)
    assert _saves(save_log) == 2, "the new id is recorded, so no third export"


def test_image_without_daemon_id_is_reimported_once(fake_daemon):
    rt, _, save_log, _ = fake_daemon
    img = rt.pull(REF)
    m = _manifest(img)
    del m["daemon_id"]
    (img.dir / "manifest.json").write_text(json.dumps(m))
    rt.resolve_image(REF)
    assert _saves(save_log) == 2
    assert _manifest(img)["daemon_id"] == MANIFEST_ID, "the re-import records the id"
    rt.resolve_image(REF)
    rt.resolve_image(REF)
    assert _saves(save_log) == 2


def test_failed_reimport_falls_back_to_the_stored_image(fake_daemon, caplog):
    rt, id_file, save_log, fail_flag = fake_daemon
    img = rt.pull(REF)
    id_file.write_text("sha256:" + "c" * 64 + "\n")
    fail_flag.touch()
    with caplog.at_level(logging.WARNING, logger="xcodon_runtime.api"):
        got = rt.resolve_image(REF)
    assert got.id == img.id
    assert _saves(save_log) == 2
    assert any("using the stored image" in r.getMessage() for r in caplog.records)


def test_pull_never_and_id_refs_skip_the_check(fake_daemon):
    rt, id_file, save_log, _ = fake_daemon
    img = rt.pull(REF)
    id_file.write_text("sha256:" + "e" * 64 + "\n")
    assert rt.resolve_image(REF, pull="never").id == img.id
    assert rt.resolve_image(img.id).id == img.id
    assert rt.resolve_image(img.id[:12]).id == img.id
    assert rt.resolve_image("sha256:" + img.id).id == img.id
    assert _saves(save_log) == 1, "neither pull='never' nor an image id asks the daemon"
    rt.resolve_image(REF)
    assert _saves(save_log) == 2, "a tag with pull='missing' still does"


@pytest.mark.docker
def test_real_daemon_image_is_exported_once(home, monkeypatch):
    ref = "busybox:latest"
    if subprocess.run(["docker", "image", "inspect", ref], capture_output=True).returncode != 0:
        pytest.skip(f"the docker daemon has no {ref}")
    rt = Runtime(home.path, engine="proot")
    rt.images.sources = [DaemonSource(rt.home)]
    calls = []
    real_fetch = DaemonSource.fetch

    def spy(self, reference, platform):
        calls.append(reference.name)
        return real_fetch(self, reference, platform)

    monkeypatch.setattr(DaemonSource, "fetch", spy)
    first = rt.resolve_image(ref)
    assert calls == ["docker.io/library/busybox:latest"]
    assert rt.resolve_image(ref).id == first.id
    assert rt.resolve_image(ref).id == first.id
    assert len(calls) == 1, "resolving an unchanged daemon tag must not run docker save again"
