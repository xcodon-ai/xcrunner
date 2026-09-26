"""The runtime home directory: layout, locks, atomic directory builds, and the refs file."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from xcodon_runtime.errors import XcodonError

# Scratch directories made at the home root (see ``RuntimeHome.prune_leftovers``):
# ``Runtime.commit`` uses commit-, and the build's COPY and WORKDIR steps use
# copy- and workdir-.
SCRATCH_PREFIXES = ("commit-", "copy-", "workdir-")

# Where container folders live when not ``<home>/containers``. On a cluster the
# home (images and layers) can sit on shared storage while this points at a
# node-local disk, where overlay upper layers work. See spec section 15.
CONTAINER_DIR_ENV = "XRUNNER_CONTAINER_DIR"
# Container locks live beside the containers they guard, not in the home.
CONTAINER_LOCKS_NAME = ".locks"


def default_home() -> Path:
    env = os.environ.get("XCODON_RUNTIME_HOME")
    if env:
        return Path(env)
    return Path.home() / ".xcodon" / "runtime"


def _refuse_overlay_separators(path: Path, what: str, setting: str) -> None:
    if "," in str(path) or ":" in str(path):
        raise XcodonError(
            f"{what} {path} contains ',' or ':'; overlayfs mount options "
            "separate their fields with those characters and cannot quote them. "
            f"Set {setting} to a path without them."
        )


@contextmanager
def flock_file(path: Path, shared: bool = False) -> Iterator[None]:
    """Hold ``flock`` on ``path`` (created if missing, along with its folder) for the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def container_lock_in(containers_dir: Path, name: str):
    """The lock ``name`` for the containers under ``containers_dir``."""
    return flock_file(Path(containers_dir) / CONTAINER_LOCKS_NAME / f"{name}.lock")


class RuntimeHome:
    def __init__(self, path: Path | str | None = None, containers: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_home()
        self.path = self.path.expanduser().resolve()
        _refuse_overlay_separators(self.path, "runtime home", "XCODON_RUNTIME_HOME")
        containers = containers or os.environ.get(CONTAINER_DIR_ENV) or None
        if containers:
            self.containers = Path(containers).expanduser().resolve()
            _refuse_overlay_separators(self.containers, "container dir", CONTAINER_DIR_ENV)
        else:
            self.containers = self.path / "containers"
        self.blobs = self.path / "blobs" / "sha256"
        self.layers = self.path / "layers"
        self.images = self.path / "images"
        self.locks = self.path / "locks"
        self.refs_file = self.path / "refs.json"
        for d in (self.blobs, self.layers, self.images, self.containers, self.locks):
            d.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, name: str, shared: bool = False) -> Iterator[None]:
        """Advisory lock shared by every process using this home.

        Exclusive (the default) by name; pass ``shared=True`` for a shared
        (reader) lock that can be held by multiple holders at once but waits
        out any exclusive (writer) holder of the same name, and vice versa.
        """
        with flock_file(self.locks / f"{name}.lock", shared):
            yield

    def container_lock(self, name: str):
        """A lock scoped to this home's container dir, which may be on another disk."""
        return container_lock_in(self.containers, name)

    @contextmanager
    def atomic_dir(self, final: Path) -> Iterator[Path]:
        """Build into ``<final>.tmp`` and rename on success. Removes the temp dir on error."""
        tmp = final.with_name(final.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            yield tmp
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        os.rename(tmp, final)

    def read_refs(self) -> dict[str, str]:
        try:
            return json.loads(self.refs_file.read_text())
        except FileNotFoundError:
            return {}

    def write_refs(self, refs: dict[str, str]) -> None:
        tmp = self.refs_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(refs, indent=2, sort_keys=True))
        os.replace(tmp, self.refs_file)

    def prune_leftovers(self) -> list[Path]:
        """Remove half-built ``*.tmp`` directories, ``*.part`` blobs, and dead scratch dirs.

        The scratch dirs are the ``commit-*``, ``copy-*`` and ``workdir-*``
        directories that a commit or a build step makes at the home root and
        removes when done; one killed midway leaves its dir behind. The
        caller must hold ``store`` exclusive: every commit and build holds it
        shared while its scratch dir exists, so none found here is live.
        """
        from xcodon_runtime.containers import _rmtree_tolerant

        removed: list[Path] = []
        for prefix in SCRATCH_PREFIXES:
            for p in self.path.glob(prefix + "*"):
                if not p.is_dir() or p.is_symlink():
                    continue
                _rmtree_tolerant(p)
                if not os.path.lexists(p):
                    removed.append(p)
        for parent in (self.layers, self.images, self.containers):
            for p in parent.glob("*.tmp"):
                shutil.rmtree(p, ignore_errors=True)
                if not os.path.lexists(p):
                    removed.append(p)
        for p in self.blobs.glob("*.part"):
            p.unlink(missing_ok=True)
            if not p.exists():
                removed.append(p)
        return removed
