"""A ContainerManager for coala-runtime backed by xcodon-runtime.

coala-runtime starts a container once, installs packages, then runs scripts by
repeated exec. This adapter maps those calls onto the Runtime API. It imports
nothing from coala-runtime, so it has no dependency on it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Sequence, Union

from xcodon_runtime.api import Runtime
from xcodon_runtime.condaroot import ENV_DIR_VAR
from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.keeper import KEEPER_LOG

log = logging.getLogger(__name__)


async def _call(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _read_log_tail(container: Container, tail: int) -> str:
    if tail <= 0:
        return ""
    path = container.dir / KEEPER_LOG
    if not path.exists():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-tail:])


class _CoalaContainer:
    """A docker-py-shaped view of an xcodon container.

    coala-runtime's executor calls ``reload()`` and reads ``status`` on the
    object the manager returns (docker-py and the Singularity manager both
    expose these). The bare ``Container`` dataclass does not, so the manager
    hands back this thin view and unwraps it on the way back in.
    """

    def __init__(self, runtime: Runtime, container: Container) -> None:
        self._runtime = runtime
        self.container = container
        self.status = container.state

    @property
    def id(self) -> str:
        return self.container.id

    @property
    def short_id(self) -> str:
        return self.container.short_id

    def reload(self) -> None:
        """Refresh ``status`` from the engine, the way ``docker reload`` does."""
        c = self._runtime.get_container(self.container.id)
        self.container = c
        self.status = c.state

    def __getattr__(self, name):
        # Delegate anything not defined here (dir, name, engine, ...) to the container.
        return getattr(self.__dict__["container"], name)


def _check_env_dir(value: str | None, source: str) -> str | None:
    """Expand ``~`` and require an absolute path, so a bad value fails once, at construction."""
    if value is None:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise XcodonError(f"{source} must be an absolute path: {value}")
    return path


def _unwrap(container) -> Container:
    """Accept either a ``_CoalaContainer`` view or a raw ``Container``."""
    return getattr(container, "container", container)


class XcodonContainerManager:
    """Rootfs files are owned by the invoking user and the writable layer persists, so installs work.

    Set XRUNNER_ENV_DIR to keep installs across coala-runtime's per-call containers.
    """

    system_site_packages_writable: bool = True

    def __init__(self, home: Path | str | None = None, engine: str | None = None,
                 env_dir: str | None = None) -> None:
        self.runtime = Runtime(home, engine=engine)
        self.env_dir = _check_env_dir(env_dir, "env_dir") if env_dir is not None else (
            _check_env_dir(os.environ.get(ENV_DIR_VAR) or None, ENV_DIR_VAR))
        self.containers: Dict[str, Container] = {}

    async def ensure_image(self, image: str) -> None:
        await _call(self.runtime.resolve_image, image)

    async def create_container(
        self,
        image: str,
        command: Optional[Union[str, Sequence[str]]] = None,
        volumes: Optional[Dict[str, Dict[str, str]]] = None,
        working_dir: str = "/workspace",
        environment: Optional[Dict[str, str]] = None,
        name: Optional[str] = None,
    ) -> Container:
        binds = []
        for host, spec in (volumes or {}).items():
            target = spec.get("bind")
            if not isinstance(target, str):
                raise XcodonError(f"volume for {host!r} needs a 'bind' container path")
            binds.append(Bind(host, target, (spec.get("mode") or "rw").lower() == "ro"))
        # The container's main command is never run: the keeper holds the container and
        # every call goes through exec. A harmless default keeps images without CMD usable.
        argv = ["/bin/sh"] if command is None else (["/bin/sh", "-c", command] if isinstance(command, str) else list(command))
        c = await _call(
            self.runtime.create, image, command=argv, binds=binds, workdir=working_dir,
            env=dict(environment or {}), name=name, env_dir=self.env_dir,
        )
        self.containers[c.id] = c
        log.info("created xcodon container %s for %s", c.short_id, image)
        return _CoalaContainer(self.runtime, c)

    async def start_container(self, container) -> None:
        await _call(self.runtime.start, _unwrap(container))

    async def exec_command(
        self,
        container,
        command: Union[str, Sequence[str]],
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> tuple[int, bytes, bytes]:
        result = await _call(self.runtime.exec, _unwrap(container), command, workdir=workdir, env=environment)
        return result.code, result.stdout, result.stderr

    async def get_logs(self, container, tail: int = 1000) -> str:
        return await _call(_read_log_tail, _unwrap(container), tail)

    async def remove_container(self, container, force: bool = True) -> None:
        c = _unwrap(container)
        await _call(self.runtime.remove, c, force=force)
        self.containers.pop(c.id, None)

    async def cleanup_all(self) -> None:
        for c in list(self.containers.values()):
            try:
                await self.remove_container(c)
            except Exception as e:  # noqa: BLE001 — best effort during shutdown
                log.warning("cleanup of %s failed: %s", c.short_id, e)
