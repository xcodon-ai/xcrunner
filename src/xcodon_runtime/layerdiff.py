"""Turn a writable layer into an OCI layer directory, and hash layer directories.

The ns engine's overlay upper directory records a deleted file as a
character device 0:0 and a replaced directory with an ``overlay.opaque``
attribute. OCI layers record the same facts as ``.wh.<name>`` files and a
``.wh..wh..opq`` file. The proot engine has no upper directory, so its layer
is the difference between the rootfs copy and the image rootfs.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import stat
import tarfile
from pathlib import Path

WHITEOUT_PREFIX = ".wh."
OPAQUE = ".wh..wh..opq"
OPAQUE_XATTRS = ("user.overlay.opaque", "trusted.overlay.opaque")


def _is_whiteout(st: os.stat_result) -> bool:
    return stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == 0 and os.minor(st.st_rdev) == 0


def _is_opaque(path: Path) -> bool:
    for name in OPAQUE_XATTRS:
        try:
            if os.getxattr(path, name, follow_symlinks=False) == b"y":
                return True
        except OSError:
            continue
    return False


def _place_file(src: Path, dst: Path) -> None:
    """Hardlink a regular file into the layer, copying when linking is not possible."""
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
            shutil.copy2(src, dst, follow_symlinks=False)
        else:
            raise


def link_tree(src: Path, dst: Path) -> None:
    """Recreate ``src`` under ``dst``: directories with their mode, symlinks as is, files hardlinked."""
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, stat.S_IMODE(os.lstat(src).st_mode) | 0o700)
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif entry.is_dir(follow_symlinks=False):
            link_tree(Path(entry.path), target)
        elif entry.is_file(follow_symlinks=False):
            _place_file(Path(entry.path), target)


def snapshot_upper(upper: Path, dest: Path) -> int:
    """Translate an overlay upper directory into an OCI layer directory. Returns entries written."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in sorted(os.scandir(upper), key=lambda e: e.name):
        src = Path(entry.path)
        st = os.lstat(src)
        target = dest / entry.name
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), target)
            count += 1
        elif stat.S_ISDIR(st.st_mode):
            target.mkdir(exist_ok=True)
            os.chmod(target, stat.S_IMODE(st.st_mode) | 0o700)
            count += 1
            if _is_opaque(src):
                (target / OPAQUE).touch()
                count += 1
            count += snapshot_upper(src, target)
        elif _is_whiteout(st):
            (dest / (WHITEOUT_PREFIX + entry.name)).touch()
            count += 1
        elif stat.S_ISREG(st.st_mode):
            _place_file(src, target)
            count += 1
        # sockets, fifos, other devices: skipped
    return count


def _same_file(a: Path, b: Path, sa: os.stat_result, sb: os.stat_result) -> bool:
    if stat.S_IFMT(sa.st_mode) != stat.S_IFMT(sb.st_mode):
        return False
    if stat.S_ISLNK(sa.st_mode):
        return os.readlink(a) == os.readlink(b)
    if stat.S_ISREG(sa.st_mode):
        return (sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns
                and stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode))
    return stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode)


def snapshot_diff(rootfs: Path, base_rootfs: Path, dest: Path) -> int:
    """Write the difference between ``rootfs`` and ``base_rootfs`` as an OCI layer directory."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    cur_names = {e.name for e in os.scandir(rootfs)}
    base_names = {e.name for e in os.scandir(base_rootfs)} if base_rootfs.is_dir() else set()
    for name in sorted(base_names - cur_names):
        (dest / (WHITEOUT_PREFIX + name)).touch()
        count += 1
    for name in sorted(cur_names):
        cur = rootfs / name
        base = base_rootfs / name
        sc = os.lstat(cur)
        try:
            sb: os.stat_result | None = os.lstat(base)
        except FileNotFoundError:
            sb = None
        target = dest / name
        if stat.S_ISDIR(sc.st_mode):
            if sb is not None and not stat.S_ISDIR(sb.st_mode):
                (dest / (WHITEOUT_PREFIX + name)).touch()
                sb = None
            if sb is None:
                link_tree(cur, target)
                count += 1
                continue
            sub = snapshot_diff(cur, base, target)
            if sub or stat.S_IMODE(sc.st_mode) != stat.S_IMODE(sb.st_mode):
                target.mkdir(exist_ok=True)
                os.chmod(target, stat.S_IMODE(sc.st_mode) | 0o700)
                count += sub + 1
            elif target.exists():
                os.rmdir(target)
            continue
        if sb is not None and _same_file(cur, base, sc, sb):
            continue
        if sb is not None and stat.S_ISDIR(sb.st_mode):
            (dest / (WHITEOUT_PREFIX + name)).touch()
        if stat.S_ISLNK(sc.st_mode):
            os.symlink(os.readlink(cur), target)
            count += 1
        elif stat.S_ISREG(sc.st_mode):
            _place_file(cur, target)
            count += 1
    return count


class _Hasher(io.RawIOBase):
    def __init__(self) -> None:
        self.h = hashlib.sha256()

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def write(self, b) -> int:  # type: ignore[override]
        self.h.update(b)
        return len(b)


def hash_layer_dir(layer: Path) -> str:
    """SHA-256 of a deterministic tar of the layer: sorted paths, owner 0, mtime 0."""
    sink = _Hasher()
    with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(layer):
            dirs.sort()
            for name in sorted(dirs + files):
                full = Path(root) / name
                info = tar.gettarinfo(str(full), arcname=str(full.relative_to(layer)))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if info.isfile():
                    with open(full, "rb") as f:
                        tar.addfile(info, f)
                else:
                    tar.addfile(info)
    return "sha256:" + sink.h.hexdigest()
