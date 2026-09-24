"""The ns engine: a Python keeper holds the namespaces, nsexec enters them."""

from __future__ import annotations

import fcntl
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
from xcodon_runtime.engine import container_lock
from xcodon_runtime.envdir import ENV_LOCK_NAME, acquire_env_lock, env_layer_dir, prepare_env_layer
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable
from xcodon_runtime.keeper import KEEPER_LOG, KEEPER_PLAN

log = logging.getLogger(__name__)
KEEPER_PID = "keeper.pid"
KEEPER_ARGV = [sys.executable, "-m", "xcodon_runtime.keeper"]
START_TIMEOUT = 60.0
KILL_TIMEOUT = 5.0


def process_start_time(pid: int) -> str | None:
    """Field 22 of /proc/PID/stat, or None if the process is gone or a zombie.

    A zombie still has a /proc entry, so it must not read as a live keeper.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    after_comm = text.rsplit(")", 1)[-1].split()
    if not after_comm or after_comm[0] == "Z":
        return None
    return after_comm[19] if len(after_comm) > 19 else None


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _overlay_failure_hint(container: Container) -> str:
    """What to tell the user when a keeper with an env folder fails before ready."""
    if not container.env_dir:
        return ""
    return ("the env folder must be on a local filesystem that supports overlay upper layers "
            "(not NFS or similar)")


class NsEngine:
    name = "ns"

    def layer_paths(self, container: Container) -> tuple[Path, Path]:
        """Where this container's upper and work directories live. A pure path: only ``start`` creates folders."""
        if container.env_dir:
            layer = env_layer_dir(container.env_dir, container.image_id)
            return layer / "upper", layer / "work"
        return container.dir / "upper", container.dir / "work"

    def _plan(self, container: Container) -> dict:
        upper, work = self.layer_paths(container)
        return {
            "lower": container.image_rootfs,
            "upper": str(upper),
            "work": str(work),
            "merged": str(container.dir / "merged"),
            "uid": container.uid,
            "gid": container.gid,
            "hostname": container.short_id,
            "workdir": container.workdir,
            "binds": [b.to_dict() for b in container.binds],
        }

    def start(self, container: Container) -> None:
        """Spawn the keeper, under the container lock so two starts cannot race."""
        with container_lock(container):
            self._start_locked(container)

    def _start_locked(self, container: Container) -> None:
        if self.is_running(container):
            return
        if container.env_dir:
            prepare_env_layer(container)
        upper, work = self.layer_paths(container)
        for d in (upper, work, container.dir / "merged"):
            d.mkdir(parents=True, exist_ok=True)
        plan_path = container.dir / KEEPER_PLAN
        plan_path.write_text(json.dumps(self._plan(container), indent=2))
        log_path = container.dir / KEEPER_LOG

        r, w = os.pipe()
        lock_fd = None
        try:
            if container.env_dir:
                lock_fd = acquire_env_lock(upper.parent, container.short_id)
            pass_fds = (w,) if lock_fd is None else (w, lock_fd)
            with open(log_path, "ab") as logf:
                proc = subprocess.Popen(
                    [*KEEPER_ARGV, str(plan_path), str(w)],
                    pass_fds=pass_fds, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                    start_new_session=True, close_fds=True,
                )
        except BaseException:
            os.close(r)
            raise
        finally:
            os.close(w)
            if lock_fd is not None:
                os.close(lock_fd)  # the keeper's inherited copy keeps the flock
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

        match = re.search(rb"pid (\d+)", buf)
        if b"ready\n" not in buf or not match:
            # The keeper may still be stalled, so kill it before waiting on it.
            proc.kill()
            proc.wait()
            if match:
                _kill(int(match.group(1)), signal.SIGKILL)
            hint = _overlay_failure_hint(container)
            raise EngineUnavailable(
                f"ns keeper failed to start for container {container.short_id}. "
                + (f"Note: {hint}. " if hint else "")
                + f"Keeper log:\n{_tail(log_path)}"
            )
        try:
            proc.wait(timeout=KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        pid = int(match.group(1))
        (container.dir / KEEPER_PID).write_text(
            json.dumps({"pid": pid, "starttime": process_start_time(pid)})
        )
        log.info("container %s: keeper pid %d", container.short_id, pid)

    def _keeper_pid(self, container: Container) -> int | None:
        try:
            info = json.loads((container.dir / KEEPER_PID).read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if process_start_time(pid) != info.get("starttime"):
            return None
        return pid

    def is_running(self, container: Container) -> bool:
        return self._keeper_pid(container) is not None

    def popen(self, container: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        pid = self._keeper_pid(container)
        if pid is None:
            raise ContainerNotRunning(
                f"container {container.short_id} is not running; start it first"
            )
        fd, env_path = tempfile.mkstemp(prefix="exec-", suffix=".env.json", dir=container.dir)
        with os.fdopen(fd, "w") as f:
            json.dump(env, f)
        try:
            return subprocess.Popen(
                [sys.executable, "-m", "xcodon_runtime.nsexec",
                 str(pid), workdir, env_path, "--", *argv],
                **popen_kwargs,
            )
        except BaseException:
            os.unlink(env_path)
            raise

    def _clear_overlay_work(self, container: Container) -> None:
        """Remove the empty ``work/work`` overlayfs leaves behind.

        The kernel makes it unreadable, so a later plain delete of the container
        directory would fail on it. It is gone once the overlay is unmounted.

        With an env folder, a second start may be waiting on the layer lock. The
        keeper drops that lock before it is fully gone, so the waiter can mount
        a new overlay on the same ``work/`` first. The directory is removed only
        while this call holds the lock; if the lock is busy, the new overlay
        owns ``work/work`` and it stays.
        """
        _, work = self.layer_paths(container)
        if not container.env_dir:
            _rmdir_quietly(work / "work")
            return
        try:
            fd = os.open(work.parent / ENV_LOCK_NAME, os.O_RDWR)
        except OSError:
            return  # no lock file: no overlay ever used this layer
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            _rmdir_quietly(work / "work")
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def stop(self, container: Container) -> None:
        with container_lock(container):
            self._stop_locked(container)

    def _stop_locked(self, container: Container) -> None:
        pid = self._keeper_pid(container)
        if pid is not None:
            _kill(pid, signal.SIGTERM)
            if not _wait_gone(pid, 2.0):
                _kill(pid, signal.SIGKILL)
                if not _wait_gone(pid, KILL_TIMEOUT):
                    log.warning("container %s: keeper pid %d is still there after SIGKILL",
                                container.short_id, pid)
            log.info("container %s: keeper stopped", container.short_id)
        (container.dir / KEEPER_PID).unlink(missing_ok=True)
        self._clear_overlay_work(container)


def _rmdir_quietly(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_gone(pid: int, timeout: float) -> bool:
    """Wait for the process to die or turn into a zombie. True if it did."""
    deadline = time.monotonic() + timeout
    while process_start_time(pid) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True
