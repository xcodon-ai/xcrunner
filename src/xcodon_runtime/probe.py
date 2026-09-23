"""Engine probes. Each probe runs in its own child process so a failure cannot hurt the caller.

Usage as a child: python -m xcodon_runtime.probe NAME HOME_PATH
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


def _enter_userns() -> None:
    # Capture the real host ids before unshare(): once CLONE_NEWUSER takes
    # effect, os.getuid()/getgid() report the unmapped overflow id instead.
    host_uid, host_gid = os.getuid(), os.getgid()
    sc.unshare(sc.CLONE_NEWUSER | sc.CLONE_NEWNS)
    sc.write_id_maps(0, 0, host_uid, host_gid)
    sc.mount(None, "/", None, sc.MS_REC | sc.MS_PRIVATE)


def _probe_userns(home: Path) -> None:
    _enter_userns()


def _probe_overlay(home: Path) -> None:
    _enter_userns()
    base = Path(tempfile.mkdtemp(prefix="probe-", dir=home))
    try:
        for d in ("lower", "upper", "work", "merged"):
            (base / d).mkdir()
        (base / "lower" / "f").write_text("x")
        sc.mount("overlay", str(base / "merged"), "overlay", 0,
                 f"lowerdir={base / 'lower'},upperdir={base / 'upper'},workdir={base / 'work'}")
        assert (base / "merged" / "f").read_text() == "x"
        sc.umount2(str(base / "merged"), sc.MNT_DETACH)
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


def _probe_pidns_proc(home: Path) -> None:
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


_PROBES = {"userns": _probe_userns, "overlay": _probe_overlay, "pidns_proc": _probe_pidns_proc}


def run_probes(home_path: Path | None = None) -> dict[str, dict]:
    from xcodon_runtime.home import RuntimeHome

    home = RuntimeHome(home_path).path
    results: dict[str, dict] = {}
    for name in PROBE_NAMES:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "xcodon_runtime.probe", name, str(home)],
                capture_output=True, text=True, timeout=30,
            )
            err = (r.stderr.strip().splitlines() or [""])[-1] if r.returncode else ""
            results[name] = {"ok": r.returncode == 0, "error": err}
        except subprocess.TimeoutExpired:
            results[name] = {"ok": False, "error": "probe timed out"}
    return results


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in _PROBES:
        print(f"usage: python -m xcodon_runtime.probe {{{'|'.join(PROBE_NAMES)}}} HOME", file=sys.stderr)
        return 2
    try:
        _PROBES[argv[0]](Path(argv[1]))
    except BaseException as e:  # noqa: BLE001 — report anything, this is a probe
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
