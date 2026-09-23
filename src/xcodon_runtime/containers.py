"""The on-disk container record, and the store that creates, finds, and removes them."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import ContainerNotFound, XcodonError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.imagestore import Image
from xcodon_runtime.spec import ProcessSpec

CONFIG_NAME = "config.json"


@dataclass
class Container:
    id: str
    image_id: str
    image_ref: str
    image_rootfs: str
    engine: str
    argv: list[str]
    env: dict[str, str]
    workdir: str
    uid: int
    gid: int
    binds: list[Bind]
    created: str
    name: str | None = None
    state: str = "created"
    dir: Path = field(default=Path("."), compare=False, repr=False)

    @property
    def short_id(self) -> str:
        return self.id[:12]

    def save(self) -> None:
        data = asdict(self)
        data.pop("dir")
        data["binds"] = [b.to_dict() for b in self.binds]
        tmp = self.dir / (CONFIG_NAME + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.dir / CONFIG_NAME)

    @classmethod
    def load(cls, dir: Path) -> "Container":
        data = json.loads((Path(dir) / CONFIG_NAME).read_text())
        data["binds"] = [Bind.from_dict(b) for b in data.get("binds", [])]
        return cls(dir=Path(dir), **data)


def _rmtree_tolerant(path: Path) -> None:
    """Remove a directory even when the ns engine left a mode-000 ``work/work`` inside it.

    Overlayfs makes that directory unreadable and undeletable; ``NsEngine.stop``
    removes it, but a keeper that died without a clean ``stop`` leaves it
    behind. On any removal error, make the offending path listable and
    writable and retry.
    """

    def _on_error(func, error_path, exc_info) -> None:
        try:
            os.chmod(error_path, 0o700)
        except OSError:
            pass
        if os.path.isdir(error_path) and not os.path.islink(error_path):
            shutil.rmtree(error_path, onerror=_on_error)
        else:
            try:
                os.unlink(error_path)
            except OSError:
                pass

    shutil.rmtree(path, onerror=_on_error)


class ContainerStore:
    """Creates, finds, lists, and removes containers under a runtime home."""

    def __init__(self, home: RuntimeHome) -> None:
        self.home = home

    def create(self, container_id: str, image: Image, image_ref: str, spec: ProcessSpec,
               binds: list[Bind], engine: str, name: str | None) -> Container:
        from datetime import datetime, timezone

        if name is not None:
            for existing in self.list():
                if existing.name == name:
                    raise XcodonError(f"container name {name!r} is already in use by {existing.short_id}")
        cdir = self.home.containers / container_id
        cdir.mkdir()
        c = Container(
            id=container_id, image_id=image.id, image_ref=image_ref, image_rootfs=str(image.rootfs),
            engine=engine, argv=spec.argv, env=spec.env, workdir=spec.workdir, uid=spec.uid, gid=spec.gid,
            binds=list(binds), created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            name=name, dir=cdir,
        )
        c.save()
        return c

    def list(self) -> list[Container]:
        out = []
        for d in sorted(self.home.containers.iterdir()):
            if d.name.endswith(".tmp") or not (d / CONFIG_NAME).exists():
                continue
            out.append(Container.load(d))
        return out

    def get(self, key: str) -> Container:
        containers = self.list()
        for c in containers:
            if c.id == key or (c.name is not None and c.name == key):
                return c
        matches = [c for c in containers if len(key) >= 4 and c.id.startswith(key)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ContainerNotFound(f"container id prefix {key!r} is ambiguous")
        raise ContainerNotFound(f"no container with id or name {key!r}")

    def remove(self, c: Container) -> None:
        _rmtree_tolerant(c.dir)
