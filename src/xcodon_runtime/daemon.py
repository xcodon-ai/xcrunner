"""Read `docker save` archives (OCI layout): from a local Docker daemon, or from a file.

The file case is `xcrunner load`: an image built on a machine with Docker (the BixBench3
sandbox, 5.8 GB, in no registry) carried as a file to a host with none, such as an HPC node.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tarfile
import tempfile
from typing import BinaryIO

from xcodon_runtime.errors import PullError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import INDEX_TYPES, FetchedImage, FetchedLayer, select_platform
from xcodon_runtime.shim import resolve_real

log = logging.getLogger(__name__)
CHUNK = 1 << 20

# Finding a real docker, skipping xcrunner's own shims, lives in shim.py (shared
# with the conda shim). Kept public here under its original name.
resolve_docker = resolve_real


def _store_blob(tar: tarfile.TarFile, member: tarfile.TarInfo, home: RuntimeHome) -> None:
    hexdigest = member.name.rsplit("/", 1)[-1]
    final = home.blobs / hexdigest
    if final.exists():
        return
    part = final.with_name(hexdigest + ".part")
    src = tar.extractfile(member)
    if src is None:
        return
    h = hashlib.sha256()
    with open(part, "wb") as out:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != hexdigest:
        part.unlink(missing_ok=True)
        raise PullError(f"digest mismatch for blob {hexdigest[:12]} in docker save output")
    os.replace(part, final)


def _read_json_blob(home: RuntimeHome, digest: str) -> dict:
    path = home.blobs / digest.split(":", 1)[1]
    if not path.exists():
        raise PullError(f"docker save output references missing blob {digest}")
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        raise PullError(f"docker save output has malformed JSON in blob {digest}") from e


_NAME_ANNOTATION = "io.containerd.image.name"


def _archive_names(manifests: list[dict], legacy: list) -> list[str]:
    """The image names an archive records, in order, each once.

    Docker 25+ with the containerd store names the image in index.json's
    ``io.containerd.image.name`` annotation; the classic store, and older layouts,
    in manifest.json's ``RepoTags``.
    """
    names = [m.get("annotations", {}).get(_NAME_ANNOTATION) for m in manifests]
    for entry in legacy if isinstance(legacy, list) else []:
        if isinstance(entry, dict):
            names.extend(entry.get("RepoTags") or [])
    return list(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))


def read_image_archive(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> tuple[FetchedImage, list[str]]:
    """Read a `docker save` archive (plain or compressed) from a stream.

    Returns the image for ``platform`` and the names the archive records for it.
    Blobs land in the home blob store. An archive of several different images is
    refused: which one a name belongs to is not reliable across docker versions.
    """
    index: dict | None = None
    legacy: list = []
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if member.name in ("index.json", "./index.json"):
                f = tar.extractfile(member)
                if f:
                    try:
                        index = json.load(f)
                    except ValueError as e:
                        raise PullError("docker save output has malformed JSON in index.json") from e
            elif member.name in ("manifest.json", "./manifest.json"):
                f = tar.extractfile(member)
                if f:
                    try:
                        legacy = json.load(f)
                    except ValueError:
                        legacy = []  # only names come from it; index.json is the authority
            elif member.isfile() and "blobs/sha256/" in member.name:
                _store_blob(tar, member, home)
    if index is None:
        raise PullError("docker save output has no index.json; is this Docker 25 or newer?")

    manifests = index.get("manifests") or []
    if not manifests:
        raise PullError("docker save index.json lists no manifests")
    named = {m.get("annotations", {}).get(_NAME_ANNOTATION) for m in manifests} - {None}
    if len({m.get("digest") for m in manifests}) > 1 and len(named) > 1:
        raise PullError(f"the archive holds {len(named)} images ({', '.join(sorted(named))}); "
                        "save one image per file")
    if len(manifests) == 1:
        desc = manifests[0]
    else:
        desc = {"digest": select_platform(manifests, platform)}
    doc = _read_json_blob(home, desc["digest"])
    if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
        doc = _read_json_blob(home, select_platform(doc["manifests"], platform))
    if "config" not in doc or "layers" not in doc:
        raise PullError("docker save output has no usable image manifest")

    config_digest = doc["config"]["digest"]
    config = _read_json_blob(home, config_digest)
    layers = [
        FetchedLayer(
            l["digest"],
            l.get("mediaType", "application/vnd.oci.image.layer.v1.tar"),
            int(l.get("size", 0)),
            home.blobs / l["digest"].split(":", 1)[1],
        )
        for l in doc["layers"]
    ]
    for l in layers:
        if not l.blob_path.exists():
            raise PullError(f"docker save output is missing layer {l.digest}")
    return FetchedImage(config_digest, config, layers, source="daemon"), _archive_names(manifests, legacy)


def load_oci_layout_tar(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> FetchedImage:
    """Read a `docker save` archive from a stream. Blobs land in the home blob store."""
    return read_image_archive(stream, home, platform)[0]


class DaemonSource:
    name = "daemon"

    def __init__(self, home: RuntimeHome, docker: str = "docker") -> None:
        self.home = home
        self.docker = docker

    def _exe(self) -> str | None:
        """The resolved docker executable, or None. Same resolution in every method here,
        so a shim on PATH is never mistaken for the real thing in one call but not another."""
        return resolve_docker(self.docker)

    def available(self) -> bool:
        exe = self._exe()
        if not exe:
            return False
        try:
            r = subprocess.run([exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def has_image(self, ref: Reference) -> bool:
        exe = self._exe()
        if not exe:
            return False
        try:
            r = subprocess.run([exe, "image", "inspect", ref.name], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def image_id(self, ref: Reference) -> str | None:
        """The daemon's image id for a tag (``sha256:<hex>``), or None if it has no such tag."""
        exe = self._exe()
        if not exe:
            return None
        try:
            r = subprocess.run([exe, "image", "inspect", "--format", "{{.Id}}", ref.name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = r.stdout.strip()
        return out if r.returncode == 0 and out.startswith("sha256:") else None

    def repo_digests(self, ref: Reference) -> list[str]:
        """docker's RepoDigests for a tag: registry copies of this image. Empty when none or unknown."""
        exe = self._exe()
        if not exe:
            return []
        try:
            r = subprocess.run([exe, "image", "inspect", "--format", "{{json .RepoDigests}}", ref.name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        try:
            value = json.loads(r.stdout.strip() or "null")
        except ValueError:
            return []
        return sorted({v for v in value or [] if isinstance(v, str) and "@sha256:" in v})

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        exe = self._exe()
        if not exe:
            raise PullError(f"no usable docker executable found for {self.docker!r}")
        log.info("exporting %s from the local docker daemon", ref.name)
        # Read the daemon's id before the export: if the tag moves while
        # `docker save` runs, the stored id is the older one and the next
        # resolve imports the image again, which is the safe direction.
        daemon_id = self.image_id(ref)
        repo_digests = self.repo_digests(ref)
        err = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen([exe, "save", ref.name], stdout=subprocess.PIPE, stderr=err)
            assert proc.stdout is not None
            fetch_error: Exception | None = None
            try:
                fetched = load_oci_layout_tar(proc.stdout, self.home, platform)
            except Exception as e:
                fetch_error = e
                fetched = None
            finally:
                proc.stdout.close()
                proc.wait()
            if proc.returncode != 0:
                err.seek(0)
                errmsg = err.read().decode(errors='replace').strip()
                if not errmsg and fetch_error is not None:
                    # docker said nothing; the reader's own complaint is the news.
                    raise fetch_error
                raise PullError(f"docker save {ref.name} failed: {errmsg}")
            if fetch_error is not None:
                raise fetch_error
            fetched.daemon_id = daemon_id
            fetched.repo_digests = repo_digests
            return fetched
        finally:
            err.close()
