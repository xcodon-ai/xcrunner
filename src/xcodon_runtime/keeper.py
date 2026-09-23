"""The ns keeper: builds the sandbox with system calls, then blocks inside it as pid 1.

Run as:  python -m xcodon_runtime.keeper PLAN_JSON INFO_FD

The parent process writes ``pid <n>`` to INFO_FD and exits. Pid 1 of the new pid
namespace pivots into the image, finishes the mounts, and writes ``ready``. Any
failure before ``ready`` is printed to the keeper log and exits non-zero.

Every mount into the image happens after ``pivot_root``. Before the pivot, a
symlink in the image would resolve against the host root, so a crafted image
could aim a mount point at any path the user can write.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from pathlib import Path

from xcodon_runtime import syscalls as sc

KEEPER_PLAN = "keeper-plan.json"
KEEPER_LOG = "keeper.log"
OLD_ROOT_PREFIX = ".xcodon-oldroot-"
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
DEV_SYMLINKS = (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"))
HOST_FILES = ("/etc/resolv.conf", "/etc/hosts")


def _in_root(path: str, old_name: str) -> str:
    """Resolve a mount target inside the new root.

    This runs after ``pivot_root``, so symlinks in the image resolve against the
    sandbox root and can never reach the host. A target that still lands in the
    old root is refused, because the old root is the host file system.
    """
    resolved = os.path.realpath(path)
    old = f"/{old_name}"
    if resolved == old or resolved.startswith(old + "/"):
        raise ValueError(f"mount target {path!r} resolves into the old root {old!r}")
    return resolved


def _setup_dev(old: str, old_name: str) -> None:
    dev = _in_root("/dev", old_name)
    os.makedirs(dev, exist_ok=True)
    sc.mount("tmpfs", dev, "tmpfs", sc.MS_NOSUID | sc.MS_NOEXEC, "mode=0755,size=65536k")
    for name in DEVICES:
        source = f"{old}/dev/{name}"
        if os.path.exists(source):
            sc.bind_mount(source, f"{dev}/{name}")
    os.makedirs(f"{dev}/pts")
    sc.mount("devpts", f"{dev}/pts", "devpts", sc.MS_NOSUID | sc.MS_NOEXEC,
             "newinstance,ptmxmode=0666,mode=0620")
    os.symlink("pts/ptmx", f"{dev}/ptmx")
    os.makedirs(f"{dev}/shm")
    sc.mount("tmpfs", f"{dev}/shm", "tmpfs", sc.MS_NOSUID | sc.MS_NODEV, "mode=1777")
    for name, target in DEV_SYMLINKS:
        os.symlink(target, f"{dev}/{name}")


def _setup_sandbox(plan: dict, old: str, old_name: str) -> None:
    """Mount everything the sandbox needs, from inside the new root.

    Targets are absolute paths in the new root. Host sources live under ``old``,
    where ``pivot_root`` parked the host file system.
    """
    _setup_dev(old, old_name)
    proc = _in_root("/proc", old_name)
    os.makedirs(proc, exist_ok=True)
    sc.mount("proc", proc, "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
    sysfs = _in_root("/sys", old_name)
    os.makedirs(sysfs, exist_ok=True)
    try:
        sc.bind_mount(f"{old}/sys", sysfs, readonly=True)
    except OSError as e:
        print(f"warning: could not bind /sys: {e}", file=sys.stderr)
    for host_file in HOST_FILES:
        source = f"{old}{host_file}"
        if os.path.exists(source):
            sc.bind_mount(source, _in_root(host_file, old_name), readonly=True)
    for b in plan["binds"]:
        sc.bind_mount(f"{old}{b['source']}", _in_root(b["target"], old_name),
                      readonly=b.get("readonly", False))
    os.makedirs(_in_root(plan["workdir"], old_name), exist_ok=True)


def _preload() -> None:
    """Warm up lazy imports while the interpreter's own files are still reachable.

    After ``pivot_root`` the stdlib is no longer on the file system, so anything
    Python defers until first use would fail. Reading mountinfo needs the
    unicode_escape codec; printing a traceback needs linecache.
    """
    b"warm-up".decode("unicode_escape")
    traceback.format_exc()


def _pivot(merged: str, old_name: str) -> None:
    os.makedirs(f"{merged}/{old_name}")
    sc.pivot_root(merged, f"{merged}/{old_name}")
    os.chdir("/")


def _drop_old_root(old_name: str) -> None:
    sc.umount2(f"/{old_name}", sc.MNT_DETACH)
    os.rmdir(f"/{old_name}")


def _reap(signum, frame) -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _pause_forever() -> None:
    signal.signal(signal.SIGCHLD, _reap)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    while True:
        signal.pause()


def _run(plan: dict, info_fd: int) -> None:
    host_uid, host_gid = os.getuid(), os.getgid()
    sc.unshare(sc.CLONE_NEWUSER | sc.CLONE_NEWNS)
    sc.write_id_maps(plan["uid"], plan["gid"], host_uid, host_gid)
    sc.mount(None, "/", None, sc.MS_REC | sc.MS_PRIVATE)

    merged = plan["merged"]
    os.makedirs(merged, exist_ok=True)
    sc.mount("overlay", merged, "overlay", 0,
             f"lowerdir={plan['lower']},upperdir={plan['upper']},workdir={plan['work']}")

    sc.unshare(sc.CLONE_NEWPID | sc.CLONE_NEWUTS)
    pid = os.fork()
    if pid:
        os.write(info_fd, f"pid {pid}\n".encode())
        os._exit(0)

    old_name = f"{OLD_ROOT_PREFIX}{os.urandom(8).hex()}"
    _preload()
    _pivot(merged, old_name)
    _setup_sandbox(plan, f"/{old_name}", old_name)
    _drop_old_root(old_name)
    sc.sethostname(plan["hostname"])
    os.write(info_fd, b"ready\n")
    os.close(info_fd)
    _pause_forever()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m xcodon_runtime.keeper PLAN_JSON INFO_FD", file=sys.stderr)
        return 2
    plan = json.loads(Path(argv[0]).read_text())
    info_fd = int(argv[1])
    try:
        _run(plan, info_fd)
    except BaseException:  # noqa: BLE001 — anything here must reach the log
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
