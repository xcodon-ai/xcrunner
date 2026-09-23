"""Thin ctypes wrappers over the Linux calls the ns engine needs. Every failure is an OSError."""

from __future__ import annotations

import ctypes
import errno
import os
import platform

_libc = ctypes.CDLL(None, use_errno=True)

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWPID = 0x20000000
CLONE_NEWUSER = 0x10000000

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_NOATIME = 1024
MS_NODIRATIME = 2048
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_RELATIME = 1 << 21
MS_STRICTATIME = 1 << 24

MNT_DETACH = 2
PR_SET_NO_NEW_PRIVS = 38

SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41}

_OPTION_FLAGS = {
    "ro": MS_RDONLY,
    "nosuid": MS_NOSUID,
    "nodev": MS_NODEV,
    "noexec": MS_NOEXEC,
    "noatime": MS_NOATIME,
    "nodiratime": MS_NODIRATIME,
    "relatime": MS_RELATIME,
    "strictatime": MS_STRICTATIME,
}


def _b(value: str | None) -> bytes | None:
    return None if value is None else os.fsencode(value)


def _fail(what: str) -> None:
    err = ctypes.get_errno()
    raise OSError(err, f"{what}: {os.strerror(err)}")


def unshare(flags: int) -> None:
    if _libc.unshare(ctypes.c_int(flags)) != 0:
        _fail(f"unshare(0x{flags:x})")


def mount(source: str | None, target: str, fstype: str | None, flags: int = 0, data: str | None = None) -> None:
    rc = _libc.mount(_b(source), _b(target), _b(fstype), ctypes.c_ulong(flags), _b(data))
    if rc != 0:
        _fail(f"mount {source or ''} -> {target} ({fstype or 'bind'}, flags=0x{flags:x})")


def umount2(target: str, flags: int = 0) -> None:
    if _libc.umount2(_b(target), ctypes.c_int(flags)) != 0:
        _fail(f"umount {target}")


def pivot_root(new_root: str, put_old: str) -> None:
    nr = SYS_PIVOT_ROOT.get(platform.machine())
    if nr is None:
        raise OSError(errno.ENOSYS, f"pivot_root syscall number unknown for {platform.machine()}")
    if _libc.syscall(ctypes.c_long(nr), _b(new_root), _b(put_old)) != 0:
        _fail(f"pivot_root {new_root}")


def setns(fd: int, nstype: int = 0) -> None:
    if _libc.setns(ctypes.c_int(fd), ctypes.c_int(nstype)) != 0:
        _fail("setns")


def sethostname(name: str) -> None:
    raw = name.encode()
    if _libc.sethostname(raw, ctypes.c_size_t(len(raw))) != 0:
        _fail("sethostname")


def set_no_new_privs() -> None:
    if _libc.prctl(ctypes.c_int(PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), 0, 0, 0) != 0:
        _fail("prctl(PR_SET_NO_NEW_PRIVS)")


def write_id_maps(uid_inside: int, gid_inside: int, host_uid: int, host_gid: int) -> None:
    """Single-line self mapping. Needs no newuidmap and no capabilities."""
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny")
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"{uid_inside} {host_uid} 1")
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"{gid_inside} {host_gid} 1")


def _unescape(text: str) -> str:
    return text.encode("latin-1").decode("unicode_escape")


def parse_mount_flags(mountinfo_text: str, path: str) -> int:
    """Flags of the topmost mount at ``path`` according to a mountinfo listing."""
    flags = 0
    for line in mountinfo_text.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        if _unescape(fields[4]) == path:
            flags = sum(_OPTION_FLAGS.get(opt, 0) for opt in fields[5].split(","))
    return flags


def mount_flags_at(path: str) -> int:
    with open("/proc/self/mountinfo") as f:
        return parse_mount_flags(f.read(), path)


def ensure_mountpoint(source: str, target: str) -> None:
    """Make ``target`` a directory or an empty file to match ``source``. Replaces a symlink."""
    if os.path.islink(target):
        os.unlink(target)
    if os.path.isdir(source):
        os.makedirs(target, exist_ok=True)
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        open(target, "a").close()


def remount_readonly(target: str) -> None:
    """Inside a user namespace the remount must repeat the source's locked flags."""
    mount(None, target, None, MS_BIND | MS_REMOUNT | MS_RDONLY | mount_flags_at(target))


def bind_mount(source: str, target: str, readonly: bool = False) -> None:
    ensure_mountpoint(source, target)
    mount(source, target, None, MS_BIND | MS_REC)
    if readonly:
        remount_readonly(target)
