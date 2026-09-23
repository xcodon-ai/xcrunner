# src/xcodon_runtime/api.py
"""The Python API. The CLI and the coala adapter are thin layers over this."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from xcodon_runtime import __version__
from xcodon_runtime.containers import Container, ContainerStore
from xcodon_runtime.engine import Bind, Engine, EngineChoice, get_engine, select_engine
from xcodon_runtime.engine_proot import find_proot
from xcodon_runtime.errors import ContainerNotRunning, ImageNotFound, XcodonError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.imagestore import Image, ImageStore
from xcodon_runtime.reference import Platform
from xcodon_runtime.spec import build_spec

log = logging.getLogger(__name__)


@dataclass
class ExecResult:
    code: int
    stdout: bytes
    stderr: bytes


def _argv(command: str | Sequence[str] | None, default: list[str]) -> list[str]:
    if command is None:
        return list(default)
    if isinstance(command, str):
        return ["/bin/sh", "-c", command]
    return list(command)


class Runtime:
    def __init__(self, home: Path | str | None = None, engine: str | None = None) -> None:
        self.home = RuntimeHome(home)
        self.images = ImageStore(self.home)
        self.store = ContainerStore(self.home)
        self._engine_override = engine
        self._choice: EngineChoice | None = None
        self._engines: dict[str, Engine] = {}

    # -- engines -----------------------------------------------------------------

    def engine_choice(self) -> EngineChoice:
        if self._choice is None:
            self._choice = select_engine(self.home, self._engine_override)
        return self._choice

    def _engine(self, c: Container) -> Engine:
        if c.engine not in self._engines:
            self._engines[c.engine] = get_engine(c.engine)
        return self._engines[c.engine]

    # -- images ------------------------------------------------------------------

    def pull(self, ref: str, platform: Platform | None = None) -> Image:
        return self.images.pull(ref, platform)

    def inspect(self, ref: str) -> Image | None:
        return self.images.get(ref)

    def list_images(self) -> list[Image]:
        return self.images.images()

    def remove_image(self, ref: str) -> None:
        self.images.remove(ref)

    def resolve_image(self, ref: str, pull: str = "missing") -> Image:
        if pull == "always":
            return self.images.pull(ref)
        img = self.images.get(ref)
        if img is not None:
            return img
        if pull == "never":
            raise ImageNotFound(f"image {ref!r} is not in the local store")
        log.info("image %s not found locally; pulling", ref)
        return self.images.pull(ref)

    # -- containers --------------------------------------------------------------

    def create(self, ref: str, command: Sequence[str] | None = None, entrypoint: Sequence[str] | None = None,
               binds: Sequence[Bind] = (), workdir: str | None = None, env: Mapping[str, str] | None = None,
               user: str | None = None, name: str | None = None, pull: str = "missing") -> Container:
        image = self.resolve_image(ref, pull)
        container_id = os.urandom(32).hex()
        spec = build_spec(image.config, image.rootfs, container_id, command=command, entrypoint=entrypoint,
                          env=env, workdir=workdir, user=user)
        for b in binds:
            if not os.path.isabs(b.source) or not os.path.isabs(b.target):
                raise XcodonError(f"bind paths must be absolute: {b.source}:{b.target}")
        engine = self.engine_choice().name
        return self.store.create(container_id, image, ref, spec, list(binds), engine, name)

    def start(self, c: Container) -> None:
        self._engine(c).start(c)
        c.state = "running"
        c.save()

    def popen(self, c: Container, command: str | Sequence[str] | None = None, workdir: str | None = None,
              env: Mapping[str, str] | None = None, **popen_kwargs) -> subprocess.Popen:
        engine = self._engine(c)
        if not engine.is_running(c):
            if c.state == "running":
                c.state = "exited"
                c.save()
            raise ContainerNotRunning(f"container {c.short_id} is not running")
        merged_env = dict(c.env)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        return engine.popen(c, _argv(command, c.argv), merged_env, workdir or c.workdir, **popen_kwargs)

    def exec(self, c: Container, command: str | Sequence[str] | None = None, workdir: str | None = None,
             env: Mapping[str, str] | None = None, capture: bool = True, timeout: float | None = None) -> ExecResult:
        pipe = subprocess.PIPE if capture else None
        p = self.popen(c, command, workdir, env, stdout=pipe, stderr=pipe, stdin=subprocess.DEVNULL)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Terminate first, then kill: a plain kill() only stops the
            # nsexec/proot wrapper, and the guest process survives it (nsexec
            # forwards only catchable signals, and PRoot detaches its tracee
            # when killed) unless a hard SIGKILL is given a chance to reach it.
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            p.kill()
            p.communicate()
            raise
        return ExecResult(p.returncode, out or b"", err or b"")

    def stop(self, c: Container) -> None:
        self._engine(c).stop(c)
        c.state = "exited"
        c.save()

    def remove(self, c: Container, force: bool = False) -> None:
        # Consult the container's own tracked state first, not the engine
        # directly: the proot engine has no persistent process to ask about,
        # so its is_running() reports "has been started" for as long as the
        # copied rootfs exists, even long after stop(). The engine is only
        # asked to catch the other direction: a state on disk that still says
        # "running" because a keeper died without going through stop().
        if c.state == "running":
            if self._engine(c).is_running(c):
                if not force:
                    raise XcodonError(f"container {c.short_id} is running; stop it first or use force")
                self.stop(c)
            else:
                c.state = "exited"
                c.save()
        self.store.remove(c)

    def containers(self, all: bool = False) -> list[Container]:
        out = []
        for c in self.store.list():
            running = self._engine(c).is_running(c)
            if c.state == "running" and not running:
                c.state = "exited"
                c.save()
            if running or all:
                out.append(c)
        return out

    def get_container(self, key: str) -> Container:
        c = self.store.get(key)
        if c.state == "running" and not self._engine(c).is_running(c):
            c.state = "exited"
            c.save()
        return c

    # -- run ---------------------------------------------------------------------

    def run(self, ref: str, command: Sequence[str] | None = None, entrypoint: Sequence[str] | None = None,
            binds: Sequence[Bind] = (), workdir: str | None = None, env: Mapping[str, str] | None = None,
            user: str | None = None, name: str | None = None, rm: bool = False, pull: str = "missing",
            stdin=None, stdout=None, stderr=None) -> int:
        """Create, start, and wait for one container, forwarding SIGINT/SIGTERM to it.

        Signal forwarding only works when this is called from the main
        thread: installing a signal handler off the main thread raises
        ValueError, and ``run`` treats that as "no forwarding available"
        instead of failing.
        """
        c = self.create(ref, command, entrypoint, binds, workdir, env, user, name, pull)
        started = False
        try:
            self.start(c)
            started = True
            p = self.popen(c, None, None, None, stdin=stdin, stdout=stdout, stderr=stderr)
            previous = {}

            def forward(signum, frame):
                try:
                    p.send_signal(signum)
                except ProcessLookupError:
                    pass

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous[sig] = signal.signal(sig, forward)
                except ValueError:
                    pass  # not the main thread; run without signal forwarding
            try:
                return p.wait()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        finally:
            try:
                if started:
                    self.stop(c)
            finally:
                if rm:
                    self.store.remove(c)

    # -- misc --------------------------------------------------------------------

    def prune(self) -> list[Path]:
        return self.images.prune()

    def info(self) -> dict:
        choice = self.engine_choice()
        return {
            "version": __version__,
            "home": str(self.home.path),
            "engine": choice.name,
            "engine_reason": choice.reason,
            "probes": choice.probes,
            "proot": find_proot(),
            "python": sys.version.split()[0],
        }
