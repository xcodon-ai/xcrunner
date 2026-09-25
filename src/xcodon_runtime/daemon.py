"""Reuse images from a local Docker daemon through `docker save` (OCI layout)."""

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

log = logging.getLogger(__name__)
CHUNK = 1 << 20

# Written as the first line of the `docker` shim `xrunner shim install` creates
# (see cli.py). A resolved `docker` executable that contains this marker is our
# own shim, not a real docker daemon client, and must never be treated as one:
# otherwise xrunner would call itself for `docker version` / `docker save`.
SHIM_MARKER = "# docker shim installed by xrunner"


def is_shim(path: str) -> bool:
    """True when ``path`` is a file that carries the xrunner shim marker near its start."""
    try:
        with open(path, "rb") as f:
            return SHIM_MARKER.encode() in f.read(512)
    except OSError:
        return False


def resolve_docker(name: str) -> str | None:
    """Find an executable docker, like shutil.which, but skip an xrunner-installed shim."""
    candidates = [name] if os.path.isabs(name) else [
        os.path.join(d, name) for d in os.environ.get("PATH", "").split(os.pathsep) if d
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and not is_shim(candidate):
            return candidate
    return None


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


def load_oci_layout_tar(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> FetchedImage:
    """Read a `docker save` archive from a stream. Blobs land in the home blob store."""
    index: dict | None = None
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.name in ("index.json", "./index.json"):
                f = tar.extractfile(member)
                if f:
                    try:
                        index = json.load(f)
                    except ValueError as e:
                        raise PullError("docker save output has malformed JSON in index.json") from e
            elif member.isfile() and "blobs/sha256/" in member.name:
                _store_blob(tar, member, home)
    if index is None:
        raise PullError("docker save output has no index.json; is this Docker 25 or newer?")

    manifests = index.get("manifests") or []
    if not manifests:
        raise PullError("docker save index.json lists no manifests")
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
    return FetchedImage(config_digest, config, layers, source="daemon")


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

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        exe = self._exe()
        if not exe:
            raise PullError(f"no usable docker executable found for {self.docker!r}")
        log.info("exporting %s from the local docker daemon", ref.name)
        # Read the daemon's id before the export: if the tag moves while
        # `docker save` runs, the stored id is the older one and the next
        # resolve imports the image again, which is the safe direction.
        daemon_id = self.image_id(ref)
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
            return fetched
        finally:
            err.close()
