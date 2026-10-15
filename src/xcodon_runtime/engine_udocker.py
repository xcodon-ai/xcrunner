"""The udocker engine: udocker's Fakechroot F3 mode, for hosts with no namespaces and no ptrace.

See spec section 17. Each container gets a udocker container named ``c`` in its
own folder (``UDOCKER_CONTAINERS``), imported from the image rootfs and patched
once with F3. Commands run through ``udocker run``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import container_lock
from xcodon_runtime.engine_proot import STARTED_MARKER, _copy_lock, _resolve_guest
from xcodon_runtime.envdir import env_layer_dir, prepare_env_layer
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable

log = logging.getLogger(__name__)

UDOCKER_ENV = "XCRUNNER_UDOCKER"
MODE_ENV = "XCRUNNER_UDOCKER_MODE"
# Settings a container gets under udocker unless the caller sets them.
#
# Fakechroot rewrites the paths a program passes to the C library, but not every entry point:
# code built against an older glibc, such as the HDF5 inside h5py's wheels, calls stat functions
# it misses. HDF5's default driver opens a file, then lstat()s its path to resolve symlinks; the
# open is rewritten and the lstat is not, so every h5py.File and read_h5ad on a bind-mounted path
# failed with "can't retrieve stat info for file" (in every F mode). The stdio driver skips that
# check, and HDF5 1.14+ reads its default driver from HDF5_DRIVER.
GUEST_ENV_DEFAULTS = {"HDF5_DRIVER": "stdio"}
DEFAULT_MODE = "F3"
VALID_MODES = ("F1", "F2", "F3", "F4")
CONTAINER_NAME = "c"
INSTALL_HINT = "pip install 'xcrunner[udocker]'"


def find_udocker() -> str | None:
    """The udocker command: XCRUNNER_UDOCKER, then beside the running Python, then on PATH."""
    env = os.environ.get(UDOCKER_ENV)
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    beside = os.path.join(os.path.dirname(sys.executable), "udocker")
    if os.access(beside, os.X_OK):
        return beside
    return shutil.which("udocker")


def exec_mode() -> str:
    mode = os.environ.get(MODE_ENV) or DEFAULT_MODE
    if mode not in VALID_MODES:
        raise EngineUnavailable(f"{MODE_ENV}={mode!r}; the udocker engine supports {', '.join(VALID_MODES)}")
    return mode


def udocker_home() -> Path:
    """udocker's own folder, which holds its tools. Left to the user: UDOCKER_DIR or ~/.udocker."""
    return Path(os.environ.get("UDOCKER_DIR") or Path.home() / ".udocker")


def _tools_installed() -> bool:
    lib = udocker_home() / "lib"
    return lib.is_dir() and any(lib.glob("libfakechroot-*.so"))


class UdockerEngine:
    """Fakechroot through udocker. No isolation; commands run as the invoking user."""

    name = "udocker"

    def __init__(self) -> None:
        self._installed = False

    # -- paths ------------------------------------------------------------------------

    def containers_dir(self, container: Container) -> Path:
        """udocker's UDOCKER_CONTAINERS for this container. A pure path."""
        if container.env_dir:
            return env_layer_dir(container.env_dir, container.image_id) / "udocker"
        return container.dir / "udocker"

    def rootfs_path(self, container: Container) -> Path:
        """The patched container root, ``<udocker folder>/<id>/ROOT``, reached through the ``c`` alias."""
        return self.containers_dir(container) / CONTAINER_NAME / "ROOT"

    def _udocker(self) -> str:
        udocker = find_udocker()
        if udocker is None:
            raise EngineUnavailable(f"udocker is not installed; {INSTALL_HINT}, or set {UDOCKER_ENV}")
        return udocker

    def _env(self, container: Container) -> dict[str, str]:
        return {**os.environ, "UDOCKER_CONTAINERS": str(self.containers_dir(container))}

    def _call(self, args: list[str], container: Container, what: str, **kw) -> subprocess.CompletedProcess:
        r = subprocess.run([self._udocker(), "--quiet", *args], env=self._env(container),
                           capture_output=True, text=True, **kw)
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
            raise EngineUnavailable(f"udocker {what} failed with exit code {r.returncode}: {tail}")
        return r

    def _ensure_tools(self, container: Container) -> None:
        if self._installed or _tools_installed():
            self._installed = True
            return
        log.info("installing udocker's tools into %s", udocker_home())
        self._call(["install"], container, "install (it needs network access once)", timeout=900)
        self._installed = True

    # -- lifecycle --------------------------------------------------------------------

    def start(self, container: Container) -> None:
        """Import and patch the container once, then mark it started."""
        with container_lock(container):
            mode = exec_mode()
            self._ensure_tools(container)
            if container.env_dir:
                prepare_env_layer(container)
            cdir = self.containers_dir(container)
            cdir.mkdir(parents=True, exist_ok=True)
            if not self.rootfs_path(container).is_dir():
                with _copy_lock(cdir):
                    if not self.rootfs_path(container).is_dir():
                        self._create(container, mode)
            (container.dir / STARTED_MARKER).write_text(self.name)
            (self.rootfs_path(container) / container.workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def _create(self, container: Container, mode: str) -> None:
        log.info("container %s: importing the image into udocker and patching it (%s)", container.short_id, mode)
        cmd = [self._udocker(), "--quiet", "import", "--tocontainer", f"--name={CONTAINER_NAME}", "-",
               f"xcrunner/{container.image_id[:12]}:latest"]
        read_end, write_end = os.pipe()
        try:
            proc = subprocess.Popen(cmd, stdin=read_end, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=self._env(container))
        finally:
            os.close(read_end)

        def send() -> None:
            # A thread, so udocker's own output is read meanwhile and neither side blocks.
            with os.fdopen(write_end, "wb") as pipe:
                try:
                    with tarfile.open(fileobj=pipe, mode="w|") as tar:
                        tar.add(container.image_rootfs, arcname=".")
                except OSError:
                    pass  # udocker stopped reading; its exit code and output say why

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        out, err = proc.communicate()
        sender.join()
        if proc.returncode != 0:
            tail = "\n".join((out + err).decode(errors="replace").strip().splitlines()[-20:])
            raise EngineUnavailable(f"udocker import failed with exit code {proc.returncode}: {tail}")
        self._call(["setup", f"--execmode={mode}", CONTAINER_NAME], container, f"setup --execmode={mode}")

    def is_running(self, container: Container) -> bool:
        return (container.dir / STARTED_MARKER).exists() and self.rootfs_path(container).is_dir()

    def popen(self, container: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        if not self.is_running(container):
            raise ContainerNotRunning(f"container {container.short_id} is not started")
        rootfs = self.rootfs_path(container)
        if argv:
            _, code = _resolve_guest(container, rootfs, workdir, argv[0], env.get("PATH", ""))
            if code != 0:
                log.info("container %s: %s resolves to exit code %d", container.short_id, argv[0], code)
                return subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"], **popen_kwargs)
        readonly = [b.target for b in container.binds if b.readonly]
        if readonly:
            log.warning("udocker engine cannot enforce read-only binds; %s is writable", ", ".join(readonly))
        cmd = [sys.executable, "-m", "xcodon_runtime.groupexec", "--", self._udocker(), "--quiet", "run",
               "--nobanner", "--nometa", f"--workdir={workdir}"]
        cmd += [f"--env={k}={v}" for k, v in {**GUEST_ENV_DEFAULTS, **env}.items()]
        cmd += [f"--volume={b.source}:{b.target}" for b in container.binds]
        cmd += [CONTAINER_NAME, *argv]
        return subprocess.Popen(cmd, env=self._env(container), **popen_kwargs)

    def stop(self, container: Container) -> None:
        """Remove the started marker; the patched copy stays, so a restart is free."""
        with container_lock(container):
            (container.dir / STARTED_MARKER).unlink(missing_ok=True)
