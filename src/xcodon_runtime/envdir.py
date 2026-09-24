"""The persistent env folder: a host directory holding a container's writable layer.

Layout is ``<env_dir>/<image_id>/`` with ``upper/`` and ``work/`` (ns engine)
or ``rootfs/`` (proot engine), an ``image.json`` for humans, and a ``.lock``
the running ns keeper holds so two overlays never share one upper directory.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xcodon_runtime.containers import Container

log = logging.getLogger(__name__)
ENV_LOCK_NAME = ".lock"
ENV_INFO_NAME = "image.json"


def env_layer_dir(env_dir: str, image_id: str) -> Path:
    return Path(env_dir) / image_id


def prepare_env_layer(container: "Container") -> Path:
    """Create the layer directory for this container's image and record what it is for."""
    assert container.env_dir is not None
    layer = env_layer_dir(container.env_dir, container.image_id)
    layer.mkdir(parents=True, exist_ok=True)
    info = layer / ENV_INFO_NAME
    if not info.exists():
        info.write_text(json.dumps({
            "image_ref": container.image_ref,
            "image_id": container.image_id,
            "engine": container.engine,
            "first_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
    return layer


def acquire_env_lock(layer_dir: Path, what: str) -> int:
    """Take the layer's exclusive lock and return the open descriptor that holds it.

    The caller passes the descriptor to the keeper, which inherits the lock and
    holds it until it exits. If another container has the layer, log once and wait.
    """
    fd = os.open(layer_dir / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("%s: waiting for env layer %s (another container of this image is running)", what, layer_dir)
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd
