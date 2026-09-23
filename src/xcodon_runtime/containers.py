"""The on-disk container record. ContainerStore is added in a later task."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xcodon_runtime.engine import Bind

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
