"""The persistent env folder: a host directory holding a container's writable layer.

Layout is ``<env_dir>/<image_id>/`` with ``upper/`` and ``work/`` (ns engine)
or ``rootfs/`` (proot engine), an ``image.json`` for humans, a ``.lock``
the running ns keeper holds so two overlays never share one upper directory,
and a ``holder`` file naming the container that took the lock last.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xcodon_runtime.containers import Container

log = logging.getLogger(__name__)
ENV_LOCK_NAME = ".lock"
ENV_INFO_NAME = "image.json"
ENV_HOLDER_NAME = "holder"
WAIT_LOG_EVERY = 60  # seconds between two "waiting for env layer" warnings


def env_layer_dir(env_dir: str, image_id: str) -> Path:
    return Path(env_dir) / image_id


def prepare_env_layer(container: "Container") -> Path:
    """Create the layer directory for this container's image and record what it is for.

    The record is written once, the first time any container uses this layer.
    Two starters can race here (the proot engine allows two containers of the
    same image to share a layer), so the create is exclusive: the loser of
    the race just finds the file already there.
    """
    assert container.env_dir is not None
    layer = env_layer_dir(container.env_dir, container.image_id)
    layer.mkdir(parents=True, exist_ok=True)
    info = layer / ENV_INFO_NAME
    try:
        fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({
                "image_ref": container.image_ref,
                "image_id": container.image_id,
                "engine": container.engine,
                "first_used": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, indent=2))
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
