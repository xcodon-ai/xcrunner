"""Engine probes. Each probe runs in its own child process so a failure cannot hurt the caller.

Usage as a child: python -m xcodon_runtime.probe NAME HOME_PATH [CONTAINER_DIR]

The overlay probe mounts the way a container does: the read-only side in the
runtime home, where images live, and the writable side in the container dir.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from xcodon_runtime import syscalls as sc

PROBE_NAMES = ("userns", "overlay", "pidns_proc")
# Not an ns probe: whether PRoot can run here at all. PRoot is built on ptrace,
# which Yama (ptrace_scope 2 or 3) or a seccomp filter can block outright.
PTRACE_PROBE = "ptrace"
ALL_PROBES = (*PROBE_NAMES, PTRACE_PROBE)
_PTRACE_TRACEME = 0


PROC_SYS = Path("/proc/sys")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _userns_blocker() -> str:
    """The sysctl that blocks unprivileged user namespaces here, or "" when none is visible."""
    if _read_int(PROC_SYS / "kernel" / "apparmor_restrict_unprivileged_userns") == 1:
        # unshare() itself succeeds, but the new namespace has no capabilities,
        # so writing uid_map fails with EPERM (Ubuntu 23.10 and newer by default).
        return ("AppArmor restricts unprivileged user namespaces "
                "(kernel.apparmor_restrict_unprivileged_userns=1); the admins can set it to 0, or add "
                f"an AppArmor profile with 'userns,' for xcrunner's Python, {os.path.realpath(sys.executable)}")
    if _read_int(PROC_SYS / "user" / "max_user_namespaces") == 0:
        return "user.max_user_namespaces is 0, which turns user namespaces off"
    if _read_int(PROC_SYS / "kernel" / "unprivileged_userns_clone") == 0:
        return "kernel.unprivileged_userns_clone is 0, which turns unprivileged user namespaces off"
    return ""


def _enter_userns() -> None:
    # Capture the real host ids before unshare(): once CLONE_NEWUSER takes
    # effect, os.getuid()/getgid() report the unmapped overflow id instead.
    host_uid, host_gid = os.getuid(), os.getgid()
    try:
        sc.unshare(sc.CLONE_NEWUSER | sc.CLONE_NEWNS)
        sc.write_id_maps(0, 0, host_uid, host_gid)
    except OSError as e:
        blocker = _userns_blocker()
        if blocker:
            raise OSError(f"{e}; {blocker}") from e
        raise
    sc.mount(None, "/", None, sc.MS_REC | sc.MS_PRIVATE)


def _probe_userns(home: Path, containers: Path) -> None:
    _enter_userns()


def _probe_overlay(home: Path, containers: Path) -> None:
    import shutil

    _enter_userns()
    ro = Path(tempfile.mkdtemp(prefix="probe-", dir=home))
    try:
        rw = Path(tempfile.mkdtemp(prefix="probe-", dir=containers))
        try:
            (ro / "lower").mkdir()
            for d in ("upper", "work", "merged"):
                (rw / d).mkdir()
            (ro / "lower" / "f").write_text("x")
            sc.mount("overlay", str(rw / "merged"), "overlay", 0,
                     f"lowerdir={ro / 'lower'},upperdir={rw / 'upper'},workdir={rw / 'work'}")
            assert (rw / "merged" / "f").read_text() == "x"
            sc.umount2(str(rw / "merged"), sc.MNT_DETACH)
        finally:
            shutil.rmtree(rw, ignore_errors=True)
    finally:
        shutil.rmtree(ro, ignore_errors=True)


def _probe_pidns_proc(home: Path, containers: Path) -> None:
    _enter_userns()
    sc.unshare(sc.CLONE_NEWPID)
    pid = os.fork()
    if pid == 0:
        try:
            target = tempfile.mkdtemp(prefix="probe-proc-", dir=home)
            sc.mount("proc", target, "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
            ok = os.path.exists(os.path.join(target, "1"))
            sc.umount2(target, sc.MNT_DETACH)
            os.rmdir(target)
            os._exit(0 if ok else 3)
        except BaseException:
            traceback.print_exc()
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        raise OSError(f"proc mount inside a new pid namespace failed (child exit {code})")


def _ptrace_blocker() -> str:
    """Why ptrace is blocked here, as far as /proc shows it."""
    try:
        scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text())
    except (OSError, ValueError):
        scope = None
    if scope is not None and scope >= 2:
        return f"Yama ptrace_scope is {scope}, which blocks ptrace for normal users"
    try:
        status = dict(line.split(":\t", 1) for line in Path("/proc/self/status").read_text().splitlines()
                      if ":\t" in line)
    except OSError:
        status = {}
    if status.get("Seccomp", "").strip() == "2":
        return ("a seccomp filter on this process blocks it, usually from a container, job sandbox "
                "or agent sandbox around this shell")
    return "the cause is not visible in /proc"


def _probe_ptrace(home: Path, containers: Path) -> None:
    """PRoot's first step: a child asks to be traced by its parent."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    pid = os.fork()
    if pid == 0:
        rc = libc.ptrace(_PTRACE_TRACEME, 0, None, None)
        os._exit(0 if rc == 0 else min(ctypes.get_errno(), 255) or 255)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        err = os.strerror(code) if 0 < code < 255 else f"exit {code}"
        raise OSError(code, f"ptrace(PTRACE_TRACEME) failed: {err}; {_ptrace_blocker()}")


_PROBES = {"userns": _probe_userns, "overlay": _probe_overlay, "pidns_proc": _probe_pidns_proc,
           PTRACE_PROBE: _probe_ptrace}


def run_probe(name: str, home_path: Path, containers_path: Path) -> dict:
    """``{"ok", "error"}`` for one probe, run in a child process."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "xcodon_runtime.probe", name, str(home_path), str(containers_path)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe timed out"}
    err = (r.stderr.strip().splitlines() or [""])[-1] if r.returncode else ""
    return {"ok": r.returncode == 0, "error": err}


def run_probes(home_path: Path | None = None, containers_path: Path | None = None) -> dict[str, dict]:
    from xcodon_runtime.home import RuntimeHome

    home = RuntimeHome(home_path, containers=containers_path)
    return {name: run_probe(name, home.path, home.containers) for name in ALL_PROBES}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (2, 3) or argv[0] not in _PROBES:
        print(f"usage: python -m xcodon_runtime.probe {{{'|'.join(ALL_PROBES)}}} HOME [CONTAINER_DIR]",
              file=sys.stderr)
        return 2
    home = Path(argv[1])
    containers = Path(argv[2]) if len(argv) == 3 else home / "containers"
    try:
        _PROBES[argv[0]](home, containers)
    except BaseException as e:  # noqa: BLE001 — report anything, this is a probe
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
