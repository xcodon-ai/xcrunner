# src/xcodon_runtime/engine_proot.py
"""The proot engine: a copied rootfs and a PRoot process per exec. Works without any kernel help."""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable

log = logging.getLogger(__name__)
VENDORED_DIR = Path(__file__).resolve().parent / "_bin"
STARTED_MARKER = "proot-started"
HOST_BINDS = ("/dev", "/proc", "/sys", "/etc/resolv.conf", "/etc/hosts")


def find_proot() -> str | None:
    """Find a PRoot binary: XCODON_PROOT, then proot on PATH, then the vendored build."""
    env = os.environ.get("XCODON_PROOT")
    if env:
        return env if os.access(env, os.X_OK) else None
    on_path = shutil.which("proot")
    if on_path:
        return on_path
    if platform.machine() == "x86_64":
        vendored = VENDORED_DIR / "proot-x86_64"
        if os.access(vendored, os.X_OK):
            return str(vendored)
    return None


def _copy_rootfs(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["cp", "-a", "--reflink=auto", f"{src}/.", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        log.info("cp --reflink failed (%s); falling back to Python copy", r.stderr.strip())
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)


def _command_exists(rootfs: Path, workdir: str, argv0: str, path_env: str) -> bool:
    """Whether argv0 resolves to an executable regular file in the guest rootfs.

    Mirrors the lookup a shell would do: an absolute path is checked directly,
    a path with a slash is relative to the working directory, and a bare name
    is searched on the guest PATH. PRoot itself refuses to even start when its
    initial command cannot be found this way, so we check first and report
    "command not found" (exit code 127, the container-engine convention) the
    same way whether or not PRoot could have started.
    """
    if argv0.startswith("/"):
        candidates = [rootfs / argv0.lstrip("/")]
    elif "/" in argv0:
        candidates = [rootfs / workdir.lstrip("/") / argv0]
    else:
        candidates = [rootfs / p.lstrip("/") / argv0 for p in path_env.split(":") if p]
    return any(p.is_file() and os.access(p, os.X_OK) for p in candidates)


class ProotEngine:
    name = "proot"

    def start(self, container: Container) -> None:
        proot = find_proot()
        if proot is None:
            raise EngineUnavailable(
                "no PRoot binary: set XCODON_PROOT to a static proot, put proot on PATH, "
                "or use an x86_64 build with the vendored binary"
            )
        rootfs = container.dir / "rootfs"
        if not (container.dir / STARTED_MARKER).exists():
            log.info(
                "container %s: copying rootfs (full copy unless the filesystem supports reflinks)",
                container.short_id,
            )
            _copy_rootfs(Path(container.image_rootfs), rootfs)
            (container.dir / STARTED_MARKER).write_text(proot)
        (rootfs / container.workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def is_running(self, container: Container) -> bool:
        return (container.dir / STARTED_MARKER).exists() and (container.dir / "rootfs").is_dir()

    def popen(self, container: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        if not self.is_running(container):
            raise ContainerNotRunning(f"container {container.short_id} is not started")
        proot = find_proot()
        if proot is None:
            raise EngineUnavailable("PRoot binary disappeared; set XCODON_PROOT")
        rootfs = container.dir / "rootfs"
        (rootfs / workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)
        if argv and not _command_exists(rootfs, workdir, argv[0], env.get("PATH", "")):
            # PRoot cannot even start if its initial command is missing; report
            # "command not found" (127) the way a container engine does, instead
            # of PRoot's own fatal startup error.
            log.info("container %s: %s not found in guest rootfs", container.short_id, argv[0])
            return subprocess.Popen([sys.executable, "-c", "raise SystemExit(127)"], **popen_kwargs)
        cmd = [proot, "-r", str(rootfs), "-w", workdir, "-i", f"{container.uid}:{container.gid}"]
        for host_path in HOST_BINDS:
            if os.path.exists(host_path):
                cmd += ["-b", host_path]
        for b in container.binds:
            cmd += ["-b", f"{b.source}:{b.target}"]
        cmd += shlex.split(os.environ.get("XCODON_PROOT_ARGS", ""))
        cmd += argv
        return subprocess.Popen(cmd, env=env, **popen_kwargs)

    def stop(self, container: Container) -> None:
        return None
