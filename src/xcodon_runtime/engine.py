"""Engine interface, bind mounts, and the choice between the ns and proot engines."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

from xcodon_runtime.errors import EngineUnavailable
from xcodon_runtime.home import CONTAINER_DIR_ENV, RuntimeHome, container_lock_in
from xcodon_runtime.probe import PROBE_NAMES, run_probes

if TYPE_CHECKING:
    from xcodon_runtime.containers import Container

log = logging.getLogger(__name__)
ENGINE_NAMES = ("ns", "proot")


@dataclass(frozen=True)
class Bind:
    source: str
    target: str
    readonly: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Bind":
        return cls(d["source"], d["target"], bool(d.get("readonly", False)))


class Engine(Protocol):
    name: str

    def start(self, container: "Container") -> None: ...

    def popen(self, container: "Container", argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen: ...

    def stop(self, container: "Container") -> None: ...

    def is_running(self, container: "Container") -> bool: ...


def container_lock(container: "Container"):
    """The lock both engines hold across start and stop of one container.

    An engine is given no runtime home, so the lock sits in the container
    dir that holds this container, which is ``<home>/containers`` or
    ``$XRUNNER_CONTAINER_DIR``. Without this lock two concurrent starts both
    see "not running" and each spawns a keeper; the first one is then leaked.
    """
    return container_lock_in(container.dir.parent, f"container-{container.id}")


@dataclass
class EngineChoice:
    name: str
    probes: dict = field(default_factory=dict)
    reason: str = ""


def select_engine(home: RuntimeHome, override: str | None = None) -> EngineChoice:
    override = override or os.environ.get("XCODON_ENGINE") or None
    if override:
        if override not in ENGINE_NAMES:
            raise EngineUnavailable(f"XCODON_ENGINE={override!r}; valid values: {', '.join(ENGINE_NAMES)}")
        return EngineChoice(override, {}, f"requested: {override}")
    probes = run_probes(home.path, home.containers)
    failed = [n for n in PROBE_NAMES if not probes[n]["ok"]]
    if not failed:
        return EngineChoice("ns", probes, "user namespaces, overlayfs, and pid namespaces all work")
    reason = "; ".join(f"{n}: {probes[n]['error'] or 'failed'}" for n in failed)
    if failed == ["overlay"]:
        reason += (f" (the container dir {home.containers} or the image store may be on a filesystem "
                   f"overlayfs cannot use, such as NFS; set {CONTAINER_DIR_ENV} to a local disk)")
    log.info("ns engine unavailable (%s); using proot", reason)
    return EngineChoice("proot", probes, reason)


def get_engine(name: str) -> Engine:
    if name == "ns":
        from xcodon_runtime.engine_ns import NsEngine

        return NsEngine()
    if name == "proot":
        from xcodon_runtime.engine_proot import ProotEngine

        return ProotEngine()
    raise EngineUnavailable(f"unknown engine {name!r}")
