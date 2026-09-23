"""Image reference and platform parsing, normalized the way docker does it."""

from __future__ import annotations

import platform as _platform
import re
from dataclasses import dataclass

DEFAULT_REGISTRY = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPOSITORY_RE = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_REGISTRY_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*(?::\d+)?$")


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def name(self) -> str:
        """Fully qualified name: registry/repository:tag[@digest]."""
        text = f"{self.registry}/{self.repository}"
        if self.tag:
            text += f":{self.tag}"
        if self.digest:
            text += f"@{self.digest}"
        return text

    @property
    def api_host(self) -> str:
        """Host to send registry API requests to."""
        return DOCKER_HUB_API if self.registry == DEFAULT_REGISTRY else self.registry

    @property
    def manifest_ref(self) -> str:
        """What to ask the registry for: the digest when present, else the tag."""
        return self.digest or self.tag or "latest"


def parse_reference(text: str) -> Reference:
    text = text.strip()
    if text.startswith("docker://"):
        text = text[len("docker://") :]
    if not text:
        raise ValueError("empty image reference")

    digest = None
    if "@" in text:
        text, digest = text.split("@", 1)
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"invalid digest {digest!r}")

    first, _, rest = text.partition("/")
    looks_like_host = "." in first or ":" in first or first == "localhost"
    if looks_like_host and rest:
        registry, path = first, rest
        if not _REGISTRY_RE.match(registry):
            raise ValueError(f"invalid registry {registry!r}")
    else:
        registry, path = DEFAULT_REGISTRY, text

    tag = None
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        path, tag = path.rsplit(":", 1)
        if not _TAG_RE.match(tag):
            raise ValueError(f"invalid tag {tag!r}")

    if registry == DEFAULT_REGISTRY and "/" not in path:
        path = f"library/{path}"
    if not _REPOSITORY_RE.match(path):
        raise ValueError(f"invalid repository name {path!r}")
    if tag is None and digest is None:
        tag = "latest"
    return Reference(registry, path, tag, digest)


@dataclass(frozen=True)
class Platform:
    os: str = "linux"
    architecture: str = "amd64"
    variant: str | None = None

    def __str__(self) -> str:
        text = f"{self.os}/{self.architecture}"
        return f"{text}/{self.variant}" if self.variant else text


_MACHINE_TO_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def host_platform() -> Platform:
    machine = _platform.machine()
    return Platform("linux", _MACHINE_TO_ARCH.get(machine, machine))


def parse_platform(text: str) -> Platform:
    parts = text.split("/")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(f"platform must be os/arch[/variant], got {text!r}")
    return Platform(parts[0], parts[1], parts[2] if len(parts) == 3 else None)
