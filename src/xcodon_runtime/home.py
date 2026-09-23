"""The runtime home directory: layout, locks, atomic directory builds, and the refs file."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def default_home() -> Path:
    env = os.environ.get("XCODON_RUNTIME_HOME")
    if env:
        return Path(env)
    return Path.home() / ".xcodon" / "runtime"


class RuntimeHome:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_home()
        self.path = self.path.expanduser().resolve()
        self.blobs = self.path / "blobs" / "sha256"
        self.layers = self.path / "layers"
        self.images = self.path / "images"
        self.containers = self.path / "containers"
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
        fd = os.open(self.locks / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

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
        """Remove half-built ``*.tmp`` directories and ``*.part`` blobs."""
        removed: list[Path] = []
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
