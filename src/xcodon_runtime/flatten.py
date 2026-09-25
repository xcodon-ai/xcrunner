"""Build one rootfs from ordered layer directories, applying OCI whiteouts."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path
from typing import Sequence

# OCI whiteout names, shared with layerdiff (which writes them).
WHITEOUT_PREFIX = ".wh."
OPAQUE = ".wh..wh..opq"


def build_rootfs(layer_dirs: Sequence[Path], dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for layer in layer_dirs:
        _apply_dir(Path(layer), dest)


def _remove(path: Path) -> None:
    """Remove a file, symlink, or directory tree. Missing is fine."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _apply_dir(src: Path, dst: Path) -> None:
    entries = list(os.scandir(src))
    names = {e.name for e in entries}

    if OPAQUE in names:
        # Materialize the listing: removing children while scanning the same
        # directory can skip entries.
        for child in list(os.scandir(dst)):
            _remove(Path(child.path))

    for e in entries:
        if e.name != OPAQUE and e.name.startswith(WHITEOUT_PREFIX):
            _remove(dst / e.name[len(WHITEOUT_PREFIX) :])

    for e in entries:
        if e.name.startswith(WHITEOUT_PREFIX):
            continue
        target = dst / e.name
        if e.is_symlink():
            _remove(target)
            os.symlink(os.readlink(e.path), target)
        elif e.is_dir(follow_symlinks=False):
            try:
                existing = os.lstat(target)
                if not stat.S_ISDIR(existing.st_mode):
                    _remove(target)
                    target.mkdir()
            except FileNotFoundError:
                target.mkdir()
            os.chmod(target, stat.S_IMODE(e.stat(follow_symlinks=False).st_mode) | 0o700)
            _apply_dir(Path(e.path), target)
        else:
            _remove(target)
            try:
                os.link(e.path, target)
            except OSError as err:
                if err.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
                    shutil.copy2(e.path, target, follow_symlinks=False)
                else:
                    raise
