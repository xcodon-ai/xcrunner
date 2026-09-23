"""The ns engine: a Python keeper holds the namespaces, nsexec enters them."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable
from xcodon_runtime.keeper import KEEPER_LOG, KEEPER_PLAN

log = logging.getLogger(__name__)
KEEPER_PID = "keeper.pid"
START_TIMEOUT = 60.0


def process_start_time(pid: int) -> str | None:
    """Field 22 of /proc/PID/stat, or None if the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    after_comm = text.rsplit(")", 1)[1].split()
    return after_comm[19] if len(after_comm) > 19 else None


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


class NsEngine:
    name = "ns"

    def _plan(self, c: Container) -> dict:
        return {
            "lower": c.image_rootfs,
            "upper": str(c.dir / "upper"),
            "work": str(c.dir / "work"),
            "merged": str(c.dir / "merged"),
            "uid": c.uid,
            "gid": c.gid,
            "hostname": c.short_id,
            "workdir": c.workdir,
            "binds": [b.to_dict() for b in c.binds],
        }

    def start(self, c: Container) -> None:
        if self.is_running(c):
            return
        for d in ("upper", "work", "merged"):
            (c.dir / d).mkdir(exist_ok=True)
        plan_path = c.dir / KEEPER_PLAN
        plan_path.write_text(json.dumps(self._plan(c), indent=2))
        log_path = c.dir / KEEPER_LOG

        r, w = os.pipe()
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "xcodon_runtime.keeper", str(plan_path), str(w)],
                pass_fds=(w,), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True, close_fds=True,
            )
        os.close(w)
        buf = b""
        deadline = time.monotonic() + START_TIMEOUT
        try:
            while b"ready\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([r], [], [], remaining)
                if not ready:
                    break
                chunk = os.read(r, 256)
                if not chunk:
                    break
                buf += chunk
        finally:
            os.close(r)
            proc.wait(timeout=5)

        match = re.search(rb"pid (\d+)", buf)
        if b"ready\n" not in buf or not match:
            if match:
                try:
                    os.kill(int(match.group(1)), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise EngineUnavailable(
                f"ns keeper failed to start for container {c.short_id}. Keeper log:\n{_tail(log_path)}"
            )
        pid = int(match.group(1))
        (c.dir / KEEPER_PID).write_text(json.dumps({"pid": pid, "starttime": process_start_time(pid)}))
        log.info("container %s: keeper pid %d", c.short_id, pid)

    def _keeper_pid(self, c: Container) -> int | None:
        try:
            info = json.loads((c.dir / KEEPER_PID).read_text())
        except (OSError, ValueError):
            return None
        pid = int(info["pid"])
        if process_start_time(pid) != info.get("starttime"):
            return None
        return pid

    def is_running(self, c: Container) -> bool:
        return self._keeper_pid(c) is not None

    def popen(self, c: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        pid = self._keeper_pid(c)
        if pid is None:
            raise ContainerNotRunning(f"container {c.short_id} is not running; start it first")
        fd, env_path = tempfile.mkstemp(prefix="exec-", suffix=".env.json", dir=c.dir)
        with os.fdopen(fd, "w") as f:
            json.dump(env, f)
        return subprocess.Popen(
            [sys.executable, "-m", "xcodon_runtime.nsexec", str(pid), workdir, env_path, "--", *argv],
            **popen_kwargs,
        )

    def _clear_overlay_work(self, c: Container) -> None:
        """Remove the empty ``work/work`` overlayfs leaves behind.

        The kernel makes it unreadable, so a later plain delete of the container
        directory would fail on it. It is gone once the overlay is unmounted.
        """
        try:
            (c.dir / "work" / "work").rmdir()
        except OSError:
            pass

    def stop(self, c: Container) -> None:
        pid = self._keeper_pid(c)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and process_start_time(pid) is not None:
                time.sleep(0.02)
            if process_start_time(pid) is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                while process_start_time(pid) is not None:
                    time.sleep(0.02)
            log.info("container %s: keeper stopped", c.short_id)
        (c.dir / KEEPER_PID).unlink(missing_ok=True)
        self._clear_overlay_work(c)
