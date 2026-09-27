"""The persistent env folder: a host directory holding a container's writable layer.

Layout is ``<env_dir>/<image_id>/`` with ``upper/`` and ``work/`` (ns engine)
or ``rootfs/`` (proot engine), an ``image.json`` for humans, a ``.lock``
the running ns keeper holds so two overlays never share one upper directory,
and a ``holder`` file naming the container that took the lock last.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from xcodon_runtime.errors import XcodonError

if TYPE_CHECKING:
    from xcodon_runtime.containers import Container

log = logging.getLogger(__name__)
ENV_LOCK_NAME = ".lock"
ENV_INFO_NAME = "image.json"
ENV_HOLDER_NAME = "holder"
WAIT_LOG_EVERY = 60  # seconds between two "waiting for env layer" warnings
# Keeps env folder layers on another disk: the VM disk on macOS, or a node-local
# disk on a cluster. The record and the conda root stay in the env folder.
ENV_LAYER_DIR_ENV = "XRUNNER_ENV_LAYER_DIR"
ENV_LAYER_SOURCE_NAME = "source"


def env_layer_root(env_dir: str | os.PathLike) -> Path:
    """The folder holding ``<image_id>/`` layers for ``env_dir``. See spec section 16.5.

    That is ``env_dir`` itself unless ``XRUNNER_ENV_LAYER_DIR`` is set. Then it is
    ``<layer dir>/<key>``, where the key is the first 16 hex digits of the
    SHA-256 of the resolved env folder path.
    """
    base = os.environ.get(ENV_LAYER_DIR_ENV)
    if not base:
        return Path(env_dir)
    root = Path(base).expanduser().resolve()
    if "," in str(root) or ":" in str(root):
        raise XcodonError(f"{ENV_LAYER_DIR_ENV}={base} contains ',' or ':'; overlayfs mount options "
                          "separate their fields with those characters and cannot quote them")
    key = hashlib.sha256(str(Path(env_dir).resolve()).encode()).hexdigest()[:16]
    return root / key


def env_layer_dir(env_dir: str | os.PathLike, image_id: str) -> Path:
    return env_layer_root(env_dir) / image_id


def _write_once(path: Path, text: str) -> bool:
    """Create ``path`` with ``text`` unless it exists. True when this call wrote it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return True


def prepare_env_layer(container: "Container") -> Path:
    """Create the layer directory for this container's image and record what it is for.

    The record is written once, the first time any container uses this layer.
    Two starters can race here (the proot engine allows two containers of the
    same image to share a layer), so the create is exclusive: the loser of
    the race just finds the file already there.
    """
    assert container.env_dir is not None
    root = env_layer_root(container.env_dir)
    layer = root / container.image_id
    layer.mkdir(parents=True, exist_ok=True)
    if root != Path(container.env_dir):
        # For people browsing the layer dir: which env folder this key belongs to.
        _write_once(root / ENV_LAYER_SOURCE_NAME, f"{Path(container.env_dir).resolve()}\n")
    if _write_once(layer / ENV_INFO_NAME, json.dumps({
        "image_ref": container.image_ref,
        "image_id": container.image_id,
        "engine": container.engine,
        "first_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2)):
        log.info("new env layer for %s (%s) in %s",
                 container.image_ref, container.image_id[:12], container.env_dir)
    return layer


def _read_holder(layer_dir: Path) -> str | None:
    try:
        return (layer_dir / ENV_HOLDER_NAME).read_text().strip() or None
    except OSError:
        return None


def acquire_env_lock(layer_dir: Path, holder: str) -> int:
    """Take the layer's exclusive lock and return the open descriptor that holds it.

    The caller passes the descriptor to the keeper, which inherits the lock and
    holds it until it exits. ``holder`` is the caller's container short id; it
    is written to the ``holder`` file once the lock is taken. While another
    container has the layer, warn every minute and keep waiting. There is no
    timeout.
    """
    fd = os.open(layer_dir / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        waited = 0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if waited % WAIT_LOG_EVERY == 0:
                    other = _read_holder(layer_dir)
                    if other:
                        log.warning("waiting for env layer %s: container %s holds it (xrunner stop %s frees it)",
                                    layer_dir, other, other)
                    else:
                        log.warning("waiting for env layer %s: another container holds it (xrunner stop frees it)",
                                    layer_dir)
                time.sleep(1)
                waited += 1
        (layer_dir / ENV_HOLDER_NAME).write_text(f"{holder}\n")
    except BaseException:
        os.close(fd)
        raise
    return fd
