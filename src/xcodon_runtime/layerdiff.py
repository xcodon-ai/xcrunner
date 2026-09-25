"""Turn a writable layer into an OCI layer directory, and hash layer directories.

The ns engine's overlay upper directory records a deleted file as a
character device 0:0 and a replaced directory with an ``overlay.opaque``
attribute. OCI layers record the same facts as ``.wh.<name>`` files and a
``.wh..wh..opq`` file. The proot engine has no upper directory, so its layer
is the difference between the rootfs copy and the image rootfs.

``snapshot_upper`` and ``snapshot_diff`` always copy regular files: their
source (a live overlay upper directory or a running proot rootfs copy) stays
writable, so hardlinking into it would let a later write against that source
mutate an already-committed layer in place. ``link_tree`` keeps the
hardlink-first behavior: its source is a throwaway snapshot directory about
to be discarded, so sharing inodes there is harmless and it is how the store
moves that snapshot into ``layers/<hex>`` cheaply on the same filesystem.
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


def _strip_overlay_xattrs(path: Path) -> None:
    """Remove any xattr that ``shutil.copy2`` carried over naming an overlay attribute.

    ``copy2`` copies ``user.*`` xattrs along with the file, which would leak
    overlayfs bookkeeping (like ``user.overlay.origin``) into the layer.
    """
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError:
        return
    for name in names:
        if "overlay." in name:
            try:
                os.removexattr(path, name, follow_symlinks=False)
            except OSError:
                pass


def _place_file(src: Path, dst: Path) -> None:
    """Hardlink a regular file into the layer, copying when linking is not possible.

    Used only by ``link_tree``, whose source is a throwaway snapshot about to
    be discarded, so a shared inode there is harmless.
    """
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
            shutil.copy2(src, dst, follow_symlinks=False)
            _strip_overlay_xattrs(dst)
        else:
            raise


def _copy_file(src: Path, dst: Path) -> None:
    """Copy a regular file into the layer, never sharing inodes with a writable source.

    Used by ``snapshot_upper`` and ``snapshot_diff``: their source stays live
    and writable after the snapshot, so hardlinking would let a later write
    mutate an already-committed layer.
    """
    shutil.copy2(src, dst, follow_symlinks=False)
    _strip_overlay_xattrs(dst)


def link_tree(src: Path, dst: Path) -> None:
    """Recreate ``src`` under ``dst``: directories with their mode, symlinks as is, files hardlinked."""
    mode = stat.S_IMODE(os.lstat(src).st_mode)
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o700)  # writable while populating; the true mode is restored below
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif entry.is_dir(follow_symlinks=False):
            link_tree(Path(entry.path), target)
        elif entry.is_file(follow_symlinks=False):
            _place_file(Path(entry.path), target)
    os.chmod(dst, mode)


def _copy_tree(src: Path, dst: Path) -> None:
    """Like ``link_tree``, but always copies regular files (see ``_copy_file``).

    Used by ``snapshot_diff`` for a directory that is entirely new relative to
    the base: ``src`` is a live, writable rootfs, so hardlinking it in would
    let a later write mutate the already-committed layer.
    """
    mode = stat.S_IMODE(os.lstat(src).st_mode)
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o700)  # writable while populating; the true mode is restored below
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif entry.is_dir(follow_symlinks=False):
            _copy_tree(Path(entry.path), target)
        elif entry.is_file(follow_symlinks=False):
            _copy_file(Path(entry.path), target)
    os.chmod(dst, mode)


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
            mode = stat.S_IMODE(st.st_mode)
            target.mkdir(exist_ok=True)
            os.chmod(target, 0o700)  # writable while populating; the true mode is restored below
            count += 1
            if _is_opaque(src):
                (target / OPAQUE).touch()
                count += 1
            count += snapshot_upper(src, target)
            os.chmod(target, mode)
        elif _is_whiteout(st):
            (dest / (WHITEOUT_PREFIX + entry.name)).touch()
            count += 1
        elif stat.S_ISREG(st.st_mode):
            _copy_file(src, target)
            count += 1
        # sockets, fifos, other devices: skipped
    return count


def _base_entry(base_rootfs: Path, parts: list[str]) -> os.stat_result | None | bool:
    """``lstat`` of a guest path in the base rootfs, walked one component at a time.

    Returns None when the path does not exist there, and True (meaning
    "something else is there; keep the entry") when a parent component is
    not a real directory: the host kernel is never asked to follow an image
    symlink.
    """
    cur = base_rootfs
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return None
        except OSError:
            return True
        if i == len(parts) - 1:
            return st
        if not stat.S_ISDIR(st.st_mode):
            return True
    return True


def drop_mount_placeholders(layer: Path, base_rootfs: Path, targets: list[str]) -> int:
    """Remove the empty mountpoints an ns keeper left in a snapshot of its upper layer.

    The keeper creates an empty file or directory (and any missing parent)
    for each mount target the image lacks, for example ``/etc/hosts``. At
    each target and each of its parents, an entry is dropped when it is an
    empty regular file or an empty directory and the base rootfs has
    nothing at that path. An empty directory is also dropped when the base
    has a directory there with the same mode: that is only overlayfs
    copying up a parent, and applying it changes nothing. Deepest paths go
    first, so a parent emptied by an earlier drop goes too. ``targets`` are
    guest paths; symlinks in the base are never followed on the host.
    Returns the number of entries removed.
    """
    paths: set[tuple[str, ...]] = set()
    for target in targets:
        parts = [p for p in os.path.normpath("/" + target.lstrip("/")).split("/") if p]
        for i in range(1, len(parts) + 1):
            paths.add(tuple(parts[:i]))
    removed = 0
    for parts in sorted(paths, key=len, reverse=True):
        entry = layer.joinpath(*parts)
        try:
            st = os.lstat(entry)
        except OSError:
            continue
        base = _base_entry(base_rootfs, list(parts))
        if base is True:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size == 0 and base is None:
            entry.unlink()
            removed += 1
        elif stat.S_ISDIR(st.st_mode) and not os.listdir(entry):
            if base is None or (stat.S_ISDIR(base.st_mode)
                                and stat.S_IMODE(base.st_mode) == stat.S_IMODE(st.st_mode)):
                entry.rmdir()
                removed += 1
    return removed


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
                count += 1
                sb = None
            if sb is None:
                _copy_tree(cur, target)
                count += 1
                continue
            sub = snapshot_diff(cur, base, target)
            if sub or stat.S_IMODE(sc.st_mode) != stat.S_IMODE(sb.st_mode):
                target.mkdir(exist_ok=True)
                os.chmod(target, stat.S_IMODE(sc.st_mode))
                count += sub + 1
            elif target.exists():
                os.rmdir(target)
            continue
        if sb is not None and _same_file(cur, base, sc, sb):
            continue
        if sb is not None and stat.S_ISDIR(sb.st_mode):
            (dest / (WHITEOUT_PREFIX + name)).touch()
            count += 1
        if stat.S_ISLNK(sc.st_mode):
            os.symlink(os.readlink(cur), target)
            count += 1
        elif stat.S_ISREG(sc.st_mode):
            _copy_file(cur, target)
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
