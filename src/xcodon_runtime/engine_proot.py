# src/xcodon_runtime/engine_proot.py
"""The proot engine: a copied rootfs and a PRoot process per exec. Works without any kernel help."""

from __future__ import annotations

import fcntl
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind, container_lock
from xcodon_runtime.envdir import env_layer_dir, prepare_env_layer
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable

log = logging.getLogger(__name__)
VENDORED_DIR = Path(__file__).resolve().parent / "_bin"
STARTED_MARKER = "proot-started"
HOST_BINDS = ("/dev", "/proc", "/sys", "/etc/resolv.conf", "/etc/hosts")
COPY_LOCK_NAME = ".copy.lock"


def find_proot() -> str | None:
    """Find a PRoot binary: XCODON_PROOT, then proot on PATH, then the vendored build."""
    env = os.environ.get("XCODON_PROOT")
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    on_path = shutil.which("proot")
    if on_path:
        return on_path
    if platform.machine() == "x86_64":
        vendored = VENDORED_DIR / "proot-x86_64"
        if os.access(vendored, os.X_OK):
            return str(vendored)
    return None


@contextmanager
def _copy_lock(layer_dir: Path):
    """An exclusive lock on a file inside the layer, so every runtime home sharing it is guarded."""
    fd = os.open(layer_dir / COPY_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _copy_rootfs(src: Path, dst: Path) -> None:
    """Copy into ``<dst>.tmp`` and rename it to ``dst`` when done.

    An interrupted copy then leaves only the ``.tmp`` directory, which the next
    copy removes first, so a partial rootfs is never taken for a complete one.
    """
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    if tmp.exists():
        raise EngineUnavailable(f"could not remove the partial rootfs copy {tmp}; delete it by hand")
    tmp.mkdir(parents=True)
    try:
        r = subprocess.run(["cp", "-a", "--reflink=auto", f"{src}/.", str(tmp)], capture_output=True, text=True)
    except OSError as e:
        log.info("cp is not available (%s); falling back to Python copy", e)
        r = subprocess.CompletedProcess(["cp"], 1, "", str(e))
    if r.returncode != 0:
        log.info("cp --reflink failed (%s); falling back to Python copy", r.stderr.strip())
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(src, tmp, symlinks=True)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise EngineUnavailable(f"could not copy image rootfs for the proot engine: {e}") from e
    os.rename(tmp, dst)


EXIT_CANNOT_EXEC = 126
EXIT_NOT_FOUND = 127


def _guest_join(base: str, rel: str) -> str:
    """Join a guest-absolute base with a relative component, guest (posix) style."""
    return base.rstrip("/") + "/" + rel.lstrip("/")


def _guest_to_host(rootfs: Path, binds: list[Bind], guest_path: str) -> Path:
    """Map a guest-absolute path to the host path PRoot would actually use.

    A bind remaps everything under its target to its source; the longest
    matching target wins, same as PRoot's own overlapping-bind rule. Anything
    not under a bind lives under the copied rootfs.
    """
    best_target = None
    best_source = None
    for b in binds:
        target = b.target.rstrip("/") or "/"
        matches = guest_path == target or (target == "/") or guest_path.startswith(target + "/")
        if matches and (best_target is None or len(target) > len(best_target)):
            best_target, best_source = target, b.source
    if best_target is not None:
        remainder = guest_path[len(best_target):] if best_target != "/" else guest_path
        return Path(best_source) / remainder.lstrip("/")
    return rootfs / guest_path.lstrip("/")


def _resolve_guest(container: Container, rootfs: Path, workdir: str, command: str, path_value: str) -> tuple[str | None, int]:
    """Find command on the guest PATH, the way PRoot's own initial-command lookup would.

    Returns the resolved host path and 0, or None with the exit code to use:
    126 when a matching guest file exists but is not an executable regular
    file, 127 when nothing matches. Candidates are the command itself if it
    contains a slash (a relative one joined to the working directory), else
    each PATH entry joined with the command; each guest candidate is then
    mapped to a host path through the container's binds (see
    ``_guest_to_host``), mirroring ``nsexec._resolve``.
    """
    if "/" in command:
        guest_candidates = [command if command.startswith("/") else _guest_join(workdir, command)]
    else:
        guest_candidates = [_guest_join(d, command) for d in path_value.split(":") if d]
    exists = False
    for guest in guest_candidates:
        host = _guest_to_host(rootfs, container.binds, guest)
        if host.is_file() and os.access(host, os.X_OK):
            return str(host), 0
        if host.exists():
            exists = True
    return None, EXIT_CANNOT_EXEC if exists else EXIT_NOT_FOUND


class ProotEngine:
    """The proot engine: a copied rootfs, PRoot per exec.

    PRoot cannot enforce read-only binds (it has no kernel mount to make
    read-only); a readonly bind is mounted writable and a warning is logged.
    """

    name = "proot"

    def rootfs_path(self, container: Container) -> Path:
        """Where the copied rootfs lives. A pure path: only ``start`` creates folders."""
        if container.env_dir:
            return env_layer_dir(container.env_dir, container.image_id) / "rootfs"
        return container.dir / "rootfs"

    def start(self, container: Container) -> None:
        """Copy the rootfs once, then mark the container started.

        Runs under the container lock so two starts cannot copy at once. The
        marker is written on every successful start, not only after a copy:
        a crash between the copy and the marker used to leave the container
        unstartable, and ``stop`` removes the marker while keeping the rootfs.
        """
        with container_lock(container):
            proot = find_proot()
            if proot is None:
                raise EngineUnavailable(
                    "no PRoot binary: set XCODON_PROOT to a static proot, put proot on PATH, "
                    "or use an x86_64 build with the vendored binary"
                )
            if container.env_dir:
                prepare_env_layer(container)
            rootfs = self.rootfs_path(container)
            if not rootfs.is_dir():
                with _copy_lock(rootfs.parent):
                    if not rootfs.is_dir():
                        log.info("container %s: copying rootfs (full copy unless the filesystem supports reflinks)",
                                 container.short_id)
                        _copy_rootfs(Path(container.image_rootfs), rootfs)
            (container.dir / STARTED_MARKER).write_text(proot)
            (rootfs / container.workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def is_running(self, container: Container) -> bool:
        rootfs = self.rootfs_path(container)
        return (container.dir / STARTED_MARKER).exists() and rootfs.is_dir()

    def popen(self, container: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        if not self.is_running(container):
            raise ContainerNotRunning(f"container {container.short_id} is not started")
        proot = find_proot()
        if proot is None:
            raise EngineUnavailable("PRoot binary disappeared; set XCODON_PROOT")
        rootfs = self.rootfs_path(container)
        (rootfs / workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)
        if argv:
            _, code = _resolve_guest(container, rootfs, workdir, argv[0], env.get("PATH", ""))
            if code != 0:
                # PRoot cannot even start if its initial command is missing or
                # not executable; report the container-engine convention (126
                # or 127) instead of PRoot's own fatal startup error.
                log.info("container %s: %s resolves to exit code %d", container.short_id, argv[0], code)
                return subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"], **popen_kwargs)
        readonly_targets = [b.target for b in container.binds if b.readonly]
        if readonly_targets:
            log.warning(
                "proot engine cannot enforce read-only binds; %s is writable",
                ", ".join(readonly_targets),
            )
        cmd = [proot, "-r", str(rootfs), "-w", workdir, "-i", f"{container.uid}:{container.gid}",
               "--kill-on-exit"]
        for host_path in HOST_BINDS:
            if os.path.exists(host_path):
                cmd += ["-b", host_path]
        for b in container.binds:
            cmd += ["-b", f"{b.source}:{b.target}"]
        cmd += shlex.split(os.environ.get("XCODON_PROOT_ARGS", ""))
        cmd += argv
        return subprocess.Popen(cmd, env=env, **popen_kwargs)

    def stop(self, container: Container) -> None:
        """Remove the started marker. The copied rootfs stays, so a restart is free."""
        with container_lock(container):
            (container.dir / STARTED_MARKER).unlink(missing_ok=True)
