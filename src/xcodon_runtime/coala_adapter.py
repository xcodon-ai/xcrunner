"""A ContainerManager for coala-runtime backed by xcodon-runtime.

coala-runtime starts a container once, installs packages, then runs scripts by
repeated exec. This adapter maps those calls onto the Runtime API. It imports
nothing from coala-runtime, so it has no dependency on it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Dict, Optional, Sequence, Union

from xcodon_runtime.api import Runtime
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


class XcodonContainerManager:
    """Rootfs files are owned by the invoking user and the writable layer persists, so installs work."""

    system_site_packages_writable: bool = True

    def __init__(self, home: Path | str | None = None, engine: str | None = None) -> None:
        self.runtime = Runtime(home, engine=engine)
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
            env=dict(environment or {}), name=name,
        )
        self.containers[c.id] = c
        log.info("created xcodon container %s for %s", c.short_id, image)
        return c

    async def start_container(self, container: Container) -> None:
        await _call(self.runtime.start, container)

    async def exec_command(
        self,
        container: Container,
        command: Union[str, Sequence[str]],
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> tuple[int, bytes, bytes]:
        result = await _call(self.runtime.exec, container, command, workdir=workdir, env=environment)
        return result.code, result.stdout, result.stderr

    async def get_logs(self, container: Container, tail: int = 1000) -> str:
        return await _call(_read_log_tail, container, tail)

    async def remove_container(self, container: Container, force: bool = True) -> None:
        await _call(self.runtime.remove, container, force=force)
        self.containers.pop(container.id, None)

    async def cleanup_all(self) -> None:
        for c in list(self.containers.values()):
            try:
                await self.remove_container(c)
            except Exception as e:  # noqa: BLE001 — best effort during shutdown
                log.warning("cleanup of %s failed: %s", c.short_id, e)
