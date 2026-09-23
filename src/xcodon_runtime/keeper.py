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


def build_mount_steps(plan: dict, old_name: str) -> list[tuple[str, ...]]:
    """The sandbox build as data: the ordered operations ``_setup_sandbox`` runs.

    Every mount target is an absolute path in the new root. Every host source
    lives under the old root, where ``pivot_root`` parks the host file system.
    The list is built before the pivot and asks for nothing the host may be
    missing: the executor skips a source that is not there.
    """
    old = f"/{old_name}"
    merged = plan["merged"]
    steps: list[tuple[str, ...]] = [("pivot_root", merged, f"{merged}/{old_name}")]
    steps.append(("tmpfs", "/dev"))
    steps += [("bind", f"{old}/dev/{name}", f"/dev/{name}", False) for name in DEVICES]
    steps.append(("devpts", "/dev/pts"))
    steps.append(("symlink", "pts/ptmx", "/dev/ptmx"))
    steps.append(("shm", "/dev/shm"))
    steps += [("symlink", target, f"/dev/{name}") for name, target in DEV_SYMLINKS]
    steps.append(("proc", "/proc"))
    steps.append(("bind", f"{old}/sys", "/sys", True))
    steps += [("bind", f"{old}{f}", f, True) for f in HOST_FILES]
    steps += [
        ("bind", f"{old}{b['source']}", b["target"], bool(b.get("readonly", False)))
        for b in plan["binds"]
    ]
    steps.append(("mkdir", plan["workdir"]))
    steps.append(("umount_old_root", old))
    return steps


def _setup_sandbox(steps: list[tuple[str, ...]], old_name: str) -> None:
    """Run the mount steps, from inside the new root once the pivot step is done.

    Each target is resolved with ``_in_root``, so a symlink in the image can
    never aim a mount at the host. A bind whose host source does not exist is
    skipped.
    """
    for step in steps:
        op, args = step[0], step[1:]
        if op == "pivot_root":
            _pivot(args[0], args[1])
        elif op == "tmpfs":
            target = _in_root(args[0], old_name)
            os.makedirs(target, exist_ok=True)
            sc.mount("tmpfs", target, "tmpfs", sc.MS_NOSUID | sc.MS_NOEXEC,
                     "mode=0755,size=65536k")
        elif op == "devpts":
            target = _in_root(args[0], old_name)
            os.makedirs(target)
            sc.mount("devpts", target, "devpts", sc.MS_NOSUID | sc.MS_NOEXEC,
                     "newinstance,ptmxmode=0666,mode=0620")
        elif op == "shm":
            target = _in_root(args[0], old_name)
            os.makedirs(target)
            sc.mount("tmpfs", target, "tmpfs", sc.MS_NOSUID | sc.MS_NODEV, "mode=1777")
        elif op == "proc":
            target = _in_root(args[0], old_name)
            os.makedirs(target, exist_ok=True)
            sc.mount("proc", target, "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
        elif op == "symlink":
            os.symlink(args[0], _in_root(args[1], old_name))
        elif op == "bind":
            source, target, readonly = args[0], args[1], bool(args[2])
            if not os.path.exists(source):
                continue
            resolved = _in_root(target, old_name)
            try:
                sc.bind_mount(source, resolved, readonly=readonly)
            except OSError as e:
                # A missing /sys costs little; anything else is fatal.
                if target != "/sys":
                    raise
                print(f"warning: could not bind /sys: {e}", file=sys.stderr)
        elif op == "mkdir":
            os.makedirs(_in_root(args[0], old_name), exist_ok=True)
        elif op == "umount_old_root":
            _drop_old_root(args[0])
        else:  # pragma: no cover - the step table above is the only producer
            raise ValueError(f"unknown mount step {op!r}")


def _preload() -> None:
    """Warm up lazy imports while the interpreter's own files are still reachable.

    After ``pivot_root`` the stdlib is no longer on the file system, so anything
    Python defers until first use would fail. Reading mountinfo needs the
    unicode_escape codec; printing a traceback needs linecache.
    """
    b"warm-up".decode("unicode_escape")
    traceback.format_exc()


def _pivot(merged: str, old_dir: str) -> None:
    os.makedirs(old_dir)
    sc.pivot_root(merged, old_dir)
    os.chdir("/")


def _drop_old_root(old: str) -> None:
    sc.umount2(old, sc.MNT_DETACH)
    os.rmdir(old)


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
    steps = build_mount_steps(plan, old_name)
    _preload()
    _setup_sandbox(steps, old_name)
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
