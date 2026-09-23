"""The ns keeper: builds the sandbox with system calls, then blocks inside it as pid 1.

Run as:  python -m xcodon_runtime.keeper PLAN_JSON INFO_FD

The parent process writes ``pid <n>`` to INFO_FD and exits. Pid 1 of the new pid
namespace finishes the mounts, pivots, and writes ``ready``. Any failure before
``ready`` is printed to the keeper log and exits non-zero.
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
OLD_ROOT = ".xcodon-oldroot"
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
DEV_SYMLINKS = (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"))
HOST_FILES = ("/etc/resolv.conf", "/etc/hosts")


def _setup_dev(merged: str) -> None:
    dev = f"{merged}/dev"
    os.makedirs(dev, exist_ok=True)
    sc.mount("tmpfs", dev, "tmpfs", sc.MS_NOSUID | sc.MS_NOEXEC, "mode=0755,size=65536k")
    for name in DEVICES:
        if os.path.exists(f"/dev/{name}"):
            sc.bind_mount(f"/dev/{name}", f"{dev}/{name}")
    os.makedirs(f"{dev}/pts")
    sc.mount("devpts", f"{dev}/pts", "devpts", sc.MS_NOSUID | sc.MS_NOEXEC,
             "newinstance,ptmxmode=0666,mode=0620")
    os.symlink("pts/ptmx", f"{dev}/ptmx")
    os.makedirs(f"{dev}/shm")
    sc.mount("tmpfs", f"{dev}/shm", "tmpfs", sc.MS_NOSUID | sc.MS_NODEV, "mode=1777")
    for name, target in DEV_SYMLINKS:
        os.symlink(target, f"{dev}/{name}")


def _setup_sandbox(plan: dict, merged: str) -> None:
    _setup_dev(merged)
    os.makedirs(f"{merged}/proc", exist_ok=True)
    sc.mount("proc", f"{merged}/proc", "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
    os.makedirs(f"{merged}/sys", exist_ok=True)
    try:
        sc.bind_mount("/sys", f"{merged}/sys", readonly=True)
    except OSError as e:
        print(f"warning: could not bind /sys: {e}", file=sys.stderr)
    for host_file in HOST_FILES:
        if os.path.exists(host_file):
            sc.bind_mount(host_file, f"{merged}{host_file}", readonly=True)
    for b in plan["binds"]:
        sc.bind_mount(b["source"], f"{merged}{b['target']}", readonly=b.get("readonly", False))
    os.makedirs(f"{merged}{plan['workdir']}", exist_ok=True)


def _pivot(merged: str) -> None:
    old = f"{merged}/{OLD_ROOT}"
    os.makedirs(old, exist_ok=True)
    sc.pivot_root(merged, old)
    os.chdir("/")
    sc.umount2(f"/{OLD_ROOT}", sc.MNT_DETACH)
    os.rmdir(f"/{OLD_ROOT}")


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

    _setup_sandbox(plan, merged)
    _pivot(merged)
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
