"""Reuse images from a local Docker daemon through `docker save` (OCI layout)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import BinaryIO

from xcodon_runtime.errors import PullError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import INDEX_TYPES, FetchedImage, FetchedLayer, select_platform

log = logging.getLogger(__name__)
CHUNK = 1 << 20


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
    return json.loads(path.read_text())


def load_oci_layout_tar(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> FetchedImage:
    """Read a `docker save` archive from a stream. Blobs land in the home blob store."""
    index: dict | None = None
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.name in ("index.json", "./index.json"):
                f = tar.extractfile(member)
                index = json.load(f) if f else None
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

    def available(self) -> bool:
        exe = shutil.which(self.docker) if not os.path.isabs(self.docker) else self.docker
        if not exe or not os.access(exe, os.X_OK):
            return False
        try:
            r = subprocess.run([exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def has_image(self, ref: Reference) -> bool:
        r = subprocess.run([self.docker, "image", "inspect", ref.name], capture_output=True, timeout=30)
        return r.returncode == 0

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        log.info("exporting %s from the local docker daemon", ref.name)
        proc = subprocess.Popen([self.docker, "save", ref.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            fetched = load_oci_layout_tar(proc.stdout, self.home, platform)
        finally:
            proc.stdout.close()
            _, err = proc.communicate()
        if proc.returncode != 0:
            raise PullError(f"docker save {ref.name} failed: {err.decode(errors='replace').strip()}")
        return fetched
