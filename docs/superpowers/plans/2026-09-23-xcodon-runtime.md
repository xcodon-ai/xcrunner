# xcodon-runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `xcodon-runtime`, a rootless container runtime that pulls Docker images and runs them with kernel namespaces on normal hosts or PRoot on locked-down hosts, exposed as a docker-compatible CLI, a Python API, and a coala-runtime adapter.

**Architecture:** An image store pulls layers from a registry or a local Docker daemon, extracts them once as the invoking user, and flattens them into a hardlinked rootfs. A container is a directory with a writable layer. Two engines run processes in it: the `ns` engine builds a sandbox in a Python keeper process with `unshare`, overlayfs, `pivot_root`, and enters it with `setns`; the `proot` engine copies the rootfs and runs a vendored static PRoot. A spec builder merges image config with run options.

**Tech Stack:** Python 3.10+, standard library only (ctypes for system calls, tarfile, urllib, argparse), hatchling, pytest. Vendored static PRoot v5.4.1 for x86_64.

**Spec:** `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`

## Global Constraints

- Python `>=3.10`, Linux only. Python's `tarfile` must offer extraction filters (`tarfile.tar_filter` exists in 3.10.12+, 3.11.4+, 3.12+). Fail with a clear error otherwise.
- No required third-party Python packages. Optional extra `zstd` installs `zstandard>=0.21`.
- Build backend hatchling. Console script `xcodon = "xcodon_runtime.cli:main"`.
- Runtime home default `~/.xcodon/runtime`, override `XCODON_RUNTIME_HOME`.
- Environment variables: `XCODON_ENGINE=ns|proot`, `XCODON_PROOT`, `XCODON_PROOT_ARGS`, `XCODON_LOG=info|debug`.
- CLI exit codes: 125 runtime error, 126 command cannot execute, 127 command not found, else the process exit code.
- Only PRoot for x86_64 is vendored: `xcodon_runtime/_bin/proot-x86_64`, from proot-me release v5.4.1, SHA-256 `19f44283f5c0e73091c60195f5fcd4f4c1165505e44410d434e2ab1b677c1a09`.
- All file ownership inside images is squashed to the invoking user. Setuid, setgid, sticky, and group/other write bits are dropped at extraction.
- Docs and messages in plain, direct English. Short sentences. No jargon without definition.
- Commit after every task. Use `git add` with explicit paths.

## File Structure

```
pyproject.toml                        package metadata, pytest markers
README.md                             install, usage, coala integration
scripts/fetch_proot.py                downloads and verifies the vendored PRoot
.github/workflows/ci.yml              unit + ns + proot + network on ubuntu-latest
src/xcodon_runtime/
  __init__.py                         __version__
  errors.py                           exception hierarchy
  reference.py                        Reference, Platform, parse_reference, host_platform
  home.py                             RuntimeHome: paths, flock, atomic dirs, refs.json
  tarlayer.py                         open_layer_stream, extract_layer
  flatten.py                          build_rootfs with whiteouts
  registry.py                         RegistryClient, FetchedImage, FetchedLayer, select_platform
  daemon.py                           DaemonSource, load_oci_layout_tar
  imagestore.py                       Image, ImageStore (pull, get, images, remove, inspect, prune)
  spec.py                             ProcessSpec, build_spec, resolve_user
  syscalls.py                         ctypes wrappers and mount helpers
  probe.py                            engine probes (run as child processes)
  engine.py                           Bind, Engine protocol, EngineChoice, select_engine, get_engine
  keeper.py                           ns keeper process (python -m xcodon_runtime.keeper)
  nsexec.py                           enters a keeper's namespaces and execs (python -m xcodon_runtime.nsexec)
  engine_ns.py                        NsEngine
  engine_proot.py                     ProotEngine, find_proot
  containers.py                       Container, ContainerStore
  api.py                              Runtime, ExecResult
  cli.py                              argparse CLI over Runtime
  coala_adapter.py                    XcodonContainerManager for coala-runtime
  _bin/proot-x86_64, _bin/MANIFEST, _bin/LICENSE-proot
tests/
  conftest.py                         fixtures: home, busybox_rootfs, layer builders; marker skips
  fake_registry.py                    in-process fake registry with token auth and blob redirect
  test_reference.py test_home.py test_tarlayer.py test_flatten.py test_registry.py
  test_daemon.py test_imagestore.py test_spec.py test_syscalls.py test_engine_select.py
  test_engine_ns.py test_engine_proot.py test_vendored.py test_containers_api.py
  test_cli.py test_coala_adapter.py test_network.py test_cwltool.py
```

Each module has one job. Later tasks import only the names listed in the **Interfaces** block of earlier tasks.

---

### Task 1: Project scaffold, errors, and image references

**Files:**
- Create: `pyproject.toml`
- Create: `README.md` (one paragraph; expanded in Task 15)
- Create: `.gitignore`
- Create: `src/xcodon_runtime/__init__.py`
- Create: `src/xcodon_runtime/errors.py`
- Create: `src/xcodon_runtime/reference.py`
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_reference.py`

**Interfaces:**
- Produces: `errors.XcodonError`, `ImageNotFound`, `PullError`, `UnsupportedLayer`, `EngineUnavailable`, `ContainerNotFound`, `ContainerNotRunning`, `ExecError` (all subclasses of `XcodonError`).
- Produces: `reference.Reference(registry: str, repository: str, tag: str | None, digest: str | None)` with properties `name`, `api_host`, `manifest_ref`; `parse_reference(text: str) -> Reference`; `Platform(os: str, architecture: str, variant: str | None)`; `host_platform() -> Platform`; `parse_platform(text: str) -> Platform`.

- [ ] **Step 1: Create pyproject.toml and package skeleton**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "xcodon-runtime"
version = "0.1.0"
description = "Rootless container runtime for Docker images, built for coala"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
zstd = ["zstandard>=0.21"]
dev = ["pytest>=8", "ruff>=0.4"]

[project.scripts]
xcodon = "xcodon_runtime.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/xcodon_runtime"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
  "network: needs internet access",
  "ns: needs unprivileged user namespaces and overlayfs",
  "proot: needs a PRoot binary",
  "docker: needs a local docker daemon",
  "cwltool: needs cwltool installed",
]

[tool.ruff]
line-length = 100
target-version = "py310"
```

```python
# src/xcodon_runtime/__init__.py
"""Rootless container runtime for Docker images."""

__version__ = "0.1.0"
```

```
# .gitignore
__pycache__/
*.pyc
.venv/
dist/
.pytest_cache/
.ruff_cache/
```

README.md for now:

```markdown
# xcodon-runtime

Rootless container runtime for Docker images. Runs with kernel user
namespaces where available and falls back to PRoot elsewhere. Built to plug
into coala and coala-runtime. See `docs/superpowers/specs/` for the design.
```

Run:
```bash
cd /home/qhu/Workspace/xcodon-runtime && uv venv .venv && . .venv/bin/activate && uv pip install -e ".[dev]" && python -c "import xcodon_runtime; print(xcodon_runtime.__version__)"
```
Expected: `0.1.0`

- [ ] **Step 2: Write errors.py**

```python
# src/xcodon_runtime/errors.py
"""Exception hierarchy. Every error raised by xcodon-runtime derives from XcodonError."""


class XcodonError(Exception):
    """Base class. The message says what failed and, when known, the fix."""


class ImageNotFound(XcodonError):
    """The image is not in the local store."""


class PullError(XcodonError):
    """Fetching an image failed: HTTP error, auth, digest mismatch, no platform match."""


class UnsupportedLayer(XcodonError):
    """A layer uses a media type or compression we cannot read."""


class EngineUnavailable(XcodonError):
    """No engine can run on this host, or the requested engine cannot."""


class ContainerNotFound(XcodonError):
    """No container matches the id, prefix, or name."""


class ContainerNotRunning(XcodonError):
    """The container has no live keeper."""


class ExecError(XcodonError):
    """A command could not be started inside the container."""
```

- [ ] **Step 3: Write the failing reference tests**

```python
# tests/test_reference.py
import pytest

from xcodon_runtime.reference import (
    Platform,
    Reference,
    host_platform,
    parse_platform,
    parse_reference,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("python:3.12", Reference("docker.io", "library/python", "3.12", None)),
        ("python", Reference("docker.io", "library/python", "latest", None)),
        ("xcodon/foo", Reference("docker.io", "xcodon/foo", "latest", None)),
        (
            "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
            Reference("quay.io", "biocontainers/samtools", "1.20--h50ea8bc_0", None),
        ),
        ("localhost:5000/img:v1", Reference("localhost:5000", "img", "v1", None)),
        ("ghcr.io/org/app", Reference("ghcr.io", "org/app", "latest", None)),
        (
            "xcodon/10-1101_2025-06-17-659900_v1-python-deps:latest",
            Reference("docker.io", "xcodon/10-1101_2025-06-17-659900_v1-python-deps", "latest", None),
        ),
        ("docker://alpine:3.19", Reference("docker.io", "library/alpine", "3.19", None)),
    ],
)
def test_parse_reference(text, expected):
    assert parse_reference(text) == expected


def test_digest_reference():
    d = "sha256:" + "a" * 64
    ref = parse_reference(f"alpine@{d}")
    assert ref == Reference("docker.io", "library/alpine", None, d)
    assert ref.manifest_ref == d


def test_tag_and_digest_prefers_digest():
    d = "sha256:" + "b" * 64
    ref = parse_reference(f"alpine:3.19@{d}")
    assert ref.tag == "3.19"
    assert ref.manifest_ref == d


def test_name_round_trip():
    assert parse_reference("python:3.12").name == "docker.io/library/python:3.12"
    assert parse_reference("quay.io/a/b").name == "quay.io/a/b:latest"


def test_api_host_for_docker_hub():
    assert parse_reference("python").api_host == "registry-1.docker.io"
    assert parse_reference("quay.io/a/b").api_host == "quay.io"


@pytest.mark.parametrize("bad", ["", "Upper/case", "a@sha256:short", "a:b:c/d"])
def test_invalid_reference(bad):
    with pytest.raises(ValueError):
        parse_reference(bad)


def test_platform_parsing():
    assert parse_platform("linux/amd64") == Platform("linux", "amd64", None)
    assert parse_platform("linux/arm64/v8") == Platform("linux", "arm64", "v8")
    assert host_platform().os == "linux"
    assert host_platform().architecture in {"amd64", "arm64"}
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_reference.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.reference'`

- [ ] **Step 5: Write reference.py**

```python
# src/xcodon_runtime/reference.py
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_reference.py -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml README.md .gitignore src/xcodon_runtime/__init__.py src/xcodon_runtime/errors.py src/xcodon_runtime/reference.py tests/__init__.py tests/test_reference.py
git commit -m "feat: project scaffold, error hierarchy, image reference parsing"
```

---

### Task 2: RuntimeHome — paths, locks, atomic directories, refs

**Files:**
- Create: `src/xcodon_runtime/home.py`
- Create: `tests/conftest.py`
- Test: `tests/test_home.py`

**Interfaces:**
- Produces: `home.RuntimeHome(path: Path | str | None = None)` with attributes `path`, `blobs`, `layers`, `images`, `containers`, `locks`, `refs_file` (all `Path`); methods `lock(name: str)` (context manager, exclusive flock), `atomic_dir(final: Path)` (context manager yielding a temp `Path`, renamed to `final` on success), `read_refs() -> dict[str, str]`, `write_refs(refs: dict[str, str]) -> None`, `prune_leftovers() -> list[Path]`.
- Produces test fixture `home` (a `RuntimeHome` under `tmp_path`).

- [ ] **Step 1: Write conftest with the home fixture**

```python
# tests/conftest.py
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from xcodon_runtime.home import RuntimeHome


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> RuntimeHome:
    """A fresh runtime home under tmp_path. Also exported via env for subprocesses."""
    root = tmp_path / "runtime-home"
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(root))
    return RuntimeHome(root)


def _busybox() -> Path | None:
    for candidate in ("/usr/bin/busybox", "/bin/busybox"):
        p = Path(candidate)
        if p.exists():
            out = subprocess.run(["file", "-L", str(p)], capture_output=True, text=True).stdout
            if "statically linked" in out:
                return p
    return None


BUSYBOX = _busybox()
APPLETS = ["sh", "id", "hostname", "ls", "cat", "head", "touch", "sleep", "echo", "env", "pwd", "true", "false"]


def build_busybox_rootfs(dest: Path) -> Path:
    """A tiny rootfs with a static busybox and the applets tests need."""
    assert BUSYBOX is not None
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    for d in ("etc", "tmp", "root", "home/user", "proc", "sys", "dev", "workspace"):
        (dest / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUSYBOX, dest / "bin" / "busybox")
    for applet in APPLETS:
        link = dest / "bin" / applet
        if not link.exists():
            link.symlink_to("busybox")
    (dest / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\nuser:x:1000:1000:user:/home/user:/bin/sh\n"
    )
    (dest / "etc" / "group").write_text("root:x:0:\nuser:x:1000:\nstaff:x:50:\n")
    os.chmod(dest / "tmp", 0o777)
    return dest


@pytest.fixture
def busybox_rootfs(tmp_path: Path) -> Path:
    if BUSYBOX is None:
        pytest.skip("no static busybox on this host")
    return build_busybox_rootfs(tmp_path / "bbroot")


def pytest_collection_modifyitems(config, items):
    """Skip marked tests whose prerequisites are missing."""
    from xcodon_runtime.probe import run_probes  # noqa: WPS433  (exists from Task 9)

    probes = None
    for item in items:
        if "ns" in item.keywords:
            if probes is None:
                probes = run_probes()
            if not all(probes[k]["ok"] for k in ("userns", "overlay", "pidns_proc")):
                item.add_marker(pytest.mark.skip(reason=f"ns engine unavailable: {probes}"))
        if "docker" in item.keywords:
            if shutil.which("docker") is None or subprocess.run(
                ["docker", "version"], capture_output=True
            ).returncode != 0:
                item.add_marker(pytest.mark.skip(reason="no docker daemon"))
        if "cwltool" in item.keywords and shutil.which("cwltool") is None:
            item.add_marker(pytest.mark.skip(reason="cwltool not installed"))
        if "network" in item.keywords and os.environ.get("XCODON_TEST_NETWORK") != "1":
            item.add_marker(pytest.mark.skip(reason="set XCODON_TEST_NETWORK=1 to run network tests"))
        if "proot" in item.keywords:
            from xcodon_runtime.engine_proot import find_proot  # exists from Task 11

            if find_proot() is None:
                item.add_marker(pytest.mark.skip(reason="no proot binary"))
```

Note for the implementer of this task: the two imports inside `pytest_collection_modifyitems` refer to modules created in Tasks 9 and 11. Until those tasks exist, guard each import with `try/except ImportError` and treat the probe as unavailable. Remove the guards in Task 11.

- [ ] **Step 2: Write the failing home tests**

```python
# tests/test_home.py
import json
import os
import threading
import time
from pathlib import Path

import pytest

from xcodon_runtime.home import RuntimeHome


def test_creates_layout(tmp_path):
    h = RuntimeHome(tmp_path / "h")
    for d in (h.blobs, h.layers, h.images, h.containers, h.locks):
        assert d.is_dir()
    assert h.blobs == tmp_path / "h" / "blobs" / "sha256"


def test_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(tmp_path / "fromenv"))
    assert RuntimeHome().path == (tmp_path / "fromenv").resolve()


def test_home_default_is_under_dot_xcodon(monkeypatch, tmp_path):
    monkeypatch.delenv("XCODON_RUNTIME_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert RuntimeHome().path == (tmp_path / ".xcodon" / "runtime").resolve()


def test_atomic_dir_renames_on_success(home):
    final = home.images / "abc"
    with home.atomic_dir(final) as tmp:
        assert tmp.name == "abc.tmp"
        (tmp / "f").write_text("x")
    assert (final / "f").read_text() == "x"
    assert not tmp.exists()


def test_atomic_dir_cleans_up_on_error(home):
    final = home.images / "abc"
    with pytest.raises(RuntimeError):
        with home.atomic_dir(final) as tmp:
            (tmp / "f").write_text("x")
            raise RuntimeError("boom")
    assert not final.exists()
    assert not tmp.exists()


def test_lock_is_exclusive(home):
    order = []

    def worker(name, hold):
        with home.lock("shared"):
            order.append(f"{name}-in")
            time.sleep(hold)
            order.append(f"{name}-out")

    t1 = threading.Thread(target=worker, args=("a", 0.2))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=("b", 0))
    t2.start()
    t1.join()
    t2.join()
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_refs_round_trip(home):
    assert home.read_refs() == {}
    home.write_refs({"docker.io/library/a:latest": "1" * 64})
    assert home.read_refs() == {"docker.io/library/a:latest": "1" * 64}
    assert json.loads(home.refs_file.read_text())


def test_prune_leftovers(home):
    (home.layers / "x.tmp").mkdir()
    (home.images / "y.tmp").mkdir()
    (home.blobs / "z.part").write_bytes(b"")
    (home.layers / "keep").mkdir()
    removed = home.prune_leftovers()
    assert {p.name for p in removed} == {"x.tmp", "y.tmp", "z.part"}
    assert (home.layers / "keep").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_home.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.home'`

- [ ] **Step 4: Write home.py**

```python
# src/xcodon_runtime/home.py
"""The runtime home directory: layout, locks, atomic directory builds, and the refs file."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def default_home() -> Path:
    env = os.environ.get("XCODON_RUNTIME_HOME")
    if env:
        return Path(env)
    return Path.home() / ".xcodon" / "runtime"


class RuntimeHome:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_home()
        self.path = self.path.expanduser().resolve()
        self.blobs = self.path / "blobs" / "sha256"
        self.layers = self.path / "layers"
        self.images = self.path / "images"
        self.containers = self.path / "containers"
        self.locks = self.path / "locks"
        self.refs_file = self.path / "refs.json"
        for d in (self.blobs, self.layers, self.images, self.containers, self.locks):
            d.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        """Exclusive advisory lock shared by every process using this home."""
        fd = os.open(self.locks / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def atomic_dir(self, final: Path) -> Iterator[Path]:
        """Build into ``<final>.tmp`` and rename on success. Removes the temp dir on error."""
        tmp = final.with_name(final.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            yield tmp
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        os.rename(tmp, final)

    def read_refs(self) -> dict[str, str]:
        try:
            return json.loads(self.refs_file.read_text())
        except FileNotFoundError:
            return {}

    def write_refs(self, refs: dict[str, str]) -> None:
        tmp = self.refs_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(refs, indent=2, sort_keys=True))
        os.replace(tmp, self.refs_file)

    def prune_leftovers(self) -> list[Path]:
        """Remove half-built ``*.tmp`` directories and ``*.part`` blobs."""
        removed: list[Path] = []
        for parent in (self.layers, self.images, self.containers):
            for p in parent.glob("*.tmp"):
                shutil.rmtree(p, ignore_errors=True)
                removed.append(p)
        for p in self.blobs.glob("*.part"):
            p.unlink(missing_ok=True)
            removed.append(p)
        return removed
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_home.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/xcodon_runtime/home.py tests/conftest.py tests/test_home.py
git commit -m "feat: runtime home with locks, atomic dirs, and refs file"
```

---

### Task 3: Layer extraction

**Files:**
- Create: `src/xcodon_runtime/tarlayer.py`
- Test: `tests/test_tarlayer.py`

**Interfaces:**
- Consumes: `errors.UnsupportedLayer`, `errors.XcodonError`.
- Produces: `tarlayer.open_layer_stream(path: Path) -> BinaryIO` (gzip, zstd, or plain by magic bytes), `tarlayer.extract_layer(stream: BinaryIO, dest: Path) -> int` (returns the count of skipped entries).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tarlayer.py
import gzip
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from xcodon_runtime.errors import UnsupportedLayer
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream


def make_tar(entries) -> bytes:
    """entries: list of (TarInfo, data bytes or None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            else:
                t.addfile(info)
    return buf.getvalue()


def ti(name, type=tarfile.REGTYPE, mode=0o644, linkname="", uid=0, gid=0):
    i = tarfile.TarInfo(name)
    i.type = type
    i.mode = mode
    i.linkname = linkname
    i.uid, i.gid = uid, gid
    return i


def test_extract_regular_files_symlinks_and_dirs(tmp_path):
    data = make_tar(
        [
            (ti("etc", tarfile.DIRTYPE, 0o755), None),
            (ti("etc/passwd", uid=0, gid=0), b"root:x:0:0::/root:/bin/sh\n"),
            (ti("bin", tarfile.SYMTYPE, linkname="usr/bin"), None),
            (ti("etc/alt", tarfile.SYMTYPE, linkname="/usr/bin/sudo"), None),
            (ti("usr/bin/sudo", mode=0o4755), b"x"),
            (ti("tmp", tarfile.DIRTYPE, 0o1777), None),
            (ti("ro", tarfile.DIRTYPE, 0o555), None),
            (ti("ro/inside"), b"y"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), tmp_path)
    assert skipped == 0
    assert (tmp_path / "etc/passwd").read_bytes().startswith(b"root")
    assert os.readlink(tmp_path / "bin") == "usr/bin"
    assert os.readlink(tmp_path / "etc/alt") == "/usr/bin/sudo"
    mode = stat.S_IMODE(os.lstat(tmp_path / "usr/bin/sudo").st_mode)
    assert not mode & stat.S_ISUID
    assert stat.S_IMODE(os.lstat(tmp_path / "tmp").st_mode) & 0o700 == 0o700
    assert (tmp_path / "ro/inside").read_bytes() == b"y", "files inside 0555 dirs must extract"
    assert os.lstat(tmp_path / "etc/passwd").st_uid == os.getuid()


def test_skips_devices_and_escapes(tmp_path, caplog):
    data = make_tar(
        [
            (ti("dev/null", tarfile.CHRTYPE), None),
            (ti("escape", tarfile.SYMTYPE, linkname="../../outside"), None),
            (ti("escape/evil"), b"pwned"),
            (ti("ok"), b"fine"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), tmp_path)
    assert skipped == 2
    assert (tmp_path / "ok").exists()
    assert not (tmp_path.parent / "outside").exists()
    assert not (tmp_path / "dev/null").exists()


def test_hardlinks_preserved(tmp_path):
    data = make_tar(
        [
            (ti("a"), b"same"),
            (ti("b", tarfile.LNKTYPE, linkname="a"), None),
        ]
    )
    extract_layer(io.BytesIO(data), tmp_path)
    assert os.stat(tmp_path / "a").st_ino == os.stat(tmp_path / "b").st_ino


def test_open_layer_stream_detects_gzip_and_plain(tmp_path):
    raw = make_tar([(ti("f"), b"1")])
    (tmp_path / "plain").write_bytes(raw)
    (tmp_path / "gz").write_bytes(gzip.compress(raw))
    for name in ("plain", "gz"):
        with open_layer_stream(tmp_path / name) as s:
            dest = tmp_path / f"out-{name}"
            dest.mkdir()
            extract_layer(s, dest)
            assert (dest / "f").read_bytes() == b"1"


def test_open_layer_stream_zstd_without_module(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zstandard":
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    (tmp_path / "z").write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 16)
    with pytest.raises(UnsupportedLayer, match="zstd"):
        open_layer_stream(tmp_path / "z")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tarlayer.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write tarlayer.py**

```python
# src/xcodon_runtime/tarlayer.py
"""Extract one image layer tar as an unprivileged user."""

from __future__ import annotations

import gzip
import logging
import tarfile
from pathlib import Path
from typing import BinaryIO

from xcodon_runtime.errors import UnsupportedLayer, XcodonError

log = logging.getLogger(__name__)

GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Entries we cannot create without privilege. Skipped with a log line.
_SKIP_TYPES = {tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE}


def open_layer_stream(path: Path) -> BinaryIO:
    """Open a layer blob and return a stream of the uncompressed tar."""
    f = open(path, "rb")
    head = f.read(4)
    f.seek(0)
    if head.startswith(GZIP_MAGIC):
        return gzip.GzipFile(fileobj=f)  # type: ignore[return-value]
    if head.startswith(ZSTD_MAGIC):
        try:
            import zstandard
        except ImportError:
            f.close()
            raise UnsupportedLayer(
                f"{path.name} is zstd-compressed; install the extra: pip install 'xcodon-runtime[zstd]'"
            ) from None
        return zstandard.ZstdDecompressor().stream_reader(f)  # type: ignore[return-value]
    return f


def _layer_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """The stdlib ``tar`` filter, plus owner read/write so later entries can land inside."""
    member = tarfile.tar_filter(member, dest)
    if member.isdir():
        return member.replace(mode=(member.mode or 0o755) | 0o700)
    if member.isreg():
        return member.replace(mode=(member.mode or 0o644) | 0o600)
    return member


def extract_layer(stream: BinaryIO, dest: Path) -> int:
    """Extract a layer tar into ``dest``. Returns how many entries were skipped.

    Ownership in the tar is ignored: every file belongs to the invoking user.
    Setuid, setgid, sticky, and group/other write bits are dropped by the filter.
    Device nodes, sockets, and fifos are skipped. Entries that would escape
    ``dest`` are refused by the filter and skipped.
    """
    if not hasattr(tarfile, "tar_filter"):
        raise XcodonError(
            "this Python's tarfile has no extraction filters; "
            "use Python 3.10.12+, 3.11.4+, or 3.12+"
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    skipped = 0
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.type in _SKIP_TYPES:
                log.debug("skip special file %s", member.name)
                skipped += 1
                continue
            try:
                tar.extract(member, dest, filter=_layer_filter)
            except tarfile.FilterError as e:
                log.warning("skip %s: %s", member.name, e)
                skipped += 1
            except (PermissionError, OSError) as e:
                log.warning("skip %s: %s", member.name, e)
                skipped += 1
    return skipped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tarlayer.py -q`
Expected: all PASS. If `test_extract_regular_files_symlinks_and_dirs` fails on the `ro/inside` assertion, the filter is not adding `0o700` to directories; check `_layer_filter`.

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/tarlayer.py tests/test_tarlayer.py
git commit -m "feat: unprivileged layer extraction with safe tar filter"
```

---

### Task 4: Flatten layers into one rootfs

**Files:**
- Create: `src/xcodon_runtime/flatten.py`
- Test: `tests/test_flatten.py`

**Interfaces:**
- Produces: `flatten.build_rootfs(layer_dirs: Sequence[Path], dest: Path) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_flatten.py
import os
from pathlib import Path

from xcodon_runtime.flatten import build_rootfs


def mk(root: Path, files: dict[str, str | None]):
    """files: path -> content; None means directory; 'LINK:target' means symlink."""
    for rel, content in files.items():
        p = root / rel
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
        elif isinstance(content, str) and content.startswith("LINK:"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(content[5:])
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return root


def test_whiteout_and_opaque(tmp_path):
    a = mk(tmp_path / "a", {"a.txt": "A", "dir/x": "x", "dir/y": "y", "keep": "k"})
    b = mk(tmp_path / "b", {".wh.a.txt": "", "dir/.wh.x": "", "new.txt": "N"})
    c = mk(tmp_path / "c", {"dir/.wh..wh..opq": "", "dir/z": "z"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b, c], out)
    assert not (out / "a.txt").exists()
    assert (out / "new.txt").read_text() == "N"
    assert (out / "keep").read_text() == "k"
    assert sorted(p.name for p in (out / "dir").iterdir()) == ["z"]
    assert not any(p.name.startswith(".wh.") for p in out.rglob("*"))


def test_file_replaces_dir_and_dir_replaces_file(tmp_path):
    a = mk(tmp_path / "a", {"thing/inner": "i", "other": "o"})
    b = mk(tmp_path / "b", {"thing": "now a file", "other/child": "c"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert (out / "thing").read_text() == "now a file"
    assert (out / "other/child").read_text() == "c"


def test_symlink_replaces_dir(tmp_path):
    a = mk(tmp_path / "a", {"lib64/f": "f", "usr/lib64/g": "g"})
    b = mk(tmp_path / "b", {"lib64": "LINK:usr/lib64"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert os.readlink(out / "lib64") == "usr/lib64"


def test_files_are_hardlinks_into_layers(tmp_path):
    a = mk(tmp_path / "a", {"big": "content"})
    out = tmp_path / "rootfs"
    build_rootfs([a], out)
    assert os.stat(out / "big").st_ino == os.stat(a / "big").st_ino


def test_later_layer_overrides_file(tmp_path):
    a = mk(tmp_path / "a", {"f": "old"})
    b = mk(tmp_path / "b", {"f": "new"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert (out / "f").read_text() == "new"
    assert (a / "f").read_text() == "old", "layers must never be modified"


def test_directory_mode_copied(tmp_path):
    a = mk(tmp_path / "a", {"d": None})
    os.chmod(a / "d", 0o750)
    out = tmp_path / "rootfs"
    build_rootfs([a], out)
    assert oct(os.stat(out / "d").st_mode & 0o777) == oct(0o750)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_flatten.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write flatten.py**

```python
# src/xcodon_runtime/flatten.py
"""Build one rootfs from ordered layer directories, applying OCI whiteouts."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path
from typing import Sequence

WHITEOUT_PREFIX = ".wh."
OPAQUE = ".wh..wh..opq"


def build_rootfs(layer_dirs: Sequence[Path], dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for layer in layer_dirs:
        _apply_dir(Path(layer), dest)


def _remove(path: Path) -> None:
    """Remove a file, symlink, or directory tree. Missing is fine."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _apply_dir(src: Path, dst: Path) -> None:
    entries = list(os.scandir(src))
    names = {e.name for e in entries}

    if OPAQUE in names:
        for child in os.scandir(dst):
            _remove(Path(child.path))

    for e in entries:
        if e.name != OPAQUE and e.name.startswith(WHITEOUT_PREFIX):
            _remove(dst / e.name[len(WHITEOUT_PREFIX) :])

    for e in entries:
        if e.name.startswith(WHITEOUT_PREFIX):
            continue
        target = dst / e.name
        if e.is_symlink():
            _remove(target)
            os.symlink(os.readlink(e.path), target)
        elif e.is_dir(follow_symlinks=False):
            try:
                existing = os.lstat(target)
                if not stat.S_ISDIR(existing.st_mode):
                    _remove(target)
                    target.mkdir()
            except FileNotFoundError:
                target.mkdir()
            os.chmod(target, stat.S_IMODE(e.stat(follow_symlinks=False).st_mode) | 0o700)
            _apply_dir(Path(e.path), target)
        else:
            _remove(target)
            try:
                os.link(e.path, target)
            except OSError as err:
                if err.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
                    shutil.copy2(e.path, target, follow_symlinks=False)
                else:
                    raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_flatten.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/flatten.py tests/test_flatten.py
git commit -m "feat: flatten layers into a hardlinked rootfs with whiteouts"
```

---
### Task 5: Registry client

**Files:**
- Create: `src/xcodon_runtime/registry.py`
- Create: `tests/fake_registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `home.RuntimeHome`, `reference.Reference`, `reference.Platform`, `errors.PullError`.
- Produces: `registry.FetchedLayer(digest: str, media_type: str, size: int, blob_path: Path)`, `registry.FetchedImage(config_digest: str, config: dict, layers: list[FetchedLayer], source: str)`, `registry.select_platform(manifests: list[dict], platform: Platform) -> str` (returns a digest), `registry.RegistryClient(home: RuntimeHome, scheme: str = "https", timeout: float = 60)` with `name = "registry"`, `fetch(ref: Reference, platform: Platform) -> FetchedImage`, `fetch_blob(ref: Reference, digest: str) -> Path`.
- Produces test helper `fake_registry.FakeRegistry` (context manager) with attributes `host` (e.g. `127.0.0.1:PORT`), `blob_host`, and method `add_image(repo, tag, config: dict, layers: list[bytes], multi_arch: bool = False)`.

- [ ] **Step 1: Write the fake registry test helper**

```python
# tests/fake_registry.py
"""An in-process registry that speaks enough of the v2 API for the client tests.

Behavior modeled on Docker Hub: manifests need a bearer token obtained from
/token; blob GETs redirect to a second server on another port that must NOT
receive the Authorization header.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}  # (repo, ref) -> (body, media type)
        self.requests: list[tuple[str, str, dict]] = []
        self.token = "test-token"

    def add_image(self, repo: str, tag: str, config: dict, layers: list[bytes], multi_arch: bool = False) -> str:
        config_bytes = json.dumps(config).encode()
        self.blobs[digest_of(config_bytes)] = config_bytes
        layer_descs = []
        for data in layers:
            self.blobs[digest_of(data)] = data
            layer_descs.append(
                {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": digest_of(data), "size": len(data)}
            )
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest_of(config_bytes), "size": len(config_bytes)},
            "layers": layer_descs,
        }
        mbytes = json.dumps(manifest).encode()
        self.manifests[(repo, digest_of(mbytes))] = (mbytes, manifest["mediaType"])
        if not multi_arch:
            self.manifests[(repo, tag)] = (mbytes, manifest["mediaType"])
            return digest_of(mbytes)
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "0" * 64, "size": 1, "platform": {"os": "linux", "architecture": "s390x"}},
                {"mediaType": manifest["mediaType"], "digest": digest_of(mbytes), "size": len(mbytes), "platform": {"os": "linux", "architecture": config["architecture"]}},
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "1" * 64, "size": 1, "platform": {"os": "unknown", "architecture": "unknown"}},
            ],
        }
        ibytes = json.dumps(index).encode()
        self.manifests[(repo, tag)] = (ibytes, index["mediaType"])
        return digest_of(mbytes)

    def __enter__(self):
        reg = self
        blob_server_ref: list[HTTPServer] = []

        class BlobHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_GET(self):
                reg.requests.append(("blob", self.path, dict(self.headers)))
                if "Authorization" in self.headers:
                    self.send_error(400, "Only one auth mechanism allowed")
                    return
                digest = self.path.rsplit("/", 1)[-1]
                data = reg.blobs.get(digest)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class ApiHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                reg.requests.append(("api", self.path, dict(self.headers)))
                if self.path.startswith("/token"):
                    body = json.dumps({"token": reg.token}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.headers.get("Authorization") != f"Bearer {reg.token}":
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Bearer realm="http://{reg.host}/token",service="fake",scope="repository:x:pull"',
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                parts = self.path.split("/")  # ['', 'v2', repo..., 'manifests'|'blobs', ref]
                kind, ref = parts[-2], parts[-1]
                repo = "/".join(parts[2:-2])
                if kind == "manifests":
                    hit = reg.manifests.get((repo, ref))
                    if hit is None:
                        self.send_error(404)
                        return
                    body, mt = hit
                    self.send_response(200)
                    self.send_header("Content-Type", mt)
                    self.send_header("Docker-Content-Digest", digest_of(body))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if kind == "blobs":
                    self.send_response(307)
                    self.send_header("Location", f"http://{reg.blob_host}/store/{ref}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_error(404)

        self._api = HTTPServer(("127.0.0.1", 0), ApiHandler)
        self._blob = HTTPServer(("127.0.0.1", 0), BlobHandler)
        blob_server_ref.append(self._blob)
        self.host = f"127.0.0.1:{self._api.server_address[1]}"
        self.blob_host = f"127.0.0.1:{self._blob.server_address[1]}"
        for srv in (self._api, self._blob):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._api.shutdown()
        self._blob.shutdown()
```

- [ ] **Step 2: Write the failing registry tests**

```python
# tests/test_registry.py
import gzip
import io
import json
import tarfile

import pytest

from tests.fake_registry import FakeRegistry, digest_of
from xcodon_runtime.errors import PullError
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import RegistryClient, select_platform


def layer_bytes(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    return gzip.compress(buf.getvalue())


def config_for(layers: list[bytes]) -> dict:
    diff_ids = []
    for gz in layers:
        diff_ids.append(digest_of(gzip.decompress(gz)))
    return {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/bin"], "Cmd": ["sh"]},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
    }


@pytest.fixture
def reg():
    with FakeRegistry() as r:
        yield r


def test_fetch_single_arch_image(home, reg):
    layers = [layer_bytes("a", b"A"), layer_bytes("b", b"B")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    client = RegistryClient(home, scheme="http")
    ref = Reference(reg.host, "lib/hello", "latest")
    fetched = client.fetch(ref, Platform("linux", "amd64"))
    assert fetched.source == "registry"
    assert fetched.config["os"] == "linux"
    assert [l.digest for l in fetched.layers] == [digest_of(x) for x in layers]
    for l in fetched.layers:
        assert l.blob_path.read_bytes() == reg.blobs[l.digest]
        assert l.blob_path.parent == home.blobs
    assert not list(home.blobs.glob("*.part"))


def test_token_challenge_is_followed_once_per_repo(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    client = RegistryClient(home, scheme="http")
    client.fetch(Reference(reg.host, "lib/hello", "latest"), Platform())
    token_calls = [r for r in reg.requests if r[0] == "api" and r[1].startswith("/token")]
    assert len(token_calls) == 1


def test_blob_redirect_drops_authorization(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/hello", "latest"), Platform())
    blob_reqs = [r for r in reg.requests if r[0] == "blob"]
    assert blob_reqs, "blob server was never hit"
    assert all("Authorization" not in r[2] for r in blob_reqs)


def test_multi_arch_index_selects_platform(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    client = RegistryClient(home, scheme="http")
    fetched = client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "amd64"))
    assert fetched.config["architecture"] == "amd64"
    with pytest.raises(PullError, match="no manifest for linux/riscv64"):
        client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "riscv64"))


def test_digest_mismatch_is_rejected(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/bad", "latest", config_for(layers), layers)
    real = digest_of(layers[0])
    reg.blobs[real] = b"tampered"
    with pytest.raises(PullError, match="digest mismatch"):
        RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/bad", "latest"), Platform())
    assert not list(home.blobs.glob("*.part"))


def test_missing_manifest_is_pull_error(home, reg):
    with pytest.raises(PullError, match="404"):
        RegistryClient(home, scheme="http").fetch(Reference(reg.host, "lib/none", "latest"), Platform())


def test_select_platform_variant_rules():
    manifests = [
        {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
    ]
    assert select_platform(manifests, Platform("linux", "arm64")) == "sha256:" + "a" * 64
    assert select_platform(manifests, Platform("linux", "arm64", "v8")) == "sha256:" + "a" * 64
    assert select_platform(manifests, Platform("linux", "amd64")) == "sha256:" + "b" * 64
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_registry.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.registry'`

- [ ] **Step 4: Write registry.py**

```python
# src/xcodon_runtime/registry.py
"""Anonymous pulls from an OCI / Docker v2 registry using only the standard library."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse

from xcodon_runtime.errors import PullError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, Reference

log = logging.getLogger(__name__)

MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPES = {MT_OCI_INDEX, MT_DOCKER_LIST}
MANIFEST_ACCEPT = ", ".join([MT_OCI_INDEX, MT_OCI_MANIFEST, MT_DOCKER_LIST, MT_DOCKER_MANIFEST])
CHUNK = 1 << 20


@dataclass
class FetchedLayer:
    digest: str
    media_type: str
    size: int
    blob_path: Path


@dataclass
class FetchedImage:
    config_digest: str
    config: dict
    layers: list[FetchedLayer] = field(default_factory=list)
    source: str = "registry"


def select_platform(manifests: list[dict], platform: Platform) -> str:
    """Pick the manifest digest for ``platform`` from an index. Raises PullError if none."""
    for m in manifests:
        p = m.get("platform") or {}
        if p.get("os") != platform.os or p.get("architecture") != platform.architecture:
            continue
        if platform.variant and p.get("variant") not in (None, platform.variant):
            continue
        return m["digest"]
    available = ", ".join(
        f"{(m.get('platform') or {}).get('os')}/{(m.get('platform') or {}).get('architecture')}" for m in manifests
    )
    raise PullError(f"no manifest for {platform}; available: {available}")


class _StripAuthOnCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Registries redirect blob downloads to object storage that rejects our bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class RegistryClient:
    name = "registry"

    def __init__(self, home: RuntimeHome, scheme: str = "https", timeout: float = 60) -> None:
        self.home = home
        self.scheme = scheme
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_StripAuthOnCrossHostRedirect())
        self._tokens: dict[tuple[str, str], str] = {}

    # -- HTTP plumbing -------------------------------------------------------------

    def _open(self, url: str, headers: dict[str, str], token: str | None):
        req = urllib.request.Request(url, headers=dict(headers))
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        return self._opener.open(req, timeout=self.timeout)

    def _fetch_token(self, challenge: str) -> str:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise PullError(f"unsupported auth scheme {scheme!r}; only anonymous bearer tokens are supported")
        kv = dict(re.findall(r'(\w+)="([^"]*)"', params))
        if "realm" not in kv:
            raise PullError(f"malformed auth challenge: {challenge}")
        query = {k: kv[k] for k in ("service", "scope") if k in kv}
        url = kv["realm"] + ("?" + urlencode(query) if query else "")
        try:
            with self._opener.open(urllib.request.Request(url), timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise PullError(f"token request failed: HTTP {e.code} from {url}") from e
        except urllib.error.URLError as e:
            raise PullError(f"token request failed: {e.reason}") from e
        token = data.get("token") or data.get("access_token")
        if not token:
            raise PullError(f"token response from {url} has no token")
        return token

    def _get(self, ref: Reference, path: str, accept: str | None = None):
        url = f"{self.scheme}://{ref.api_host}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept} if accept else {}
        key = (ref.api_host, ref.repository)
        try:
            return self._open(url, headers, self._tokens.get(key))
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate") if e.code == 401 else None
            if not challenge:
                raise PullError(f"{url}: HTTP {e.code} {e.reason}") from e
            self._tokens[key] = self._fetch_token(challenge)
        except urllib.error.URLError as e:
            raise PullError(f"{url}: {e.reason}") from e
        try:
            return self._open(url, headers, self._tokens[key])
        except urllib.error.HTTPError as e:
            raise PullError(f"{url}: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise PullError(f"{url}: {e.reason}") from e

    # -- public API ----------------------------------------------------------------

    def _manifest(self, ref: Reference, manifest_ref: str) -> dict:
        with self._get(ref, f"manifests/{manifest_ref}", MANIFEST_ACCEPT) as r:
            body = r.read()
            media_type = r.headers.get("Content-Type", "").split(";")[0].strip()
        data = json.loads(body)
        data.setdefault("mediaType", media_type)
        return data

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        manifest = self._manifest(ref, ref.manifest_ref)
        if manifest.get("mediaType") in INDEX_TYPES or "manifests" in manifest:
            digest = select_platform(manifest["manifests"], platform)
            manifest = self._manifest(ref, digest)
        if "config" not in manifest or "layers" not in manifest:
            raise PullError(f"{ref.name}: unsupported manifest type {manifest.get('mediaType')!r} (schema v1 is not supported)")
        config_digest = manifest["config"]["digest"]
        config = json.loads(self.fetch_blob(ref, config_digest).read_text())
        layers = [
            FetchedLayer(l["digest"], l.get("mediaType", ""), int(l.get("size", 0)), self.fetch_blob(ref, l["digest"]))
            for l in manifest["layers"]
        ]
        return FetchedImage(config_digest, config, layers, source=self.name)

    def fetch_blob(self, ref: Reference, digest: str) -> Path:
        """Download a blob to the home blob store, verifying its digest. Idempotent."""
        algo, _, hexdigest = digest.partition(":")
        if algo != "sha256":
            raise PullError(f"unsupported digest algorithm {algo!r}")
        final = self.home.blobs / hexdigest
        if final.exists():
            return final
        part = final.with_name(hexdigest + ".part")
        h = hashlib.sha256()
        log.info("downloading %s", digest[:19])
        with self._get(ref, f"blobs/{digest}") as r, open(part, "wb") as out:
            for chunk in iter(lambda: r.read(CHUNK), b""):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != hexdigest:
            part.unlink(missing_ok=True)
            raise PullError(f"digest mismatch for {digest}: got sha256:{h.hexdigest()}")
        os.replace(part, final)
        return final
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_registry.py -q`
Expected: all PASS. If `test_blob_redirect_drops_authorization` fails with a 400, check that `remove_header("Authorization")` is called on the redirected request; `urllib` capitalizes header names, so `"Authorization"` is the right key.

- [ ] **Step 6: Commit**

```bash
git add src/xcodon_runtime/registry.py tests/fake_registry.py tests/test_registry.py
git commit -m "feat: stdlib registry client with anonymous token auth and blob verification"
```

---

### Task 6: Docker daemon source (docker save, OCI layout)

**Files:**
- Create: `src/xcodon_runtime/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `registry.FetchedImage`, `registry.FetchedLayer`, `registry.select_platform`, `registry.INDEX_TYPES`, `home.RuntimeHome`, `reference.Reference`, `reference.Platform`, `errors.PullError`.
- Produces: `daemon.load_oci_layout_tar(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> FetchedImage`, `daemon.DaemonSource(home: RuntimeHome, docker: str = "docker")` with `name = "daemon"`, `available() -> bool`, `has_image(ref: Reference) -> bool`, `fetch(ref: Reference, platform: Platform) -> FetchedImage`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_daemon.py
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from xcodon_runtime.daemon import DaemonSource, load_oci_layout_tar
from xcodon_runtime.errors import PullError
from xcodon_runtime.reference import Platform, Reference


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def layer_tar(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        i = tarfile.TarInfo(name)
        i.size = len(content)
        t.addfile(i, io.BytesIO(content))
    return buf.getvalue()


def oci_layout_tar(nested_index: bool = True) -> tuple[bytes, dict]:
    """Build what `docker save` emits: index.json, oci-layout, manifest.json, blobs/sha256/*."""
    layers = [layer_tar("a", b"A"), layer_tar("b", b"B")]
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Cmd": ["sh"], "Env": ["PATH=/bin"]},
        "rootfs": {"type": "layers", "diff_ids": [f"sha256:{sha(l)}" for l in layers]},
    }
    cbytes = json.dumps(config).encode()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": f"sha256:{sha(cbytes)}", "size": len(cbytes)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": f"sha256:{sha(l)}", "size": len(l)} for l in layers],
    }
    mbytes = json.dumps(manifest).encode()
    blobs = {sha(cbytes): cbytes, sha(mbytes): mbytes}
    for l in layers:
        blobs[sha(l)] = l
    entry = {"mediaType": manifest["mediaType"], "digest": f"sha256:{sha(mbytes)}", "size": len(mbytes)}
    if nested_index:
        inner = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {**entry, "platform": {"os": "linux", "architecture": "amd64"}},
                {"mediaType": manifest["mediaType"], "digest": "sha256:" + "f" * 64, "size": 1, "platform": {"os": "unknown", "architecture": "unknown"}},
            ],
        }
        ibytes = json.dumps(inner).encode()
        blobs[sha(ibytes)] = ibytes
        entry = {"mediaType": inner["mediaType"], "digest": f"sha256:{sha(ibytes)}", "size": len(ibytes)}
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [entry]}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        def add(name, data):
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
        add("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add("manifest.json", b"[]")
        for hexd, data in blobs.items():
            add(f"blobs/sha256/{hexd}", data)
        add("index.json", json.dumps(index).encode())
    return buf.getvalue(), config


@pytest.mark.parametrize("nested", [True, False])
def test_load_oci_layout(home, nested):
    data, config = oci_layout_tar(nested_index=nested)
    fetched = load_oci_layout_tar(io.BytesIO(data), home, Platform("linux", "amd64"))
    assert fetched.source == "daemon"
    assert fetched.config == config
    assert len(fetched.layers) == 2
    assert fetched.layers[0].media_type == "application/vnd.oci.image.layer.v1.tar"
    for l in fetched.layers:
        assert l.blob_path.exists()
        assert sha(l.blob_path.read_bytes()) == l.digest.split(":")[1]


def test_load_rejects_corrupt_blob(home):
    data, _ = oci_layout_tar()
    # flip a byte inside the first blob's content
    buf = io.BytesIO(data)
    out = io.BytesIO()
    with tarfile.open(fileobj=buf) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src:
            content = src.extractfile(m).read() if m.isfile() else None
            if m.name.startswith("blobs/") and content and content.startswith(b"{") is False and m.size > 100:
                content = b"X" + content[1:]
            if content is not None:
                m.size = len(content)
                dst.addfile(m, io.BytesIO(content))
            else:
                dst.addfile(m)
    with pytest.raises(PullError, match="digest mismatch"):
        load_oci_layout_tar(io.BytesIO(out.getvalue()), home, Platform())


def test_daemon_source_uses_fake_docker(home, tmp_path, monkeypatch):
    data, config = oci_layout_tar()
    tarfile_path = tmp_path / "img.tar"
    tarfile_path.write_bytes(data)
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  version) echo 29.0.0; exit 0;;\n"
        "  image) [ \"$3\" = docker.io/library/present:latest ] && exit 0 || exit 1;;\n"
        f"  save) cat {tarfile_path}; exit 0;;\n"
        "esac\nexit 2\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    src = DaemonSource(home, docker=str(fake))
    assert src.available()
    assert src.has_image(Reference("docker.io", "library/present", "latest"))
    assert not src.has_image(Reference("docker.io", "library/absent", "latest"))
    fetched = src.fetch(Reference("docker.io", "library/present", "latest"), Platform())
    assert fetched.config == config


def test_daemon_source_unavailable_when_missing(home):
    assert not DaemonSource(home, docker="/nonexistent/docker").available()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_daemon.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write daemon.py**

```python
# src/xcodon_runtime/daemon.py
"""Reuse images from a local Docker daemon through `docker save` (OCI layout)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import BinaryIO

from xcodon_runtime.errors import PullError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import INDEX_TYPES, FetchedImage, FetchedLayer, select_platform

log = logging.getLogger(__name__)
CHUNK = 1 << 20


def _store_blob(tar: tarfile.TarFile, member: tarfile.TarInfo, home: RuntimeHome) -> None:
    hexdigest = member.name.rsplit("/", 1)[-1]
    final = home.blobs / hexdigest
    if final.exists():
        return
    part = final.with_name(hexdigest + ".part")
    src = tar.extractfile(member)
    if src is None:
        return
    h = hashlib.sha256()
    with open(part, "wb") as out:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != hexdigest:
        part.unlink(missing_ok=True)
        raise PullError(f"digest mismatch for blob {hexdigest[:12]} in docker save output")
    os.replace(part, final)


def _read_json_blob(home: RuntimeHome, digest: str) -> dict:
    path = home.blobs / digest.split(":", 1)[1]
    if not path.exists():
        raise PullError(f"docker save output references missing blob {digest}")
    return json.loads(path.read_text())


def load_oci_layout_tar(stream: BinaryIO, home: RuntimeHome, platform: Platform) -> FetchedImage:
    """Read a `docker save` archive from a stream. Blobs land in the home blob store."""
    index: dict | None = None
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.name in ("index.json", "./index.json"):
                f = tar.extractfile(member)
                index = json.load(f) if f else None
            elif member.isfile() and "blobs/sha256/" in member.name:
                _store_blob(tar, member, home)
    if index is None:
        raise PullError("docker save output has no index.json; is this Docker 25 or newer?")

    manifests = index.get("manifests") or []
    if not manifests:
        raise PullError("docker save index.json lists no manifests")
    if len(manifests) == 1:
        desc = manifests[0]
    else:
        desc = {"digest": select_platform(manifests, platform)}
    doc = _read_json_blob(home, desc["digest"])
    if doc.get("mediaType") in INDEX_TYPES or "manifests" in doc:
        doc = _read_json_blob(home, select_platform(doc["manifests"], platform))
    if "config" not in doc or "layers" not in doc:
        raise PullError("docker save output has no usable image manifest")

    config_digest = doc["config"]["digest"]
    config = _read_json_blob(home, config_digest)
    layers = [
        FetchedLayer(
            l["digest"],
            l.get("mediaType", "application/vnd.oci.image.layer.v1.tar"),
            int(l.get("size", 0)),
            home.blobs / l["digest"].split(":", 1)[1],
        )
        for l in doc["layers"]
    ]
    for l in layers:
        if not l.blob_path.exists():
            raise PullError(f"docker save output is missing layer {l.digest}")
    return FetchedImage(config_digest, config, layers, source="daemon")


class DaemonSource:
    name = "daemon"

    def __init__(self, home: RuntimeHome, docker: str = "docker") -> None:
        self.home = home
        self.docker = docker

    def available(self) -> bool:
        exe = shutil.which(self.docker) if not os.path.isabs(self.docker) else self.docker
        if not exe or not os.access(exe, os.X_OK):
            return False
        try:
            r = subprocess.run([exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    def has_image(self, ref: Reference) -> bool:
        r = subprocess.run([self.docker, "image", "inspect", ref.name], capture_output=True, timeout=30)
        return r.returncode == 0

    def fetch(self, ref: Reference, platform: Platform) -> FetchedImage:
        log.info("exporting %s from the local docker daemon", ref.name)
        proc = subprocess.Popen([self.docker, "save", ref.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            fetched = load_oci_layout_tar(proc.stdout, self.home, platform)
        finally:
            proc.stdout.close()
            _, err = proc.communicate()
        if proc.returncode != 0:
            raise PullError(f"docker save {ref.name} failed: {err.decode(errors='replace').strip()}")
        return fetched
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_daemon.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/daemon.py tests/test_daemon.py
git commit -m "feat: load images from a local docker daemon via docker save"
```

---

### Task 7: ImageStore — pull orchestration, refs, inspect, prune

**Files:**
- Create: `src/xcodon_runtime/imagestore.py`
- Test: `tests/test_imagestore.py`

**Interfaces:**
- Consumes: `home.RuntimeHome`, `reference.parse_reference`, `reference.host_platform`, `registry.RegistryClient`, `registry.FetchedImage`, `daemon.DaemonSource`, `tarlayer.open_layer_stream`, `tarlayer.extract_layer`, `flatten.build_rootfs`, `errors.ImageNotFound`, `errors.PullError`.
- Produces: `imagestore.Image(id: str, dir: Path, config: dict, refs: list[str])` with property `rootfs -> Path` and `short_id -> str`; `imagestore.ImageStore(home: RuntimeHome, sources: list | None = None)` with `pull(ref: str, platform: Platform | None = None) -> Image`, `get(ref_or_id: str) -> Image | None`, `require(ref_or_id: str) -> Image` (raises ImageNotFound), `images() -> list[Image]`, `remove(ref_or_id: str) -> None`, `inspect(ref_or_id: str) -> list[dict]`, `prune() -> list[Path]`, `import_fetched(fetched: FetchedImage, ref_name: str) -> Image`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_imagestore.py
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from xcodon_runtime.errors import ImageNotFound, PullError
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform
from xcodon_runtime.registry import FetchedImage, FetchedLayer


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def layer(home, files: dict[str, bytes]) -> tuple[FetchedLayer, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, content in files.items():
            i = tarfile.TarInfo(name)
            i.size = len(content)
            t.addfile(i, io.BytesIO(content))
    raw = buf.getvalue()
    gz = gzip.compress(raw)
    path = home.blobs / sha(gz).split(":")[1]
    path.write_bytes(gz)
    return FetchedLayer(sha(gz), "application/vnd.oci.image.layer.v1.tar+gzip", len(gz), path), sha(raw)


class FakeSource:
    name = "fake"

    def __init__(self, home, layers_spec):
        self.home = home
        self.layers_spec = layers_spec
        self.calls = 0

    def fetch(self, ref, platform):
        self.calls += 1
        layers, diff_ids = [], []
        for spec in self.layers_spec:
            l, diff = layer(self.home, spec)
            layers.append(l)
            diff_ids.append(diff)
        config = {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Env": ["PATH=/bin"], "Cmd": ["/bin/sh"], "WorkingDir": "/w"},
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }
        cbytes = json.dumps(config).encode()
        (self.home.blobs / sha(cbytes).split(":")[1]).write_bytes(cbytes)
        return FetchedImage(sha(cbytes), config, layers, source="fake")


@pytest.fixture
def store(home):
    src = FakeSource(home, [{"a": b"A", "dir/x": b"x"}, {".wh.a": b"", "b": b"B"}])
    return ImageStore(home, sources=[src]), src


def test_pull_builds_rootfs_and_ref(store):
    st, src = store
    img = st.pull("example/app:v1")
    assert img.refs == ["docker.io/example/app:v1"]
    assert (img.rootfs / "b").read_bytes() == b"B"
    assert not (img.rootfs / "a").exists()
    assert (img.rootfs / "dir/x").read_bytes() == b"x"
    assert img.config["config"]["WorkingDir"] == "/w"
    assert len(list(st.home.layers.iterdir())) == 2
    assert not list(st.home.blobs.iterdir()), "blobs are deleted after import"


def test_second_pull_of_same_image_hits_store_only_on_get(store):
    st, src = store
    st.pull("example/app:v1")
    assert st.get("example/app:v1") is not None
    assert src.calls == 1
    st.pull("example/app:v1")
    assert src.calls == 2, "pull always fetches; get is the cache"


def test_get_by_id_prefix(store):
    st, _ = store
    img = st.pull("example/app:v1")
    assert st.get(img.id[:12]).id == img.id
    assert st.get("nonexistent:latest") is None
    with pytest.raises(ImageNotFound):
        st.require("nonexistent:latest")


def test_inspect_shape(store):
    st, _ = store
    img = st.pull("example/app:v1")
    doc = st.inspect("example/app:v1")
    assert isinstance(doc, list) and len(doc) == 1
    assert doc[0]["Id"] == f"sha256:{img.id}"
    assert doc[0]["RepoTags"] == ["docker.io/example/app:v1"]
    assert doc[0]["Config"]["Cmd"] == ["/bin/sh"]
    assert doc[0]["RootFS"]["Type"] == "layers"
    assert doc[0]["Architecture"] == "amd64"


def test_remove_and_prune(store):
    st, _ = store
    img = st.pull("example/app:v1")
    st.remove("example/app:v1")
    assert st.get("example/app:v1") is None
    assert not img.dir.exists()
    assert len(list(st.home.layers.iterdir())) == 2
    pruned = st.prune()
    assert len(pruned) == 2
    assert not list(st.home.layers.iterdir())


def test_two_tags_share_one_image(store):
    st, _ = store
    a = st.pull("example/app:v1")
    b = st.pull("example/app:latest")
    assert a.id == b.id
    assert sorted(st.get(a.id).refs) == ["docker.io/example/app:latest", "docker.io/example/app:v1"]
    st.remove("example/app:v1")
    assert st.get("example/app:latest") is not None
    assert a.dir.exists()


def test_all_sources_fail(home):
    class Failing:
        name = "failing"

        def fetch(self, ref, platform):
            raise PullError("nope")

    st = ImageStore(home, sources=[Failing()])
    with pytest.raises(PullError, match="failing: nope"):
        st.pull("x/y")


def test_layer_count_mismatch_is_error(home):
    class Bad(FakeSource):
        def fetch(self, ref, platform):
            f = super().fetch(ref, platform)
            f.config["rootfs"]["diff_ids"].append("sha256:" + "0" * 64)
            return f

    st = ImageStore(home, sources=[Bad(home, [{"a": b"A"}])])
    with pytest.raises(PullError, match="diff_ids"):
        st.pull("x/y")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_imagestore.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write imagestore.py**

```python
# src/xcodon_runtime/imagestore.py
"""Stored images: pull from sources, extract layers once, flatten, track refs."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.errors import ImageNotFound, PullError
from xcodon_runtime.flatten import build_rootfs
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.reference import Platform, host_platform, parse_reference
from xcodon_runtime.registry import FetchedImage, RegistryClient
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream

log = logging.getLogger(__name__)


@dataclass
class Image:
    id: str
    dir: Path
    config: dict
    refs: list[str]

    @property
    def rootfs(self) -> Path:
        return self.dir / "rootfs"

    @property
    def short_id(self) -> str:
        return self.id[:12]


class ImageStore:
    def __init__(self, home: RuntimeHome, sources: list | None = None) -> None:
        self.home = home
        self.sources = sources if sources is not None else [DaemonSource(home), RegistryClient(home)]

    # -- lookup ------------------------------------------------------------------

    def _load(self, image_id: str) -> Image:
        d = self.home.images / image_id
        config = json.loads((d / "config.json").read_text())
        refs = sorted(name for name, iid in self.home.read_refs().items() if iid == image_id)
        return Image(image_id, d, config, refs)

    def get(self, ref_or_id: str) -> Image | None:
        refs = self.home.read_refs()
        try:
            name = parse_reference(ref_or_id).name
        except ValueError:
            name = None
        if name in refs and (self.home.images / refs[name]).exists():
            return self._load(refs[name])
        candidate = ref_or_id.removeprefix("sha256:")
        matches = [p.name for p in self.home.images.iterdir() if p.name.startswith(candidate) and not p.name.endswith(".tmp")]
        if len(matches) == 1 and len(candidate) >= 4:
            return self._load(matches[0])
        return None

    def require(self, ref_or_id: str) -> Image:
        img = self.get(ref_or_id)
        if img is None:
            raise ImageNotFound(f"image {ref_or_id!r} is not in the local store; run: xcodon pull {ref_or_id}")
        return img

    def images(self) -> list[Image]:
        return [self._load(p.name) for p in sorted(self.home.images.iterdir()) if not p.name.endswith(".tmp")]

    # -- pull --------------------------------------------------------------------

    def pull(self, ref: str, platform: Platform | None = None) -> Image:
        reference = parse_reference(ref)
        platform = platform or host_platform()
        lock_name = "pull-" + hashlib.sha256(reference.name.encode()).hexdigest()[:16]
        with self.home.lock(lock_name):
            errors: list[str] = []
            for source in self.sources:
                if isinstance(source, DaemonSource):
                    if not source.available() or not source.has_image(reference):
                        continue
                try:
                    fetched = source.fetch(reference, platform)
                    break
                except PullError as e:
                    errors.append(f"{source.name}: {e}")
            else:
                raise PullError(f"could not fetch {reference.name}: " + ("; ".join(errors) or "no source available"))
            return self.import_fetched(fetched, reference.name)

    def import_fetched(self, fetched: FetchedImage, ref_name: str) -> Image:
        image_id = fetched.config_digest.split(":", 1)[1]
        diff_ids = fetched.config.get("rootfs", {}).get("diff_ids", [])
        if len(diff_ids) != len(fetched.layers):
            raise PullError(f"config lists {len(diff_ids)} diff_ids but manifest has {len(fetched.layers)} layers")
        layer_dirs = [self._ensure_layer(diff_id, layer.blob_path) for diff_id, layer in zip(diff_ids, fetched.layers)]

        image_dir = self.home.images / image_id
        with self.home.lock(f"image-{image_id}"):
            if not image_dir.exists():
                with self.home.atomic_dir(image_dir) as tmp:
                    (tmp / "config.json").write_text(json.dumps(fetched.config, indent=2))
                    (tmp / "manifest.json").write_text(
                        json.dumps(
                            {
                                "config": fetched.config_digest,
                                "diff_ids": diff_ids,
                                "layers": [{"digest": l.digest, "mediaType": l.media_type, "size": l.size} for l in fetched.layers],
                                "source": fetched.source,
                            },
                            indent=2,
                        )
                    )
                    log.info("flattening %d layers for %s", len(layer_dirs), image_id[:12])
                    build_rootfs(layer_dirs, tmp / "rootfs")
        for layer in fetched.layers:
            layer.blob_path.unlink(missing_ok=True)
        (self.home.blobs / image_id).unlink(missing_ok=True)

        with self.home.lock("refs"):
            refs = self.home.read_refs()
            refs[ref_name] = image_id
            self.home.write_refs(refs)
        return self._load(image_id)

    def _ensure_layer(self, diff_id: str, blob_path: Path) -> Path:
        hexdigest = diff_id.split(":", 1)[1]
        dest = self.home.layers / hexdigest
        if dest.exists():
            return dest
        with self.home.lock(f"layer-{hexdigest}"):
            if dest.exists():
                return dest
            log.info("extracting layer %s", hexdigest[:12])
            with self.home.atomic_dir(dest) as tmp, open_layer_stream(blob_path) as stream:
                skipped = extract_layer(stream, tmp)
                if skipped:
                    log.info("layer %s: skipped %d special entries", hexdigest[:12], skipped)
        return dest

    # -- remove / inspect / prune ------------------------------------------------

    def remove(self, ref_or_id: str) -> None:
        img = self.require(ref_or_id)
        with self.home.lock("refs"):
            refs = self.home.read_refs()
            try:
                name = parse_reference(ref_or_id).name
            except ValueError:
                name = None
            if name in refs:
                del refs[name]
            else:
                for n in list(refs):
                    if refs[n] == img.id:
                        del refs[n]
            self.home.write_refs(refs)
            still_referenced = img.id in refs.values()
        if not still_referenced:
            with self.home.lock(f"image-{img.id}"):
                shutil.rmtree(img.dir, ignore_errors=True)

    def inspect(self, ref_or_id: str) -> list[dict]:
        img = self.get(ref_or_id)
        if img is None:
            return []
        manifest = json.loads((img.dir / "manifest.json").read_text())
        return [
            {
                "Id": f"sha256:{img.id}",
                "RepoTags": img.refs,
                "Architecture": img.config.get("architecture"),
                "Os": img.config.get("os"),
                "Created": img.config.get("created"),
                "Config": img.config.get("config", {}),
                "RootFS": {"Type": "layers", "Layers": manifest.get("diff_ids", [])},
                "XcodonSource": manifest.get("source"),
            }
        ]

    def prune(self) -> list[Path]:
        """Remove leftovers and layers no stored image references."""
        removed = self.home.prune_leftovers()
        used: set[str] = set()
        for img in self.images():
            manifest = json.loads((img.dir / "manifest.json").read_text())
            used.update(d.split(":", 1)[1] for d in manifest.get("diff_ids", []))
        for layer_dir in self.home.layers.iterdir():
            if layer_dir.name not in used:
                with self.home.lock(f"layer-{layer_dir.name}"):
                    shutil.rmtree(layer_dir, ignore_errors=True)
                removed.append(layer_dir)
        return removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_imagestore.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/imagestore.py tests/test_imagestore.py
git commit -m "feat: image store with pull orchestration, refs, inspect, and prune"
```

---

### Task 8: Process spec builder

**Files:**
- Create: `src/xcodon_runtime/spec.py`
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces: `spec.ProcessSpec(argv: list[str], env: dict[str, str], workdir: str, uid: int, gid: int)`; `spec.build_spec(image_config: dict, rootfs: Path, container_id: str, command: Sequence[str] | None = None, entrypoint: Sequence[str] | None = None, env: Mapping[str, str] | None = None, workdir: str | None = None, user: str | None = None) -> ProcessSpec`; `spec.resolve_user(user: str | None, rootfs: Path) -> tuple[int, int]`; `spec.DEFAULT_PATH`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spec.py
from pathlib import Path

import pytest

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.spec import DEFAULT_PATH, build_spec, resolve_user


@pytest.fixture
def rootfs(tmp_path: Path) -> Path:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\nappuser:x:1001:2001:App:/srv/app:/bin/sh\n"
    )
    (tmp_path / "etc/group").write_text("root:x:0:\nappgrp:x:2001:\nstaff:x:50:\n")
    return tmp_path


CID = "abcdef1234567890" * 4


def test_argv_from_entrypoint_and_cmd(rootfs):
    cfg = {"Entrypoint": ["/bin/tool"], "Cmd": ["--help"]}
    assert build_spec({"config": cfg}, rootfs, CID).argv == ["/bin/tool", "--help"]
    assert build_spec({"config": cfg}, rootfs, CID, command=["run"]).argv == ["/bin/tool", "run"]
    assert build_spec({"config": cfg}, rootfs, CID, entrypoint=["/bin/sh"], command=["-c", "x"]).argv == ["/bin/sh", "-c", "x"]
    assert build_spec({"config": {"Cmd": ["sh"]}}, rootfs, CID, entrypoint=[]).argv == ["sh"]


def test_empty_argv_is_error(rootfs):
    with pytest.raises(XcodonError, match="no command"):
        build_spec({"config": {}}, rootfs, CID)


def test_env_layers(rootfs):
    cfg = {"Env": ["PATH=/usr/bin", "A=1"]}
    s = build_spec({"config": cfg}, rootfs, CID, command=["x"], env={"A": "2", "B": "3"})
    assert s.env["PATH"] == "/usr/bin"
    assert s.env["A"] == "2"
    assert s.env["B"] == "3"
    assert s.env["HOSTNAME"] == CID[:12]
    assert s.env["HOME"] == "/root"


def test_default_path_when_missing(rootfs):
    s = build_spec({"config": {}}, rootfs, CID, command=["x"])
    assert s.env["PATH"] == DEFAULT_PATH


def test_home_from_passwd_for_user(rootfs):
    s = build_spec({"config": {"User": "appuser"}}, rootfs, CID, command=["x"])
    assert (s.uid, s.gid) == (1001, 2001)
    assert s.env["HOME"] == "/srv/app"


def test_home_override_wins(rootfs):
    s = build_spec({"config": {"Env": ["HOME=/opt"]}}, rootfs, CID, command=["x"])
    assert s.env["HOME"] == "/opt"


def test_workdir_rules(rootfs):
    assert build_spec({"config": {}}, rootfs, CID, command=["x"]).workdir == "/"
    assert build_spec({"config": {"WorkingDir": "/w"}}, rootfs, CID, command=["x"]).workdir == "/w"
    assert build_spec({"config": {"WorkingDir": "/w"}}, rootfs, CID, command=["x"], workdir="/o").workdir == "/o"


@pytest.mark.parametrize(
    "user,expected",
    [
        (None, (0, 0)),
        ("", (0, 0)),
        ("root", (0, 0)),
        ("appuser", (1001, 2001)),
        ("appuser:staff", (1001, 50)),
        ("1001", (1001, 2001)),
        ("1001:50", (1001, 50)),
        ("4242", (4242, 4242)),
        ("4242:4343", (4242, 4343)),
    ],
)
def test_resolve_user(rootfs, user, expected):
    assert resolve_user(user, rootfs) == expected


def test_unknown_user_name_is_error(rootfs):
    with pytest.raises(XcodonError, match="unknown user"):
        resolve_user("nobody-here", rootfs)


def test_user_option_overrides_image(rootfs):
    s = build_spec({"config": {"User": "appuser"}}, rootfs, CID, command=["x"], user="0")
    assert (s.uid, s.gid) == (0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_spec.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write spec.py**

```python
# src/xcodon_runtime/spec.py
"""Merge image config and run options into the process to start."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from xcodon_runtime.errors import XcodonError

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class ProcessSpec:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/"
    uid: int = 0
    gid: int = 0


def _read_db(path: Path) -> list[list[str]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [line.split(":") for line in lines if line and not line.startswith("#")]


def _passwd_lookup(rootfs: Path, key: str) -> list[str] | None:
    for row in _read_db(rootfs / "etc" / "passwd"):
        if len(row) >= 7 and (row[0] == key or row[2] == key):
            return row
    return None


def _group_lookup(rootfs: Path, key: str) -> list[str] | None:
    for row in _read_db(rootfs / "etc" / "group"):
        if len(row) >= 3 and (row[0] == key or row[2] == key):
            return row
    return None


def resolve_user(user: str | None, rootfs: Path) -> tuple[int, int]:
    """Turn ``name``, ``uid``, ``name:group``, or ``uid:gid`` into numbers using the rootfs databases."""
    if not user:
        return 0, 0
    user_part, _, group_part = user.partition(":")
    pw = _passwd_lookup(rootfs, user_part)
    if pw is not None:
        uid, default_gid = int(pw[2]), int(pw[3])
    elif user_part.isdigit():
        uid, default_gid = int(user_part), int(user_part)
    else:
        raise XcodonError(f"unknown user {user_part!r} in image /etc/passwd")
    if not group_part:
        return uid, default_gid
    gr = _group_lookup(rootfs, group_part)
    if gr is not None:
        return uid, int(gr[2])
    if group_part.isdigit():
        return uid, int(group_part)
    raise XcodonError(f"unknown group {group_part!r} in image /etc/group")


def _home_for(uid: int, rootfs: Path) -> str:
    pw = _passwd_lookup(rootfs, str(uid))
    if pw is not None and pw[5]:
        return pw[5]
    return "/root" if uid == 0 else "/"


def build_spec(
    image_config: dict,
    rootfs: Path,
    container_id: str,
    command: Sequence[str] | None = None,
    entrypoint: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    user: str | None = None,
) -> ProcessSpec:
    cfg = image_config.get("config") or {}

    ep = list(entrypoint) if entrypoint is not None else list(cfg.get("Entrypoint") or [])
    cmd = list(command) if command is not None else list(cfg.get("Cmd") or [])
    if entrypoint is not None and command is None:
        cmd = []  # a new entrypoint discards the image CMD, as docker does
    argv = ep + cmd
    if not argv:
        raise XcodonError("no command: the image has no ENTRYPOINT or CMD and none was given")

    uid, gid = resolve_user(user if user is not None else cfg.get("User"), rootfs)

    merged: dict[str, str] = {}
    for item in cfg.get("Env") or []:
        k, _, v = item.partition("=")
        merged[k] = v
    merged["HOSTNAME"] = container_id[:12]
    merged.setdefault("HOME", _home_for(uid, rootfs))
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    merged.setdefault("PATH", DEFAULT_PATH)

    wd = workdir or cfg.get("WorkingDir") or "/"
    if not wd.startswith("/"):
        wd = "/" + wd
    return ProcessSpec(argv=argv, env=merged, workdir=wd, uid=uid, gid=gid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_spec.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/spec.py tests/test_spec.py
git commit -m "feat: process spec builder for entrypoint, env, workdir, and user"
```

---
### Task 9: System call wrappers, probes, and engine selection

**Files:**
- Create: `src/xcodon_runtime/syscalls.py`
- Create: `src/xcodon_runtime/probe.py`
- Create: `src/xcodon_runtime/engine.py`
- Test: `tests/test_syscalls.py`
- Test: `tests/test_engine_select.py`

**Interfaces:**
- Produces `syscalls`: constants `CLONE_NEWNS, CLONE_NEWUTS, CLONE_NEWPID, CLONE_NEWUSER, MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE, MNT_DETACH`; functions `unshare(flags: int)`, `mount(source: str | None, target: str, fstype: str | None, flags: int = 0, data: str | None = None)`, `umount2(target: str, flags: int = 0)`, `pivot_root(new_root: str, put_old: str)`, `setns(fd: int, nstype: int = 0)`, `sethostname(name: str)`, `set_no_new_privs()`, `write_id_maps(uid_inside: int, gid_inside: int, host_uid: int, host_gid: int)`, `parse_mount_flags(mountinfo_text: str, path: str) -> int`, `mount_flags_at(path: str) -> int`, `ensure_mountpoint(source: str, target: str)`, `bind_mount(source: str, target: str, readonly: bool = False)`, `remount_readonly(target: str)`. All raise `OSError` with errno on failure.
- Produces `probe.PROBE_NAMES = ("userns", "overlay", "pidns_proc")`, `probe.run_probes(home_path: Path | None = None) -> dict[str, dict]` where each value is `{"ok": bool, "error": str}`; module runnable as `python -m xcodon_runtime.probe NAME HOME`.
- Produces `engine.Bind(source: str, target: str, readonly: bool = False)` with `to_dict()` and `Bind.from_dict(d)`; `engine.Engine` Protocol with `name`, `start(container)`, `popen(container, argv, env, workdir, **popen_kwargs) -> subprocess.Popen`, `stop(container)`, `is_running(container) -> bool`; `engine.EngineChoice(name: str, probes: dict, reason: str)`; `engine.select_engine(home: RuntimeHome, override: str | None = None) -> EngineChoice`; `engine.get_engine(name: str) -> Engine`; `engine.ENGINE_NAMES = ("ns", "proot")`.

- [ ] **Step 1: Write the failing syscalls tests**

```python
# tests/test_syscalls.py
import os
import platform
import subprocess
import sys

import pytest

from xcodon_runtime import syscalls as sc

MOUNTINFO = """\
25 1 0:23 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc rw
40 25 0:35 / /proc/sys/fs/binfmt_misc rw,relatime shared:22 - autofs systemd-1 rw
99 1 8:1 / /mnt/my\\040disk ro,noatime shared:50 - ext4 /dev/sda1 ro
100 1 0:50 / /sys rw,nosuid,nodev,noexec,relatime shared:8 - sysfs sysfs rw
"""


def test_parse_mount_flags_reads_locked_flags():
    flags = sc.parse_mount_flags(MOUNTINFO, "/proc")
    assert flags & sc.MS_NOSUID and flags & sc.MS_NODEV and flags & sc.MS_NOEXEC
    assert not flags & sc.MS_RDONLY


def test_parse_mount_flags_unescapes_spaces_and_reads_ro():
    flags = sc.parse_mount_flags(MOUNTINFO, "/mnt/my disk")
    assert flags & sc.MS_RDONLY
    assert flags & sc.MS_NOATIME


def test_parse_mount_flags_unknown_path_is_zero():
    assert sc.parse_mount_flags(MOUNTINFO, "/nope") == 0


def test_mount_flags_at_real_root_returns_int():
    assert isinstance(sc.mount_flags_at("/"), int)


def test_pivot_root_syscall_number_known_for_this_machine():
    assert platform.machine() in sc.SYS_PIVOT_ROOT


def test_unshare_without_privilege_fails_cleanly_for_net():
    # CLONE_NEWNET without a user namespace needs CAP_SYS_ADMIN; we expect EPERM, not a crash.
    code = subprocess.run(
        [sys.executable, "-c", "from xcodon_runtime import syscalls as s\n"
         "try:\n s.unshare(0x40000000)\nexcept OSError as e:\n print(e.errno)"],
        capture_output=True, text=True,
    )
    assert code.stdout.strip() in {"1", ""}  # EPERM, or empty if the host allows it


def test_ensure_mountpoint_creates_dir_or_file(tmp_path):
    sc.ensure_mountpoint("/etc", str(tmp_path / "a/b/etc"))
    assert (tmp_path / "a/b/etc").is_dir()
    sc.ensure_mountpoint("/etc/hosts", str(tmp_path / "x/hosts"))
    assert (tmp_path / "x/hosts").is_file()
    (tmp_path / "y").mkdir()
    (tmp_path / "y/link").symlink_to("/nonexistent/target")
    sc.ensure_mountpoint("/etc/hosts", str(tmp_path / "y/link"))
    assert (tmp_path / "y/link").is_file() and not (tmp_path / "y/link").is_symlink()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_syscalls.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write syscalls.py**

```python
# src/xcodon_runtime/syscalls.py
"""Thin ctypes wrappers over the Linux calls the ns engine needs. Every failure is an OSError."""

from __future__ import annotations

import ctypes
import errno
import os
import platform

_libc = ctypes.CDLL(None, use_errno=True)

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWPID = 0x20000000
CLONE_NEWUSER = 0x10000000

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_NOATIME = 1024
MS_NODIRATIME = 2048
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_RELATIME = 1 << 21
MS_STRICTATIME = 1 << 24

MNT_DETACH = 2
PR_SET_NO_NEW_PRIVS = 38

SYS_PIVOT_ROOT = {"x86_64": 155, "aarch64": 41}

_OPTION_FLAGS = {
    "ro": MS_RDONLY,
    "nosuid": MS_NOSUID,
    "nodev": MS_NODEV,
    "noexec": MS_NOEXEC,
    "noatime": MS_NOATIME,
    "nodiratime": MS_NODIRATIME,
    "relatime": MS_RELATIME,
    "strictatime": MS_STRICTATIME,
}


def _b(value: str | None) -> bytes | None:
    return None if value is None else os.fsencode(value)


def _fail(what: str) -> None:
    err = ctypes.get_errno()
    raise OSError(err, f"{what}: {os.strerror(err)}")


def unshare(flags: int) -> None:
    if _libc.unshare(ctypes.c_int(flags)) != 0:
        _fail(f"unshare(0x{flags:x})")


def mount(source: str | None, target: str, fstype: str | None, flags: int = 0, data: str | None = None) -> None:
    rc = _libc.mount(_b(source), _b(target), _b(fstype), ctypes.c_ulong(flags), _b(data))
    if rc != 0:
        _fail(f"mount {source or ''} -> {target} ({fstype or 'bind'}, flags=0x{flags:x})")


def umount2(target: str, flags: int = 0) -> None:
    if _libc.umount2(_b(target), ctypes.c_int(flags)) != 0:
        _fail(f"umount {target}")


def pivot_root(new_root: str, put_old: str) -> None:
    nr = SYS_PIVOT_ROOT.get(platform.machine())
    if nr is None:
        raise OSError(errno.ENOSYS, f"pivot_root syscall number unknown for {platform.machine()}")
    if _libc.syscall(ctypes.c_long(nr), _b(new_root), _b(put_old)) != 0:
        _fail(f"pivot_root {new_root}")


def setns(fd: int, nstype: int = 0) -> None:
    if _libc.setns(ctypes.c_int(fd), ctypes.c_int(nstype)) != 0:
        _fail("setns")


def sethostname(name: str) -> None:
    raw = name.encode()
    if _libc.sethostname(raw, ctypes.c_size_t(len(raw))) != 0:
        _fail("sethostname")


def set_no_new_privs() -> None:
    if _libc.prctl(ctypes.c_int(PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), 0, 0, 0) != 0:
        _fail("prctl(PR_SET_NO_NEW_PRIVS)")


def write_id_maps(uid_inside: int, gid_inside: int, host_uid: int, host_gid: int) -> None:
    """Single-line self mapping. Needs no newuidmap and no capabilities."""
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny")
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"{uid_inside} {host_uid} 1")
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"{gid_inside} {host_gid} 1")


def _unescape(text: str) -> str:
    return text.encode("latin-1").decode("unicode_escape")


def parse_mount_flags(mountinfo_text: str, path: str) -> int:
    """Flags of the topmost mount at ``path`` according to a mountinfo listing."""
    flags = 0
    for line in mountinfo_text.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        if _unescape(fields[4]) == path:
            flags = sum(_OPTION_FLAGS.get(opt, 0) for opt in fields[5].split(","))
    return flags


def mount_flags_at(path: str) -> int:
    with open("/proc/self/mountinfo") as f:
        return parse_mount_flags(f.read(), path)


def ensure_mountpoint(source: str, target: str) -> None:
    """Make ``target`` a directory or an empty file to match ``source``. Replaces a symlink."""
    if os.path.islink(target):
        os.unlink(target)
    if os.path.isdir(source):
        os.makedirs(target, exist_ok=True)
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        open(target, "a").close()


def remount_readonly(target: str) -> None:
    """Inside a user namespace the remount must repeat the source's locked flags."""
    mount(None, target, None, MS_BIND | MS_REMOUNT | MS_RDONLY | mount_flags_at(target))


def bind_mount(source: str, target: str, readonly: bool = False) -> None:
    ensure_mountpoint(source, target)
    mount(source, target, None, MS_BIND | MS_REC)
    if readonly:
        remount_readonly(target)
```

- [ ] **Step 4: Run syscalls tests**

Run: `pytest tests/test_syscalls.py -q`
Expected: all PASS

- [ ] **Step 5: Write probe.py**

```python
# src/xcodon_runtime/probe.py
"""Engine probes. Each probe runs in its own child process so a failure cannot hurt the caller.

Usage as a child: python -m xcodon_runtime.probe NAME HOME_PATH
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from xcodon_runtime import syscalls as sc

PROBE_NAMES = ("userns", "overlay", "pidns_proc")


def _enter_userns() -> None:
    sc.unshare(sc.CLONE_NEWUSER | sc.CLONE_NEWNS)
    sc.write_id_maps(0, 0, os.getuid(), os.getgid())
    sc.mount(None, "/", None, sc.MS_REC | sc.MS_PRIVATE)


def _probe_userns(home: Path) -> None:
    _enter_userns()


def _probe_overlay(home: Path) -> None:
    _enter_userns()
    base = Path(tempfile.mkdtemp(prefix="probe-", dir=home))
    try:
        for d in ("lower", "upper", "work", "merged"):
            (base / d).mkdir()
        (base / "lower" / "f").write_text("x")
        sc.mount("overlay", str(base / "merged"), "overlay", 0,
                 f"lowerdir={base / 'lower'},upperdir={base / 'upper'},workdir={base / 'work'}")
        assert (base / "merged" / "f").read_text() == "x"
        sc.umount2(str(base / "merged"), sc.MNT_DETACH)
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


def _probe_pidns_proc(home: Path) -> None:
    _enter_userns()
    sc.unshare(sc.CLONE_NEWPID)
    pid = os.fork()
    if pid == 0:
        try:
            target = tempfile.mkdtemp(prefix="probe-proc-", dir=home)
            sc.mount("proc", target, "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
            ok = os.path.exists(os.path.join(target, "1"))
            sc.umount2(target, sc.MNT_DETACH)
            os.rmdir(target)
            os._exit(0 if ok else 3)
        except BaseException:
            traceback.print_exc()
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        raise OSError(f"proc mount inside a new pid namespace failed (child exit {code})")


_PROBES = {"userns": _probe_userns, "overlay": _probe_overlay, "pidns_proc": _probe_pidns_proc}


def run_probes(home_path: Path | None = None) -> dict[str, dict]:
    from xcodon_runtime.home import RuntimeHome

    home = RuntimeHome(home_path).path
    results: dict[str, dict] = {}
    for name in PROBE_NAMES:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "xcodon_runtime.probe", name, str(home)],
                capture_output=True, text=True, timeout=30,
            )
            err = (r.stderr.strip().splitlines() or [""])[-1] if r.returncode else ""
            results[name] = {"ok": r.returncode == 0, "error": err}
        except subprocess.TimeoutExpired:
            results[name] = {"ok": False, "error": "probe timed out"}
    return results


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in _PROBES:
        print(f"usage: python -m xcodon_runtime.probe {{{'|'.join(PROBE_NAMES)}}} HOME", file=sys.stderr)
        return 2
    try:
        _PROBES[argv[0]](Path(argv[1]))
    except BaseException as e:  # noqa: BLE001 — report anything, this is a probe
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Write engine.py**

```python
# src/xcodon_runtime/engine.py
"""Engine interface, bind mounts, and the choice between the ns and proot engines."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

from xcodon_runtime.errors import EngineUnavailable
from xcodon_runtime.home import RuntimeHome
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
    probes = run_probes(home.path)
    failed = [n for n in PROBE_NAMES if not probes[n]["ok"]]
    if not failed:
        return EngineChoice("ns", probes, "user namespaces, overlayfs, and pid namespaces all work")
    reason = "; ".join(f"{n}: {probes[n]['error'] or 'failed'}" for n in failed)
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
```

- [ ] **Step 7: Write the engine selection tests**

```python
# tests/test_engine_select.py
import pytest

from xcodon_runtime import engine as eng
from xcodon_runtime.errors import EngineUnavailable


def all_ok():
    return {n: {"ok": True, "error": ""} for n in eng.PROBE_NAMES}


def test_ns_when_all_probes_pass(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda p: all_ok())
    assert eng.select_engine(home).name == "ns"


def test_proot_when_a_probe_fails(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    probes = all_ok()
    probes["overlay"] = {"ok": False, "error": "overlay: Operation not permitted"}
    monkeypatch.setattr(eng, "run_probes", lambda p: probes)
    choice = eng.select_engine(home)
    assert choice.name == "proot"
    assert "overlay" in choice.reason


def test_override_env_and_arg(home, monkeypatch):
    monkeypatch.setattr(eng, "run_probes", lambda p: pytest.fail("probes must not run when overridden"))
    monkeypatch.setenv("XCODON_ENGINE", "proot")
    assert eng.select_engine(home).name == "proot"
    assert eng.select_engine(home, override="ns").name == "ns"
    monkeypatch.setenv("XCODON_ENGINE", "bogus")
    with pytest.raises(EngineUnavailable, match="bogus"):
        eng.select_engine(home)


def test_bind_round_trip():
    b = eng.Bind("/h", "/c", True)
    assert eng.Bind.from_dict(b.to_dict()) == b


@pytest.mark.ns
def test_real_probes_pass_on_ns_capable_host(home):
    from xcodon_runtime.probe import run_probes

    results = run_probes(home.path)
    assert all(r["ok"] for r in results.values()), results
```

- [ ] **Step 8: Run the tests**

Run: `pytest tests/test_syscalls.py tests/test_engine_select.py -q`
Expected: all PASS (the `ns`-marked test passes on this host and skips where namespaces are off). Also run `python -m xcodon_runtime.probe overlay /tmp && echo ok` and expect `ok`.

- [ ] **Step 9: Update conftest**

In `tests/conftest.py`, the `pytest_collection_modifyitems` import of `xcodon_runtime.probe.run_probes` now resolves. Remove the `try/except ImportError` guard around it if one was added in Task 2. Leave the `engine_proot` guard until Task 11.

- [ ] **Step 10: Commit**

```bash
git add src/xcodon_runtime/syscalls.py src/xcodon_runtime/probe.py src/xcodon_runtime/engine.py tests/test_syscalls.py tests/test_engine_select.py tests/conftest.py
git commit -m "feat: ctypes syscalls, engine probes, and engine selection"
```

---

### Task 10: Container record, ns keeper, nsexec, and NsEngine

**Files:**
- Create: `src/xcodon_runtime/containers.py` (the `Container` dataclass only; `ContainerStore` is added in Task 12)
- Create: `src/xcodon_runtime/keeper.py`
- Create: `src/xcodon_runtime/nsexec.py`
- Create: `src/xcodon_runtime/engine_ns.py`
- Test: `tests/test_engine_ns.py`

**Interfaces:**
- Consumes: `syscalls.*`, `engine.Bind`, `errors.ContainerNotRunning`, `errors.EngineUnavailable`.
- Produces `containers.Container` dataclass with fields `id: str, image_id: str, image_ref: str, image_rootfs: str, engine: str, argv: list[str], env: dict[str, str], workdir: str, uid: int, gid: int, binds: list[Bind], created: str, name: str | None = None, state: str = "created", dir: Path` (dir is not serialized); methods `save() -> None`, `Container.load(dir: Path) -> Container`, property `short_id`.
- Produces `keeper.main(argv)` (runnable as `python -m xcodon_runtime.keeper PLAN_JSON INFO_FD`), `keeper.KEEPER_PLAN = "keeper-plan.json"`, `keeper.KEEPER_LOG = "keeper.log"`.
- Produces `nsexec.main(argv)` (runnable as `python -m xcodon_runtime.nsexec PID WORKDIR ENV_JSON -- ARGV...`).
- Produces `engine_ns.NsEngine` implementing `Engine`; `engine_ns.KEEPER_PID = "keeper.pid"`; `engine_ns.process_start_time(pid: int) -> str | None`.

- [ ] **Step 1: Write containers.py (Container record)**

```python
# src/xcodon_runtime/containers.py
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
```

- [ ] **Step 2: Write the failing ns engine tests**

```python
# tests/test_engine_ns.py
"""Integration tests for the ns engine. All marked `ns`; skipped where namespaces are off."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind
from xcodon_runtime.engine_ns import KEEPER_PID, NsEngine
from xcodon_runtime.errors import ContainerNotRunning

pytestmark = pytest.mark.ns


def make_container(home, rootfs: Path, uid=0, gid=0, binds=(), workdir="/") -> Container:
    cid = os.urandom(32).hex()
    cdir = home.containers / cid
    cdir.mkdir()
    c = Container(
        id=cid, image_id="img", image_ref="test/bb:latest", image_rootfs=str(rootfs), engine="ns",
        argv=["/bin/sh"], env={"PATH": "/bin", "HOME": "/root"}, workdir=workdir, uid=uid, gid=gid,
        binds=list(binds), created=datetime.now(timezone.utc).isoformat(), dir=cdir,
    )
    c.save()
    return c


def sh(engine, c, script, **kw) -> subprocess.CompletedProcess:
    p = engine.popen(c, ["/bin/sh", "-c", script], c.env, c.workdir,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    out, err = p.communicate(timeout=30)
    return subprocess.CompletedProcess(p.args, p.returncode, out.decode(), err.decode())


@pytest.fixture
def engine():
    return NsEngine()


@pytest.fixture
def running(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    engine.start(c)
    yield engine, c
    engine.stop(c)


def test_start_records_live_keeper(running):
    engine, c = running
    assert engine.is_running(c)
    pidinfo = json.loads((c.dir / KEEPER_PID).read_text())
    assert Path(f"/proc/{pidinfo['pid']}").exists()


def test_sandbox_identity(running):
    engine, c = running
    r = sh(engine, c, "echo host=$(hostname) uid=$(id -u) pid=$$ home=$HOME")
    assert r.returncode == 0, r.stderr
    assert f"host={c.short_id}" in r.stdout
    assert "uid=0" in r.stdout
    assert "home=/root" in r.stdout
    pid = int(r.stdout.split("pid=")[1].split()[0])
    assert pid < 100, "must be inside the new pid namespace"


def test_dev_is_minimal_and_proc_is_ours(running):
    engine, c = running
    r = sh(engine, c, "ls /dev | tr '\\n' ' '; echo; ls /proc | head -3 | tr '\\n' ' '")
    devs = set(r.stdout.splitlines()[0].split())
    assert {"null", "zero", "urandom", "pts", "ptmx", "shm", "stdin", "stdout", "stderr"} <= devs
    assert "sda" not in devs and "nvme0" not in devs
    assert "1" in r.stdout.splitlines()[1].split()


def test_writes_persist_in_upper_and_across_restart(running, engine):
    engine, c = running
    assert sh(engine, c, "echo hello > /persist.txt").returncode == 0
    assert (c.dir / "upper" / "persist.txt").read_text() == "hello\n"
    engine.stop(c)
    assert not engine.is_running(c)
    engine.start(c)
    assert sh(engine, c, "cat /persist.txt").stdout == "hello\n"


def test_exit_code_and_stdin_passthrough(running):
    engine, c = running
    assert sh(engine, c, "exit 7").returncode == 7
    p = engine.popen(c, ["/bin/cat"], c.env, "/", stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(b"from stdin", timeout=30)
    assert out == b"from stdin"


def test_command_not_found_is_127(running):
    engine, c = running
    p = engine.popen(c, ["/no/such/binary"], c.env, "/", stderr=subprocess.PIPE)
    _, err = p.communicate(timeout=30)
    assert p.returncode == 127
    assert b"not found" in err


def test_sys_is_read_only_and_hosts_visible(running):
    engine, c = running
    r = sh(engine, c, "touch /sys/x 2>&1; head -c 9 /etc/hosts")
    assert "Read-only" in r.stdout
    assert "127.0.0.1" in r.stdout


def test_binds_rw_and_ro(home, busybox_rootfs, engine, tmp_path):
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    rw.mkdir()
    ro.mkdir()
    (ro / "f").write_text("ro-content")
    c = make_container(home, busybox_rootfs, binds=[Bind(str(rw), "/data"), Bind(str(ro), "/rodata", readonly=True)])
    engine.start(c)
    try:
        r = sh(engine, c, "echo w > /data/out; cat /rodata/f; touch /rodata/x 2>&1")
        assert (rw / "out").read_text() == "w\n"
        assert "ro-content" in r.stdout
        assert "Read-only" in r.stdout
        assert os.stat(rw / "out").st_uid == os.getuid()
    finally:
        engine.stop(c)


def test_non_root_uid_mapping(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs, uid=1000, gid=1000)
    engine.start(c)
    try:
        r = sh(engine, c, "id -u; id -g; echo ok > /home/user/f && echo wrote")
        assert r.stdout.splitlines() == ["1000", "1000", "wrote"]
    finally:
        engine.stop(c)


def test_workdir_created_and_used(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs, workdir="/made/up/dir")
    engine.start(c)
    try:
        assert sh(engine, c, "pwd").stdout.strip() == "/made/up/dir"
        assert (c.dir / "upper" / "made/up/dir").is_dir()
    finally:
        engine.stop(c)


def test_zombies_are_reaped_and_host_mounts_untouched(running):
    engine, c = running
    before = Path("/proc/self/mountinfo").read_text().count(str(c.dir))
    assert before == 0
    sh(engine, c, "(sleep 0.2 &) ; true")
    time.sleep(0.6)
    pid = json.loads((c.dir / KEEPER_PID).read_text())["pid"]
    ps = subprocess.run(["ps", "-o", "stat=", "--ppid", str(pid)], capture_output=True, text=True).stdout
    assert "Z" not in ps
    assert Path("/proc/self/mountinfo").read_text().count(str(c.dir)) == 0


def test_stop_kills_everything_and_exec_after_stop_fails(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    engine.start(c)
    p = engine.popen(c, ["/bin/sleep", "30"], c.env, "/")
    time.sleep(0.3)
    pid = json.loads((c.dir / KEEPER_PID).read_text())["pid"]
    engine.stop(c)
    assert p.wait(timeout=5) != 0
    assert not Path(f"/proc/{pid}").exists()
    with pytest.raises(ContainerNotRunning):
        engine.popen(c, ["/bin/true"], c.env, "/")


def test_stale_pid_file_is_not_running(home, busybox_rootfs, engine):
    c = make_container(home, busybox_rootfs)
    (c.dir / KEEPER_PID).write_text(json.dumps({"pid": 1, "starttime": "0"}))
    assert not engine.is_running(c)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_engine_ns.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.engine_ns'`

- [ ] **Step 4: Write keeper.py**

```python
# src/xcodon_runtime/keeper.py
"""The ns keeper: builds the sandbox with system calls, then blocks inside it as pid 1.

Run as:  python -m xcodon_runtime.keeper PLAN_JSON INFO_FD

The parent process writes ``pid <n>`` to INFO_FD and exits. Pid 1 of the new pid
namespace finishes the mounts, pivots, and writes ``ready``. Any failure before
``ready`` is printed to the keeper log and exits non-zero.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from pathlib import Path

from xcodon_runtime import syscalls as sc

KEEPER_PLAN = "keeper-plan.json"
KEEPER_LOG = "keeper.log"
OLD_ROOT = ".xcodon-oldroot"
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")
DEV_SYMLINKS = (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"))
HOST_FILES = ("/etc/resolv.conf", "/etc/hosts")


def _setup_dev(merged: str) -> None:
    dev = f"{merged}/dev"
    os.makedirs(dev, exist_ok=True)
    sc.mount("tmpfs", dev, "tmpfs", sc.MS_NOSUID | sc.MS_NOEXEC, "mode=0755,size=65536k")
    for name in DEVICES:
        if os.path.exists(f"/dev/{name}"):
            sc.bind_mount(f"/dev/{name}", f"{dev}/{name}")
    os.makedirs(f"{dev}/pts")
    sc.mount("devpts", f"{dev}/pts", "devpts", sc.MS_NOSUID | sc.MS_NOEXEC,
             "newinstance,ptmxmode=0666,mode=0620")
    os.symlink("pts/ptmx", f"{dev}/ptmx")
    os.makedirs(f"{dev}/shm")
    sc.mount("tmpfs", f"{dev}/shm", "tmpfs", sc.MS_NOSUID | sc.MS_NODEV, "mode=1777")
    for name, target in DEV_SYMLINKS:
        os.symlink(target, f"{dev}/{name}")


def _setup_sandbox(plan: dict, merged: str) -> None:
    _setup_dev(merged)
    os.makedirs(f"{merged}/proc", exist_ok=True)
    sc.mount("proc", f"{merged}/proc", "proc", sc.MS_NOSUID | sc.MS_NODEV | sc.MS_NOEXEC)
    os.makedirs(f"{merged}/sys", exist_ok=True)
    try:
        sc.bind_mount("/sys", f"{merged}/sys", readonly=True)
    except OSError as e:
        print(f"warning: could not bind /sys: {e}", file=sys.stderr)
    for host_file in HOST_FILES:
        if os.path.exists(host_file):
            sc.bind_mount(host_file, f"{merged}{host_file}", readonly=True)
    for b in plan["binds"]:
        sc.bind_mount(b["source"], f"{merged}{b['target']}", readonly=b.get("readonly", False))
    os.makedirs(f"{merged}{plan['workdir']}", exist_ok=True)


def _pivot(merged: str) -> None:
    old = f"{merged}/{OLD_ROOT}"
    os.makedirs(old, exist_ok=True)
    sc.pivot_root(merged, old)
    os.chdir("/")
    sc.umount2(f"/{OLD_ROOT}", sc.MNT_DETACH)
    os.rmdir(f"/{OLD_ROOT}")


def _reap(signum, frame) -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _pause_forever() -> None:
    signal.signal(signal.SIGCHLD, _reap)
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    while True:
        signal.pause()


def _run(plan: dict, info_fd: int) -> None:
    host_uid, host_gid = os.getuid(), os.getgid()
    sc.unshare(sc.CLONE_NEWUSER | sc.CLONE_NEWNS)
    sc.write_id_maps(plan["uid"], plan["gid"], host_uid, host_gid)
    sc.mount(None, "/", None, sc.MS_REC | sc.MS_PRIVATE)

    merged = plan["merged"]
    os.makedirs(merged, exist_ok=True)
    sc.mount("overlay", merged, "overlay", 0,
             f"lowerdir={plan['lower']},upperdir={plan['upper']},workdir={plan['work']}")

    sc.unshare(sc.CLONE_NEWPID | sc.CLONE_NEWUTS)
    pid = os.fork()
    if pid:
        os.write(info_fd, f"pid {pid}\n".encode())
        os._exit(0)

    _setup_sandbox(plan, merged)
    _pivot(merged)
    sc.sethostname(plan["hostname"])
    os.write(info_fd, b"ready\n")
    os.close(info_fd)
    _pause_forever()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m xcodon_runtime.keeper PLAN_JSON INFO_FD", file=sys.stderr)
        return 2
    plan = json.loads(Path(argv[0]).read_text())
    info_fd = int(argv[1])
    try:
        _run(plan, info_fd)
    except BaseException:  # noqa: BLE001 — anything here must reach the log
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Write nsexec.py**

```python
# src/xcodon_runtime/nsexec.py
"""Enter a keeper's namespaces and exec a command there.

Run as:  python -m xcodon_runtime.nsexec PID WORKDIR ENV_JSON -- ARGV...

ENV_JSON is a file holding the container environment. It is read and deleted
before entering the namespaces. Exit code: the command's, or 125 when the
container is gone, 126 when the command cannot execute, 127 when not found.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from xcodon_runtime import syscalls as sc

NAMESPACES = ("user", "mnt", "pid", "uts")


def _which(command: str, path_value: str) -> str | None:
    if "/" in command:
        return command if os.access(command, os.X_OK) else None
    for d in path_value.split(":"):
        candidate = os.path.join(d or ".", command)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 4 or argv[3] != "--":
        print("usage: python -m xcodon_runtime.nsexec PID WORKDIR ENV_JSON -- ARGV...", file=sys.stderr)
        return 2
    pid, workdir, env_file, command = int(argv[0]), argv[1], Path(argv[2]), argv[4:]
    env = json.loads(env_file.read_text())
    env_file.unlink(missing_ok=True)

    # Open every namespace file first: after entering the mount namespace, /proc is the sandbox's.
    try:
        fds = [os.open(f"/proc/{pid}/ns/{ns}", os.O_RDONLY) for ns in NAMESPACES]
    except FileNotFoundError:
        print("xcodon: container is not running", file=sys.stderr)
        return 125
    try:
        for fd in fds:
            sc.setns(fd, 0)
            os.close(fd)
    except OSError as e:
        print(f"xcodon: cannot enter container: {e}", file=sys.stderr)
        return 125

    child = os.fork()
    if child == 0:
        try:
            sc.set_no_new_privs()
            os.makedirs(workdir, exist_ok=True)
            os.chdir(workdir)
            exe = _which(command[0], env.get("PATH", ""))
            if exe is None:
                print(f"xcodon: exec: {command[0]}: not found", file=sys.stderr)
                os._exit(127)
            os.execve(exe, command, env)
        except PermissionError as e:
            print(f"xcodon: exec: {command[0]}: {e.strerror}", file=sys.stderr)
            os._exit(126)
        except OSError as e:
            print(f"xcodon: exec: {command[0]}: {e}", file=sys.stderr)
            os._exit(126)

    def forward(signum, frame):
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, forward)
    while True:
        try:
            _, status = os.waitpid(child, 0)
            break
        except InterruptedError:
            continue
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Write engine_ns.py**

```python
# src/xcodon_runtime/engine_ns.py
"""The ns engine: a Python keeper holds the namespaces, nsexec enters them."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable
from xcodon_runtime.keeper import KEEPER_LOG, KEEPER_PLAN

log = logging.getLogger(__name__)
KEEPER_PID = "keeper.pid"
START_TIMEOUT = 60.0


def process_start_time(pid: int) -> str | None:
    """Field 22 of /proc/PID/stat, or None if the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    after_comm = text.rsplit(")", 1)[1].split()
    return after_comm[19] if len(after_comm) > 19 else None


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


class NsEngine:
    name = "ns"

    def _plan(self, c: Container) -> dict:
        return {
            "lower": c.image_rootfs,
            "upper": str(c.dir / "upper"),
            "work": str(c.dir / "work"),
            "merged": str(c.dir / "merged"),
            "uid": c.uid,
            "gid": c.gid,
            "hostname": c.short_id,
            "workdir": c.workdir,
            "binds": [b.to_dict() for b in c.binds],
        }

    def start(self, c: Container) -> None:
        if self.is_running(c):
            return
        for d in ("upper", "work", "merged"):
            (c.dir / d).mkdir(exist_ok=True)
        plan_path = c.dir / KEEPER_PLAN
        plan_path.write_text(json.dumps(self._plan(c), indent=2))
        log_path = c.dir / KEEPER_LOG

        r, w = os.pipe()
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                [sys.executable, "-m", "xcodon_runtime.keeper", str(plan_path), str(w)],
                pass_fds=(w,), stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                start_new_session=True, close_fds=True,
            )
        os.close(w)
        buf = b""
        deadline = time.monotonic() + START_TIMEOUT
        try:
            while b"ready\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([r], [], [], remaining)
                if not ready:
                    break
                chunk = os.read(r, 256)
                if not chunk:
                    break
                buf += chunk
        finally:
            os.close(r)
            proc.wait(timeout=5)

        match = re.search(rb"pid (\d+)", buf)
        if b"ready\n" not in buf or not match:
            if match:
                try:
                    os.kill(int(match.group(1)), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise EngineUnavailable(
                f"ns keeper failed to start for container {c.short_id}. Keeper log:\n{_tail(log_path)}"
            )
        pid = int(match.group(1))
        (c.dir / KEEPER_PID).write_text(json.dumps({"pid": pid, "starttime": process_start_time(pid)}))
        log.info("container %s: keeper pid %d", c.short_id, pid)

    def _keeper_pid(self, c: Container) -> int | None:
        try:
            info = json.loads((c.dir / KEEPER_PID).read_text())
        except (OSError, ValueError):
            return None
        pid = int(info["pid"])
        if process_start_time(pid) != info.get("starttime"):
            return None
        return pid

    def is_running(self, c: Container) -> bool:
        return self._keeper_pid(c) is not None

    def popen(self, c: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        pid = self._keeper_pid(c)
        if pid is None:
            raise ContainerNotRunning(f"container {c.short_id} is not running; start it first")
        fd, env_path = tempfile.mkstemp(prefix="exec-", suffix=".env.json", dir=c.dir)
        with os.fdopen(fd, "w") as f:
            json.dump(env, f)
        return subprocess.Popen(
            [sys.executable, "-m", "xcodon_runtime.nsexec", str(pid), workdir, env_path, "--", *argv],
            **popen_kwargs,
        )

    def stop(self, c: Container) -> None:
        pid = self._keeper_pid(c)
        if pid is None:
            (c.dir / KEEPER_PID).unlink(missing_ok=True)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and process_start_time(pid) is not None:
            time.sleep(0.02)
        if process_start_time(pid) is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while process_start_time(pid) is not None:
                time.sleep(0.02)
        (c.dir / KEEPER_PID).unlink(missing_ok=True)
        log.info("container %s: keeper stopped", c.short_id)
```

- [ ] **Step 7: Run the ns engine tests**

Run: `pytest tests/test_engine_ns.py -v`
Expected: all PASS on this host. Debugging notes:
- If `start` raises with an empty log, run `python -m xcodon_runtime.keeper <plan> 2` by hand with a plan file to see the traceback.
- `EPERM` on the `/sys` remount means `mount_flags_at` did not find the mount; check that `ensure_mountpoint` created `merged/sys` before the bind.
- If `test_sandbox_identity` shows a large pid, `unshare(CLONE_NEWPID)` happened after the fork; check the order in `_run`.

- [ ] **Step 8: Commit**

```bash
git add src/xcodon_runtime/containers.py src/xcodon_runtime/keeper.py src/xcodon_runtime/nsexec.py src/xcodon_runtime/engine_ns.py tests/test_engine_ns.py
git commit -m "feat: ns engine with Python keeper, nsexec, and container record"
```

---

### Task 11: Vendored PRoot and the proot engine

**Files:**
- Create: `scripts/fetch_proot.py`
- Create: `src/xcodon_runtime/_bin/proot-x86_64` (downloaded, committed)
- Create: `src/xcodon_runtime/_bin/MANIFEST`
- Create: `src/xcodon_runtime/_bin/LICENSE-proot`
- Create: `src/xcodon_runtime/engine_proot.py`
- Modify: `tests/conftest.py` (remove the `engine_proot` import guard)
- Test: `tests/test_vendored.py`
- Test: `tests/test_engine_proot.py`

**Interfaces:**
- Consumes: `containers.Container`, `engine.Bind`, `errors.EngineUnavailable`, `errors.ContainerNotRunning`.
- Produces: `engine_proot.find_proot() -> str | None` (order: `XCODON_PROOT`, `proot` on PATH, vendored x86_64 binary), `engine_proot.ProotEngine` implementing `Engine`, `engine_proot.STARTED_MARKER = "proot-started"`, `engine_proot.VENDORED_DIR`.

- [ ] **Step 1: Write scripts/fetch_proot.py and run it**

```python
#!/usr/bin/env python3
# scripts/fetch_proot.py
"""Download the PRoot static binary we vendor and record its checksum.

PRoot is GPL-2.0. It is distributed next to this MIT package as a separate
program; its license text is kept beside the binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.request
from pathlib import Path

VERSION = "v5.4.1"
URL = f"https://github.com/proot-me/proot/releases/download/{VERSION}/proot"
SHA256 = "19f44283f5c0e73091c60195f5fcd4f4c1165505e44410d434e2ab1b677c1a09"
LICENSE_URL = f"https://raw.githubusercontent.com/proot-me/proot/{VERSION}/COPYING"
DEST_DIR = Path(__file__).resolve().parents[1] / "src" / "xcodon_runtime" / "_bin"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main() -> None:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    data = download(URL)
    got = hashlib.sha256(data).hexdigest()
    if got != SHA256:
        raise SystemExit(f"checksum mismatch: expected {SHA256}, got {got}")
    target = DEST_DIR / "proot-x86_64"
    target.write_bytes(data)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (DEST_DIR / "LICENSE-proot").write_bytes(download(LICENSE_URL))
    (DEST_DIR / "MANIFEST").write_text(
        json.dumps({"proot-x86_64": {"version": VERSION, "sha256": SHA256, "url": URL}}, indent=2) + "\n"
    )
    print(f"wrote {target} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
```

Run:
```bash
python scripts/fetch_proot.py && src/xcodon_runtime/_bin/proot-x86_64 --version | head -1
```
Expected: `wrote .../proot-x86_64 (1824992 bytes)` and a PRoot banner line.

- [ ] **Step 2: Write test_vendored.py**

```python
# tests/test_vendored.py
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "src" / "xcodon_runtime" / "_bin"


def test_manifest_matches_binaries():
    manifest = json.loads((BIN / "MANIFEST").read_text())
    assert "proot-x86_64" in manifest
    for name, meta in manifest.items():
        path = BIN / name
        assert path.exists(), f"{name} missing; run scripts/fetch_proot.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]
        assert os.access(path, os.X_OK)
    assert "GNU" in (BIN / "LICENSE-proot").read_text()[:300]


@pytest.mark.skipif(platform.machine() != "x86_64", reason="vendored binary is x86_64")
def test_vendored_proot_runs():
    out = subprocess.run([str(BIN / "proot-x86_64"), "--version"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "proot" in (out.stdout + out.stderr).lower()
```

Run: `pytest tests/test_vendored.py -q` → PASS.

- [ ] **Step 3: Write the failing proot engine tests**

```python
# tests/test_engine_proot.py
"""Integration tests for the proot engine. Marked `proot`; skipped without a binary."""

import os
import subprocess
from datetime import datetime, timezone

import pytest

from xcodon_runtime.containers import Container
from xcodon_runtime.engine import Bind
from xcodon_runtime.engine_proot import STARTED_MARKER, ProotEngine, find_proot
from xcodon_runtime.errors import ContainerNotRunning

pytestmark = pytest.mark.proot


def make_container(home, rootfs, uid=0, gid=0, binds=(), workdir="/"):
    cid = os.urandom(32).hex()
    cdir = home.containers / cid
    cdir.mkdir()
    c = Container(
        id=cid, image_id="img", image_ref="test/bb:latest", image_rootfs=str(rootfs), engine="proot",
        argv=["/bin/sh"], env={"PATH": "/bin", "HOME": "/root"}, workdir=workdir, uid=uid, gid=gid,
        binds=list(binds), created=datetime.now(timezone.utc).isoformat(), dir=cdir,
    )
    c.save()
    return c


def sh(engine, c, script):
    p = engine.popen(c, ["/bin/sh", "-c", script], c.env, c.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(timeout=60)
    return subprocess.CompletedProcess(p.args, p.returncode, out.decode(), err.decode())


def test_find_proot_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "proot"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("XCODON_PROOT", str(fake))
    assert find_proot() == str(fake)
    monkeypatch.delenv("XCODON_PROOT")
    assert find_proot() is not None


def test_start_copies_rootfs_and_marks(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    assert not e.is_running(c)
    e.start(c)
    assert (c.dir / "rootfs" / "bin" / "busybox").exists()
    assert (c.dir / STARTED_MARKER).exists()
    assert e.is_running(c)
    assert os.stat(c.dir / "rootfs/bin/busybox").st_ino != os.stat(busybox_rootfs / "bin/busybox").st_ino


def test_exec_identity_exit_code_and_persistence(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, workdir="/work/dir")
    e.start(c)
    r = sh(e, c, "id -u; pwd; echo hi > /persist; exit 3")
    assert r.returncode == 3, r.stderr
    assert r.stdout.splitlines()[:2] == ["0", "/work/dir"]
    assert (c.dir / "rootfs" / "persist").read_text() == "hi\n"
    e.stop(c)
    e.start(c)
    assert sh(e, c, "cat /persist").stdout == "hi\n"


def test_non_root_uid(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, uid=1000, gid=1000)
    e.start(c)
    assert sh(e, c, "id -u; id -g").stdout.splitlines() == ["1000", "1000"]


def test_binds_and_command_not_found(home, busybox_rootfs, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    e = ProotEngine()
    c = make_container(home, busybox_rootfs, binds=[Bind(str(data), "/data")])
    e.start(c)
    assert sh(e, c, "echo w > /data/out").returncode == 0
    assert (data / "out").read_text() == "w\n"
    p = e.popen(c, ["/no/such"], c.env, "/", stderr=subprocess.PIPE)
    p.communicate(timeout=60)
    assert p.returncode == 127


def test_popen_before_start_fails(home, busybox_rootfs):
    e = ProotEngine()
    c = make_container(home, busybox_rootfs)
    with pytest.raises(ContainerNotRunning):
        e.popen(c, ["/bin/true"], c.env, "/")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_engine_proot.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.engine_proot'`

- [ ] **Step 5: Write engine_proot.py**

```python
# src/xcodon_runtime/engine_proot.py
"""The proot engine: a copied rootfs and a PRoot process per exec. Works without any kernel help."""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

from xcodon_runtime.containers import Container
from xcodon_runtime.errors import ContainerNotRunning, EngineUnavailable

log = logging.getLogger(__name__)
VENDORED_DIR = Path(__file__).resolve().parent / "_bin"
STARTED_MARKER = "proot-started"
HOST_BINDS = ("/dev", "/proc", "/sys", "/etc/resolv.conf", "/etc/hosts")


def find_proot() -> str | None:
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


class ProotEngine:
    name = "proot"

    def start(self, c: Container) -> None:
        proot = find_proot()
        if proot is None:
            raise EngineUnavailable(
                "no PRoot binary: set XCODON_PROOT to a static proot, put proot on PATH, "
                "or use an x86_64 build with the vendored binary"
            )
        rootfs = c.dir / "rootfs"
        if not (c.dir / STARTED_MARKER).exists():
            log.info("container %s: copying rootfs (full copy unless the filesystem supports reflinks)", c.short_id)
            _copy_rootfs(Path(c.image_rootfs), rootfs)
            (c.dir / STARTED_MARKER).write_text(proot)
        (rootfs / c.workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def is_running(self, c: Container) -> bool:
        return (c.dir / STARTED_MARKER).exists() and (c.dir / "rootfs").is_dir()

    def popen(self, c: Container, argv: list[str], env: dict[str, str], workdir: str,
              **popen_kwargs) -> subprocess.Popen:
        if not self.is_running(c):
            raise ContainerNotRunning(f"container {c.short_id} is not started")
        proot = find_proot()
        if proot is None:
            raise EngineUnavailable("PRoot binary disappeared; set XCODON_PROOT")
        rootfs = c.dir / "rootfs"
        (rootfs / workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)
        cmd = [proot, "-r", str(rootfs), "-w", workdir, "-i", f"{c.uid}:{c.gid}"]
        for host_path in HOST_BINDS:
            if os.path.exists(host_path):
                cmd += ["-b", host_path]
        for b in c.binds:
            cmd += ["-b", f"{b.source}:{b.target}"]
        cmd += shlex.split(os.environ.get("XCODON_PROOT_ARGS", ""))
        cmd += argv
        return subprocess.Popen(cmd, env=env, **popen_kwargs)

    def stop(self, c: Container) -> None:
        return None
```

Then in `tests/conftest.py` remove the `try/except ImportError` around `from xcodon_runtime.engine_proot import find_proot`.

- [ ] **Step 6: Run the proot engine tests**

Run: `pytest tests/test_engine_proot.py -v`
Expected: all PASS. If PRoot prints `proot warning: can't sanitize binding "/etc/resolv.conf"`, that is a warning only; the test reads exit codes and stdout. If `test_binds_and_command_not_found` sees exit code 1 instead of 127, PRoot could not exec; check that argv is appended after the binds and that no `--` separator was inserted.

- [ ] **Step 7: Commit**

```bash
git add scripts/fetch_proot.py src/xcodon_runtime/_bin/proot-x86_64 src/xcodon_runtime/_bin/MANIFEST src/xcodon_runtime/_bin/LICENSE-proot src/xcodon_runtime/engine_proot.py tests/test_vendored.py tests/test_engine_proot.py tests/conftest.py
git commit -m "feat: proot engine with vendored static PRoot v5.4.1"
```

---
### Task 12: ContainerStore and the Runtime API

**Files:**
- Modify: `src/xcodon_runtime/containers.py` (add `ContainerStore`)
- Create: `src/xcodon_runtime/api.py`
- Modify: `tests/conftest.py` (add `engine_name` and `busybox_image` fixtures)
- Test: `tests/test_containers_api.py`

**Interfaces:**
- Consumes: `containers.Container`, `imagestore.Image`, `imagestore.ImageStore`, `spec.build_spec`, `spec.ProcessSpec`, `engine.Bind`, `engine.select_engine`, `engine.get_engine`, `engine.EngineChoice`, `engine_proot.find_proot`, `errors.*`.
- Produces `containers.ContainerStore(home: RuntimeHome)` with `create(container_id: str, image: Image, image_ref: str, spec: ProcessSpec, binds: list[Bind], engine: str, name: str | None) -> Container`, `get(key: str) -> Container` (id, unique prefix of 4+ chars, or name; raises ContainerNotFound), `list() -> list[Container]`, `remove(c: Container) -> None`.
- Produces `api.ExecResult(code: int, stdout: bytes, stderr: bytes)`, `api.Runtime(home: Path | str | None = None, engine: str | None = None)` with `home`, `images` (ImageStore), `store` (ContainerStore); methods `engine_choice() -> EngineChoice`, `pull(ref, platform=None) -> Image`, `inspect(ref) -> Image | None`, `list_images() -> list[Image]`, `remove_image(ref) -> None`, `resolve_image(ref, pull: str = "missing") -> Image`, `create(ref, command=None, entrypoint=None, binds=(), workdir=None, env=None, user=None, name=None, pull="missing") -> Container`, `start(c)`, `popen(c, command=None, workdir=None, env=None, **popen_kwargs) -> subprocess.Popen`, `exec(c, command=None, workdir=None, env=None, capture=True, timeout=None) -> ExecResult`, `stop(c)`, `remove(c, force=False)`, `containers(all=False) -> list[Container]`, `get_container(key) -> Container`, `run(ref, command=None, entrypoint=None, binds=(), workdir=None, env=None, user=None, name=None, rm=False, pull="missing", stdin=None, stdout=None, stderr=None) -> int`, `info() -> dict`, `prune() -> list[Path]`.

- [ ] **Step 1: Add fixtures to conftest**

Append to `tests/conftest.py` (put the new `import` lines at the top of the file with the others):

```python
import gzip
import hashlib
import io
import json
import tarfile


def _ns_available() -> bool:
    from xcodon_runtime.probe import run_probes

    return all(v["ok"] for v in run_probes().values())


def _proot_available() -> bool:
    from xcodon_runtime.engine_proot import find_proot

    return find_proot() is not None


@pytest.fixture(params=["ns", "proot"])
def engine_name(request) -> str:
    name = request.param
    if name == "ns" and not _ns_available():
        pytest.skip("ns engine unavailable on this host")
    if name == "proot" and not _proot_available():
        pytest.skip("no proot binary")
    return name


def pack_rootfs_as_image(home: RuntimeHome, rootfs: Path, ref: str, config: dict | None = None):
    """Import a directory as a one-layer image into ``home`` under ``ref``. Returns the Image."""
    from xcodon_runtime.imagestore import ImageStore
    from xcodon_runtime.registry import FetchedImage, FetchedLayer

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        t.add(rootfs, arcname=".")
    raw = buf.getvalue()
    gz = gzip.compress(raw)
    blob = home.blobs / hashlib.sha256(gz).hexdigest()
    blob.write_bytes(gz)
    cfg = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/bin"], "Cmd": ["/bin/sh"], "WorkingDir": "/workspace"},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(raw).hexdigest()]},
    }
    if config:
        cfg["config"].update(config)
    cbytes = json.dumps(cfg).encode()
    fetched = FetchedImage("sha256:" + hashlib.sha256(cbytes).hexdigest(), cfg,
                           [FetchedLayer("sha256:" + hashlib.sha256(gz).hexdigest(), "tar+gzip", len(gz), blob)],
                           source="test")
    from xcodon_runtime.reference import parse_reference

    return ImageStore(home, sources=[]).import_fetched(fetched, parse_reference(ref).name)


@pytest.fixture
def busybox_image(home, busybox_rootfs):
    return pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/busybox:latest")
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_containers_api.py
import subprocess
import time

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import ContainerNotFound, ContainerNotRunning, ImageNotFound, XcodonError


@pytest.fixture
def rt(home, busybox_image, engine_name) -> Runtime:
    return Runtime(home.path, engine=engine_name)


def test_create_start_exec_stop_remove(rt):
    c = rt.create("xcodon-test/busybox", command=["/bin/sh"], name="one")
    assert c.state == "created"
    assert c.workdir == "/workspace"
    assert c.env["HOSTNAME"] == c.short_id
    rt.start(c)
    assert rt.get_container("one").state == "running"
    r = rt.exec(c, "echo hi; echo err >&2; exit 4")
    assert (r.code, r.stdout, r.stderr) == (4, b"hi\n", b"err\n")
    assert rt.exec(c, ["/bin/pwd"]).stdout == b"/workspace\n"
    assert rt.exec(c, "echo x > /f && cat /f").stdout == b"x\n"
    rt.stop(c)
    assert rt.get_container(c.id[:8]).state == "exited"
    rt.start(c)
    assert rt.exec(c, ["/bin/cat", "/f"]).stdout == b"x\n"
    rt.stop(c)
    rt.remove(c)
    with pytest.raises(ContainerNotFound):
        rt.get_container("one")


def test_exec_env_and_workdir_overrides(rt):
    c = rt.create("xcodon-test/busybox", env={"A": "1"})
    rt.start(c)
    try:
        r = rt.exec(c, "echo $A $B; pwd", workdir="/tmp", env={"B": "2"})
        assert r.stdout == b"1 2\n/tmp\n"
    finally:
        rt.stop(c)


def test_exec_before_start_raises(rt):
    c = rt.create("xcodon-test/busybox")
    with pytest.raises(ContainerNotRunning):
        rt.exec(c, "true")


def test_remove_running_requires_force(rt):
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    with pytest.raises(XcodonError, match="running"):
        rt.remove(c)
    rt.remove(c, force=True)
    assert rt.containers(all=True) == []


def test_run_returns_exit_code_and_rm(rt, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    code = rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "echo ran > /o/f; exit 5"],
                  binds=[Bind(str(out), "/o")], rm=True)
    assert code == 5
    assert (out / "f").read_text() == "ran\n"
    assert rt.containers(all=True) == []


def test_run_without_rm_leaves_exited_container(rt):
    code = rt.run("xcodon-test/busybox", command=["/bin/true"], name="kept")
    assert code == 0
    c = rt.get_container("kept")
    assert c.state == "exited"
    rt.remove(c)


def test_run_streams_stdout(rt, tmp_path):
    log = tmp_path / "log"
    with open(log, "wb") as f:
        rt.run("xcodon-test/busybox", command=["/bin/echo", "streamed"], rm=True, stdout=f)
    assert log.read_bytes() == b"streamed\n"


def test_unknown_image_without_pull_source(rt):
    with pytest.raises((ImageNotFound, XcodonError)):
        rt.create("nonexistent/never:latest")


def test_duplicate_name_rejected(rt):
    rt.create("xcodon-test/busybox", name="dup")
    with pytest.raises(XcodonError, match="dup"):
        rt.create("xcodon-test/busybox", name="dup")


def test_info_reports_engine(rt, engine_name):
    info = rt.info()
    assert info["engine"] == engine_name
    assert "home" in info and "python" in info
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_containers_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.api'`

- [ ] **Step 4: Add ContainerStore to containers.py**

Append to `src/xcodon_runtime/containers.py`:

```python
import shutil

from xcodon_runtime.errors import ContainerNotFound, XcodonError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.imagestore import Image
from xcodon_runtime.spec import ProcessSpec


class ContainerStore:
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
        shutil.rmtree(c.dir, ignore_errors=True)
```

Move the `import shutil` and the four `from xcodon_runtime...` imports to the top of the file with the others.

- [ ] **Step 5: Write api.py**

```python
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
            p.kill()
            out, err = p.communicate()
            raise
        return ExecResult(p.returncode, out or b"", err or b"")

    def stop(self, c: Container) -> None:
        self._engine(c).stop(c)
        c.state = "exited"
        c.save()

    def remove(self, c: Container, force: bool = False) -> None:
        if self._engine(c).is_running(c):
            if not force:
                raise XcodonError(f"container {c.short_id} is running; stop it first or use force")
            self.stop(c)
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
        c = self.create(ref, command, entrypoint, binds, workdir, env, user, name, pull)
        try:
            self.start(c)
            p = self.popen(c, None, None, None, stdin=stdin, stdout=stdout, stderr=stderr)
            previous = {}

            def forward(signum, frame):
                try:
                    p.send_signal(signum)
                except ProcessLookupError:
                    pass

            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, forward)
            try:
                return p.wait()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        finally:
            try:
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
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_containers_api.py -v`
Expected: PASS for both engine parameters on this host. `test_exec_env_and_workdir_overrides` under proot: PRoot prints a warning to stderr about `/etc/resolv.conf` on some hosts; the assertion reads stdout only.

- [ ] **Step 7: Commit**

```bash
git add src/xcodon_runtime/containers.py src/xcodon_runtime/api.py tests/conftest.py tests/test_containers_api.py
git commit -m "feat: container store and Runtime API"
```

---

### Task 13: Command-line interface

**Files:**
- Create: `src/xcodon_runtime/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `api.Runtime`, `engine.Bind`, `errors.XcodonError`, `reference.parse_platform`.
- Produces: `cli.main(argv: list[str] | None = None) -> int`, `cli.parse_run_args(tokens: list[str]) -> RunOptions`, `cli.RunOptions` dataclass with `binds: list[Bind]`, `workdir`, `env: dict`, `entrypoint: list[str] | None`, `user`, `name`, `rm: bool`, `pull: str`, `cidfile`, `image`, `command: list[str]`, `ignored: list[str]`.
- Exit codes: 125 runtime error, 126/127 pass through from the engines, else the process exit code.

- [ ] **Step 1: Write the failing CLI tests**

```python
# tests/test_cli.py
import json
import subprocess
import sys

import pytest

from xcodon_runtime import cli
from xcodon_runtime.engine import Bind


def test_parse_run_args_docker_style():
    opts = cli.parse_run_args(
        [
            "--mount=type=bind,source=/h/out,target=/var/spool/cwl",
            "--mount=type=bind,source=/h/tmp,target=/tmp,readonly",
            "-v", "/a:/b:ro", "--volume=/c:/d",
            "--workdir=/var/spool/cwl", "--env=HOME=/var/spool/cwl", "-e", "TMPDIR=/tmp",
            "--rm", "-i", "--user=1000:1000", "--name", "job1", "--entrypoint=/bin/sh",
            "--memory=512m", "--net=none", "--read-only=true", "--log-driver=none", "--cpus", "2", "--gpus=1",
            "--cidfile=/h/cid", "--pull=always",
            "busybox:latest", "sh", "-c", "echo --not-a-flag",
        ]
    )
    assert opts.binds == [
        Bind("/h/out", "/var/spool/cwl", False),
        Bind("/h/tmp", "/tmp", True),
        Bind("/a", "/b", True),
        Bind("/c", "/d", False),
    ]
    assert opts.workdir == "/var/spool/cwl"
    assert opts.env == {"HOME": "/var/spool/cwl", "TMPDIR": "/tmp"}
    assert opts.rm and opts.user == "1000:1000" and opts.name == "job1"
    assert opts.entrypoint == ["/bin/sh"]
    assert opts.cidfile == "/h/cid" and opts.pull == "always"
    assert opts.image == "busybox:latest"
    assert opts.command == ["sh", "-c", "echo --not-a-flag"]
    assert {"--memory", "--net", "--read-only", "--log-driver", "--cpus", "--gpus"} <= set(opts.ignored)


def test_split_argv_keeps_command_flags_out_of_argparse():
    head, rest = cli._split_argv(["--engine", "proot", "-v", "run", "--rm", "img", "grep", "-v", "x"])
    assert head == ["--engine", "proot", "-v", "run"]
    assert rest == ["--rm", "img", "grep", "-v", "x"]
    assert cli._split_argv(["ps", "-a"]) == (["ps", "-a"], None)
    assert cli._split_argv(["create", "img"]) == (["create"], ["img"])


def test_run_help_exits_zero(home, capsys):
    assert cli.main(["run", "--help"]) == 0
    assert "IMAGE [COMMAND...]" in capsys.readouterr().out


def test_parse_run_args_unknown_flag_is_error():
    with pytest.raises(cli.UsageError, match="--privileged"):
        cli.parse_run_args(["--privileged", "img"])


def test_parse_run_args_requires_image():
    with pytest.raises(cli.UsageError, match="image"):
        cli.parse_run_args(["--rm"])


def test_parse_mount_requires_source_and_target():
    with pytest.raises(cli.UsageError, match="target"):
        cli.parse_run_args(["--mount=type=bind,source=/x", "img"])


def test_runtime_error_exit_125(home, capsys):
    code = cli.main(["inspect-not-a-command"])
    assert code == 2
    code = cli.main(["--engine", "proot", "start", "nosuchcontainer"])
    assert code == 125
    assert "xcodon:" in capsys.readouterr().err


def test_inspect_missing_image_prints_empty_array(home, capsys):
    assert cli.main(["inspect", "nobody/nothing:latest"]) == 1
    assert json.loads(capsys.readouterr().out) == []


def test_info_prints_json(home, engine_name, capsys):
    assert cli.main(["--engine", engine_name, "info"]) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == engine_name


def test_full_cli_flow(home, busybox_image, engine_name, capsys, tmp_path):
    e = ["--engine", engine_name]
    assert cli.main([*e, "images"]) == 0
    assert "xcodon-test/busybox:latest" in capsys.readouterr().out
    assert cli.main([*e, "inspect", "xcodon-test/busybox"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["Config"]["Cmd"] == ["/bin/sh"]

    assert cli.main([*e, "create", "--name", "c1", "xcodon-test/busybox", "/bin/sh"]) == 0
    cid = capsys.readouterr().out.strip()
    assert len(cid) == 64
    assert cli.main([*e, "start", "c1"]) == 0
    assert cli.main([*e, "exec", "-w", "/tmp", "--env=Q=1", "c1", "/bin/sh", "-c", "pwd; echo $Q; exit 3"]) == 3
    assert capsys.readouterr().out == "/tmp\n1\n"
    assert cli.main([*e, "ps"]) == 0
    assert "c1" in capsys.readouterr().out
    assert cli.main([*e, "stop", "c1"]) == 0
    assert cli.main([*e, "rm", "c1"]) == 0

    out = tmp_path / "o"
    out.mkdir()
    cid_file = tmp_path / "cid"
    code = cli.main([*e, "run", "--rm", f"--mount=type=bind,source={out},target=/o", "--workdir=/o",
                     "--env=NAME=cwl", f"--cidfile={cid_file}", "--memory=10m", "xcodon-test/busybox",
                     "/bin/sh", "-c", "echo hello $NAME > f; exit 6"])
    assert code == 6
    assert (out / "f").read_text() == "hello cwl\n"
    assert len(cid_file.read_text().strip()) == 64
    assert cli.main([*e, "ps", "-a"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1, "run --rm must leave no container"
    assert cli.main([*e, "prune"]) == 0


def test_console_script_entry_point():
    r = subprocess.run([sys.executable, "-m", "xcodon_runtime.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "pull" in r.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'xcodon_runtime.cli'`

- [ ] **Step 3: Write cli.py**

```python
# src/xcodon_runtime/cli.py
"""Docker-compatible command line over the Runtime API."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from io import StringIO

from xcodon_runtime import __version__
from xcodon_runtime.api import Runtime
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.reference import parse_platform

log = logging.getLogger("xcodon")

EXIT_RUNTIME_ERROR = 125


class UsageError(XcodonError):
    """Bad command-line input."""


# Flags docker or cwltool may pass that we accept and ignore. True = takes a value.
IGNORED_FLAGS = {
    "--memory": True, "-m": True, "--memory-swap": True, "--cpus": True, "--cpu-shares": True,
    "--gpus": True, "--net": True, "--network": True, "--read-only": True, "--log-driver": True,
    "--userns": True, "--security-opt": True, "-t": False, "--tty": False, "--init": False,
    "--detach-keys": True, "--platform": True,
}
# Flags we honor. True = takes a value.
RUN_FLAGS = {
    "--mount": True, "-v": True, "--volume": True, "-w": True, "--workdir": True, "-e": True, "--env": True,
    "--entrypoint": True, "-u": True, "--user": True, "--name": True, "--rm": False, "-i": False,
    "--interactive": False, "--cidfile": True, "--pull": True,
}


@dataclass
class RunOptions:
    image: str = ""
    command: list[str] = field(default_factory=list)
    binds: list[Bind] = field(default_factory=list)
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    entrypoint: list[str] | None = None
    user: str | None = None
    name: str | None = None
    rm: bool = False
    pull: str = "missing"
    cidfile: str | None = None
    ignored: list[str] = field(default_factory=list)


def _parse_mount(value: str) -> Bind:
    fields = next(csv.reader(StringIO(value)))
    kv: dict[str, str] = {}
    for f in fields:
        k, _, v = f.partition("=")
        kv[k.strip()] = v.strip()
    if kv.get("type", "bind") != "bind":
        raise UsageError(f"--mount type {kv.get('type')!r} is not supported; only bind mounts")
    src = kv.get("source") or kv.get("src")
    dst = kv.get("target") or kv.get("destination") or kv.get("dst")
    if not src or not dst:
        raise UsageError(f"--mount needs source and target: {value}")
    readonly = "readonly" in kv or "ro" in kv or kv.get("readonly") == "true"
    return Bind(os.path.abspath(src), dst, readonly)


def _parse_volume(value: str) -> Bind:
    parts = value.split(":")
    if len(parts) < 2:
        raise UsageError(f"-v needs host:container[:ro|rw]: {value}")
    readonly = len(parts) > 2 and "ro" in parts[2].split(",")
    return Bind(os.path.abspath(parts[0]), parts[1], readonly)


def parse_run_args(tokens: list[str]) -> RunOptions:
    """Hand-rolled so that everything after IMAGE is the command, as docker does."""
    opts = RunOptions()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or tok == "-":
            opts.image = tok
            opts.command = tokens[i + 1 :]
            return opts
        flag, has_eq, inline = tok.partition("=")
        table = RUN_FLAGS if flag in RUN_FLAGS else IGNORED_FLAGS if flag in IGNORED_FLAGS else None
        if table is None:
            raise UsageError(f"unknown option {flag}; xcodon supports a docker subset (see xcodon run --help)")
        takes_value = table[flag]
        value = None
        if takes_value:
            if has_eq:
                value = inline
            else:
                i += 1
                if i >= len(tokens):
                    raise UsageError(f"option {flag} needs a value")
                value = tokens[i]
        i += 1
        if table is IGNORED_FLAGS:
            opts.ignored.append(flag)
            continue
        if flag == "--mount":
            opts.binds.append(_parse_mount(value))
        elif flag in ("-v", "--volume"):
            opts.binds.append(_parse_volume(value))
        elif flag in ("-w", "--workdir"):
            opts.workdir = value
        elif flag in ("-e", "--env"):
            k, _, v = value.partition("=")
            opts.env[k] = v if _ else os.environ.get(k, "")
        elif flag == "--entrypoint":
            opts.entrypoint = [value] if value else []
        elif flag in ("-u", "--user"):
            opts.user = value
        elif flag == "--name":
            opts.name = value
        elif flag == "--rm":
            opts.rm = True
        elif flag == "--cidfile":
            opts.cidfile = value
        elif flag == "--pull":
            if value not in ("missing", "always", "never"):
                raise UsageError("--pull must be missing, always, or never")
            opts.pull = value
        # -i / --interactive: stdin always passes through
    raise UsageError("no image given: usage: xcodon run [OPTIONS] IMAGE [COMMAND...]")


RUN_USAGE = ("xcodon {cmd} [--mount=... | -v HOST:CONTAINER[:ro]] [-w DIR] [-e K=V] [--entrypoint E] "
             "[-u USER] [--name N] [--rm] [-i] [--cidfile F] [--pull missing|always|never] IMAGE [COMMAND...]")

_GLOBAL_OPTIONS_WITH_VALUE = {"--engine", "--home"}


def _split_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split at a `run` or `create` subcommand. argparse's REMAINDER rejects leading options,
    and a global `-v` must not swallow a `-v` inside the container command."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok in ("run", "create"):
            return argv[: i + 1], argv[i + 1 :]
        return argv, None
    return argv, None


def _warn_ignored(opts: RunOptions) -> None:
    if opts.ignored:
        log.warning("ignoring unsupported docker options: %s", ", ".join(sorted(set(opts.ignored))))


# -- commands ------------------------------------------------------------------------


def cmd_pull(rt: Runtime, args) -> int:
    platform = parse_platform(args.platform) if args.platform else None
    img = rt.pull(args.image, platform)
    print(f"{args.image}: {img.short_id}")
    return 0


def cmd_inspect(rt: Runtime, args) -> int:
    doc = rt.images.inspect(args.image)
    print(json.dumps(doc, indent=2))
    return 0 if doc else 1


def cmd_images(rt: Runtime, args) -> int:
    print(f"{'REPOSITORY:TAG':<60} {'IMAGE ID':<14} SOURCE")
    for img in rt.list_images():
        source = json.loads((img.dir / "manifest.json").read_text()).get("source", "")
        for ref in img.refs or ["<none>"]:
            print(f"{ref:<60} {img.short_id:<14} {source}")
    return 0


def cmd_rmi(rt: Runtime, args) -> int:
    rt.remove_image(args.image)
    return 0


def cmd_run(rt: Runtime, args) -> int:
    if args.rest[:1] in (["-h"], ["--help"]):
        print(RUN_USAGE.format(cmd="run"))
        return 0
    opts = parse_run_args(args.rest)
    _warn_ignored(opts)
    c = rt.create(opts.image, command=opts.command or None, entrypoint=opts.entrypoint, binds=opts.binds,
                  workdir=opts.workdir, env=opts.env, user=opts.user, name=opts.name, pull=opts.pull)
    if opts.cidfile:
        with open(opts.cidfile, "w") as f:
            f.write(c.id)
    try:
        rt.start(c)
        p = rt.popen(c)
        return p.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            rt.stop(c)
        finally:
            if opts.rm:
                rt.store.remove(c)


def cmd_create(rt: Runtime, args) -> int:
    if args.rest[:1] in (["-h"], ["--help"]):
        print(RUN_USAGE.format(cmd="create"))
        return 0
    opts = parse_run_args(args.rest)
    _warn_ignored(opts)
    c = rt.create(opts.image, command=opts.command or None, entrypoint=opts.entrypoint, binds=opts.binds,
                  workdir=opts.workdir, env=opts.env, user=opts.user, name=opts.name, pull=opts.pull)
    if opts.cidfile:
        with open(opts.cidfile, "w") as f:
            f.write(c.id)
    print(c.id)
    return 0


def cmd_start(rt: Runtime, args) -> int:
    rt.start(rt.get_container(args.container))
    return 0


def cmd_exec(rt: Runtime, args) -> int:
    env = {}
    for item in args.env or []:
        k, _, v = item.partition("=")
        env[k] = v
    c = rt.get_container(args.container)
    p = rt.popen(c, args.command or None, workdir=args.workdir, env=env)
    return p.wait()


def cmd_stop(rt: Runtime, args) -> int:
    rt.stop(rt.get_container(args.container))
    return 0


def cmd_rm(rt: Runtime, args) -> int:
    rt.remove(rt.get_container(args.container), force=args.force)
    return 0


def cmd_ps(rt: Runtime, args) -> int:
    print(f"{'CONTAINER ID':<14} {'IMAGE':<40} {'ENGINE':<6} {'STATE':<8} NAME")
    for c in rt.containers(all=args.all):
        print(f"{c.short_id:<14} {c.image_ref:<40} {c.engine:<6} {c.state:<8} {c.name or ''}")
    return 0


def cmd_logs(rt: Runtime, args) -> int:
    c = rt.get_container(args.container)
    path = c.dir / "keeper.log"
    if path.exists():
        sys.stdout.write(path.read_text(errors="replace"))
    return 0


def cmd_info(rt: Runtime, args) -> int:
    print(json.dumps(rt.info(), indent=2))
    return 0


def cmd_prune(rt: Runtime, args) -> int:
    for p in rt.prune():
        print(f"removed {p}")
    return 0


# -- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xcodon", description="Rootless container runtime for Docker images.")
    p.add_argument("--version", action="version", version=f"xcodon-runtime {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for info, -vv for debug")
    p.add_argument("--engine", choices=("ns", "proot"), help="force an engine (default: probe the host)")
    p.add_argument("--home", help="runtime home (default: $XCODON_RUNTIME_HOME or ~/.xcodon/runtime)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pull", help="fetch an image into the local store")
    s.add_argument("image")
    s.add_argument("--platform")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("inspect", help="print image metadata as JSON")
    s.add_argument("image")
    s.set_defaults(func=cmd_inspect)

    sub.add_parser("images", help="list stored images").set_defaults(func=cmd_images)

    s = sub.add_parser("rmi", help="remove an image reference")
    s.add_argument("image")
    s.set_defaults(func=cmd_rmi)

    for name, func, help_text in (("run", cmd_run, "create, start, and run a command"),
                                  ("create", cmd_create, "create a container and print its id")):
        # Docker-style options are parsed by parse_run_args, not argparse: main() splits
        # argv at the subcommand and hands everything after it over untouched.
        s = sub.add_parser(name, help=help_text, add_help=False, usage=RUN_USAGE.format(cmd=name))
        s.set_defaults(func=func, rest=[])

    s = sub.add_parser("start", help="start a container's keeper")
    s.add_argument("container")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("exec", help="run a command in a running container")
    s.add_argument("-w", "--workdir")
    s.add_argument("-e", "--env", action="append")
    s.add_argument("container")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("stop", help="stop a container")
    s.add_argument("container")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("rm", help="remove a container")
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument("container")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("ps", help="list containers")
    s.add_argument("-a", "--all", action="store_true")
    s.set_defaults(func=cmd_ps)

    s = sub.add_parser("logs", help="print the keeper log")
    s.add_argument("container")
    s.set_defaults(func=cmd_logs)

    sub.add_parser("info", help="engine choice, probe results, paths").set_defaults(func=cmd_info)
    sub.add_parser("prune", help="remove leftovers and unreferenced layers").set_defaults(func=cmd_prune)
    return p


def _configure_logging(verbosity: int) -> None:
    env_level = os.environ.get("XCODON_LOG", "").lower()
    level = logging.WARNING
    if verbosity >= 2 or env_level == "debug":
        level = logging.DEBUG
    elif verbosity == 1 or env_level == "info":
        level = logging.INFO
    logging.basicConfig(level=level, format="xcodon: %(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    head, rest = _split_argv(list(sys.argv[1:] if argv is None else argv))
    try:
        args = parser.parse_args(head)
    except SystemExit as e:
        return int(e.code or 0)
    if rest is not None:
        args.rest = rest
    _configure_logging(args.verbose)
    try:
        rt = Runtime(args.home, engine=args.engine)
        return args.func(rt, args)
    except XcodonError as e:
        print(f"xcodon: {e}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_cli.py -v`
Expected: all PASS for both engines. `test_runtime_error_exit_125` expects argparse to return 2 for an unknown subcommand; `main` converts the `SystemExit` into a return value.

- [ ] **Step 5: Try it by hand**

```bash
xcodon info
xcodon images
```
Expected: JSON with `"engine": "ns"` on this host, then the images table (empty header only if nothing pulled).

- [ ] **Step 6: Commit**

```bash
git add src/xcodon_runtime/cli.py tests/test_cli.py
git commit -m "feat: docker-compatible CLI"
```

---

### Task 14: coala-runtime adapter

**Files:**
- Create: `src/xcodon_runtime/coala_adapter.py`
- Test: `tests/test_coala_adapter.py`

**Interfaces:**
- Consumes: `api.Runtime`, `containers.Container`, `engine.Bind`.
- Produces: `coala_adapter.XcodonContainerManager(home: Path | str | None = None, engine: str | None = None)` with class attribute `system_site_packages_writable = True`, attribute `containers: dict[str, Container]`, and async methods matching coala-runtime's manager interface: `ensure_image(image: str) -> None`, `create_container(image, command=None, volumes=None, working_dir="/workspace", environment=None, name=None) -> Container`, `start_container(container) -> None`, `exec_command(container, command, workdir=None, environment=None) -> tuple[int, bytes, bytes]`, `get_logs(container, tail=1000) -> str`, `remove_container(container, force=True) -> None`, `cleanup_all() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coala_adapter.py
import asyncio

import pytest

from xcodon_runtime.coala_adapter import XcodonContainerManager


@pytest.fixture
def manager(home, busybox_image, engine_name):
    return XcodonContainerManager(home.path, engine=engine_name)


def test_matches_coala_runtime_interface():
    for method in ("ensure_image", "create_container", "start_container", "exec_command",
                   "get_logs", "remove_container", "cleanup_all"):
        assert asyncio.iscoroutinefunction(getattr(XcodonContainerManager, method))
    assert XcodonContainerManager.system_site_packages_writable is True


def test_lifecycle_like_coala_runtime(manager, tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "data.csv").write_text("a,b\n")
    out = tmp_path / "out"
    out.mkdir()

    async def flow():
        await manager.ensure_image("xcodon-test/busybox:latest")
        c = await manager.create_container(
            "xcodon-test/busybox:latest",
            command="tail -f /dev/null",
            volumes={str(inp): {"bind": "/input", "mode": "ro"}, str(out): {"bind": "/output", "mode": "rw"}},
            working_dir="/workspace",
            environment={"COALA": "1"},
        )
        await manager.start_container(c)
        code, so, se = await manager.exec_command(c, "cat /input/data.csv; echo $COALA; pwd")
        assert (code, so) == (0, b"a,b\n1\n/workspace\n"), se
        code, _, _ = await manager.exec_command(c, ["/bin/sh", "-c", "echo r > /output/result; exit 2"], workdir="/output")
        assert code == 2
        code, so, _ = await manager.exec_command(c, "echo $EXTRA", environment={"EXTRA": "yes"})
        assert so == b"yes\n"
        logs = await manager.get_logs(c)
        assert isinstance(logs, str)
        assert c.id in manager.containers
        await manager.remove_container(c)
        assert c.id not in manager.containers

    asyncio.run(flow())
    assert (out / "result").read_text() == "r\n"


def test_cleanup_all(manager):
    async def flow():
        a = await manager.create_container("xcodon-test/busybox:latest")
        b = await manager.create_container("xcodon-test/busybox:latest")
        await manager.start_container(a)
        await manager.cleanup_all()
        assert manager.containers == {}
        assert manager.runtime.containers(all=True) == []

    asyncio.run(flow())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_coala_adapter.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write coala_adapter.py**

```python
# src/xcodon_runtime/coala_adapter.py
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

log = logging.getLogger(__name__)


async def _call(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


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
        binds = [
            Bind(host, spec["bind"], (spec.get("mode") or "rw").lower() == "ro")
            for host, spec in (volumes or {}).items()
        ]
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
        path = container.dir / "keeper.log"
        if not path.exists():
            return ""
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:] if tail > 0 else lines)

    async def remove_container(self, container: Container, force: bool = True) -> None:
        await _call(self.runtime.remove, container, force=force)
        self.containers.pop(container.id, None)

    async def cleanup_all(self) -> None:
        for c in list(self.containers.values()):
            try:
                await self.remove_container(c)
            except Exception as e:  # noqa: BLE001 — best effort during shutdown
                log.warning("cleanup of %s failed: %s", c.short_id, e)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_coala_adapter.py -v`
Expected: all PASS for both engines.

- [ ] **Step 5: Commit**

```bash
git add src/xcodon_runtime/coala_adapter.py tests/test_coala_adapter.py
git commit -m "feat: ContainerManager adapter for coala-runtime"
```

---

### Task 15: End-to-end tests, README, and CI

**Files:**
- Create: `tests/test_network.py`
- Create: `tests/test_cwltool.py`
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes everything above. Produces no new code interfaces.

- [ ] **Step 1: Write the network test**

```python
# tests/test_network.py
import subprocess

import pytest

from xcodon_runtime.api import Runtime

pytestmark = pytest.mark.network


def test_pull_busybox_from_docker_hub_and_run(home, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    img = rt.pull("busybox:latest")
    assert (img.rootfs / "bin" / "busybox").exists()
    assert rt.inspect("busybox") is not None
    out = tmp_path / "o"
    with open(out, "wb") as f:
        code = rt.run("busybox:latest", command=["sh", "-c", "echo from-hub; id -u"], rm=True, stdout=f)
    assert code == 0
    assert out.read_bytes() == b"from-hub\n0\n"


def test_pull_multi_arch_biocontainer_manifest(home):
    """quay.io serves a plain manifest with anonymous token auth; docker.io serves an index."""
    rt = Runtime(home.path)
    img = rt.pull("quay.io/biocontainers/samtools:1.20--h50ea8bc_0")
    assert (img.rootfs / "usr" / "local" / "bin" / "samtools").exists()
```

- [ ] **Step 2: Write the cwltool test**

```python
# tests/test_cwltool.py
"""coala drives cwltool, and cwltool drives us through --user-space-docker-cmd."""

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.cwltool

TOOL = textwrap.dedent(
    """\
    cwlVersion: v1.2
    class: CommandLineTool
    requirements:
      DockerRequirement:
        dockerPull: xcodon-test/busybox:latest
    inputs:
      message:
        type: string
        inputBinding: {position: 1}
    baseCommand: [echo]
    stdout: out.txt
    outputs:
      out:
        type: File
        outputBinding: {glob: out.txt}
    """
)


def test_cwltool_runs_a_tool_through_xcodon(home, busybox_image, engine_name, tmp_path):
    (tmp_path / "echo.cwl").write_text(TOOL)
    xcodon = shutil.which("xcodon") or os.path.join(os.path.dirname(sys.executable), "xcodon")
    assert os.path.exists(xcodon), "install the package so the xcodon script exists"
    env = {**os.environ, "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(
        ["cwltool", "--user-space-docker-cmd", xcodon, "--outdir", str(tmp_path / "out"),
         str(tmp_path / "echo.cwl"), "--message", "hello from cwl"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert open(result["out"]["path"]).read().strip() == "hello from cwl"
```

- [ ] **Step 3: Run the end-to-end tests**

```bash
XCODON_TEST_NETWORK=1 pytest tests/test_network.py -v
/media/qhu/slim/Workspace/coala/.venv/bin/pip show cwltool >/dev/null && PATH=/media/qhu/slim/Workspace/coala/.venv/bin:$PATH pytest tests/test_cwltool.py -v
```
Expected: PASS. The cwltool test needs `cwltool` on PATH; coala's venv has it. If cwltool reports `inspect` failed, run `XCODON_RUNTIME_HOME=<home> xcodon inspect xcodon-test/busybox:latest` by hand.

- [ ] **Step 4: Write the CI workflow**

```yaml
# .github/workflows/ci.yml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: sudo apt-get update && sudo apt-get install -y busybox-static
      - run: pip install -e ".[dev,zstd]"
      - run: xcodon info
      - run: pytest -q
      - run: XCODON_TEST_NETWORK=1 pytest -q -m network
```

- [ ] **Step 5: Write README.md**

```markdown
# xcodon-runtime

A rootless container runtime for Docker images. It runs the images that
coala and coala-runtime use on hosts without Docker and without root.

Two engines, chosen automatically:

- **ns**: kernel user namespaces plus overlayfs, no helper binary. Native
  speed. Needs a Linux kernel 5.11 or newer with unprivileged user namespaces
  enabled. Most workstations, cloud VMs, and current HPC nodes qualify.
- **proot**: a vendored static PRoot runs the container under ptrace. Works
  on any Linux, slower, and creation copies the image rootfs.

## Install

    pip install xcodon-runtime            # or: uv pip install xcodon-runtime
    xcodon info                            # shows the engine and probe results

Optional: `pip install 'xcodon-runtime[zstd]'` for zstd-compressed layers.
On aarch64 hosts without user namespaces, provide a PRoot binary with
`XCODON_PROOT=/path/to/proot`.

## Use

    xcodon pull python:3.12-slim
    xcodon run --rm -v $PWD:/work -w /work python:3.12-slim python -c 'print("hi")'
    xcodon create --name dev python:3.12-slim
    xcodon start dev
    xcodon exec dev pip install numpy      # persists in the container's writable layer
    xcodon exec dev python -c 'import numpy'
    xcodon stop dev && xcodon rm dev

Images already in a local Docker daemon are reused through `docker save`, so
locally built images work without a registry.

## Environment variables

| Variable | Meaning |
|---|---|
| `XCODON_RUNTIME_HOME` | State directory. Default `~/.xcodon/runtime`. |
| `XCODON_ENGINE` | `ns` or `proot`. Skips probing. |
| `XCODON_PROOT` | Path to a PRoot binary. |
| `XCODON_PROOT_ARGS` | Extra PRoot flags, for example `-k 5.15.0`. |
| `XCODON_LOG` | `info` or `debug`. |

## Limits

- One uid inside the container. Every image file is owned by that uid.
  `chown` to another user fails. Setuid binaries do not elevate.
- Host network only. No `--net=none`, no port mapping.
- No cgroups. `--memory` and `--cpus` are accepted and ignored with a warning.
- No GPU passthrough.

## coala

In coala, add one branch to `configure_container_runner`:

```python
    if container_runner == "xcodon":
        runtime_context.user_space_docker_cmd = shutil.which("xcodon") or "xcodon"
```

cwltool then calls `xcodon inspect`, `xcodon pull`, and `xcodon run` with
docker-style flags.

## coala-runtime

xcodon-runtime ships `xcodon_runtime.coala_adapter.XcodonContainerManager`,
which implements coala-runtime's `ContainerManager` interface. In
coala-runtime, add `XCODON = "xcodon"` to `ContainerEngine`, return the
adapter from `make_container_manager` for that value, and try it in
autodetection after Docker and Podman and before Apptainer.

## Development

    uv venv .venv && . .venv/bin/activate
    uv pip install -e ".[dev,zstd]"
    python scripts/fetch_proot.py          # only if _bin/ is missing
    pytest -q                              # unit + ns + proot on a capable host
    XCODON_TEST_NETWORK=1 pytest -m network

Design: `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`.
```

- [ ] **Step 6: Run the whole suite and commit**

```bash
pytest -q
git add tests/test_network.py tests/test_cwltool.py .github/workflows/ci.yml README.md
git commit -m "test: network and cwltool end-to-end tests; docs and CI"
```

---

### Task 16: Wire xcodon into coala and coala-runtime (sibling repositories)

These changes live in `/media/qhu/slim/Workspace/coala` and `/media/qhu/slim/Workspace/coala-runtime`, each its own git repository. Create a branch in each before editing.

**Files:**
- Modify: `/media/qhu/slim/Workspace/coala/coala/tool_logic.py:37-49` (`configure_container_runner`)
- Modify: `/media/qhu/slim/Workspace/coala-runtime/src/coala_runtime/runtime/engine.py` (`ContainerEngine`, `_autodetect_container_engine`, `get_engine_from_env`, `make_container_manager`)
- Modify: `/media/qhu/slim/Workspace/coala-runtime/src/coala_runtime/__main__.py` (`--engine` choices)
- Modify: `/media/qhu/slim/Workspace/coala-runtime/pyproject.toml` (optional dependency)
- Test: `/media/qhu/slim/Workspace/coala-runtime/tests/test_engine_xcodon.py`

**Interfaces:**
- Consumes: `xcodon_runtime.coala_adapter.XcodonContainerManager`.

- [ ] **Step 1: coala — add the xcodon branch**

In `coala/tool_logic.py`, replace `configure_container_runner` with:

```python
def configure_container_runner(runtime_context: RuntimeContext, container_runner: str) -> None:
    """
    Configure the runtime context with the specified container runner.

    Parameters:
        runtime_context: The RuntimeContext to configure
        container_runner: 'docker', 'podman', 'singularity', 'udocker', or 'xcodon'
    """
    runtime_context.default_container = container_runner
    runtime_context.singularity = (container_runner == 'singularity')
    runtime_context.podman = (container_runner == 'podman')
    if container_runner == 'xcodon':
        import shutil
        runtime_context.user_space_docker_cmd = shutil.which('xcodon') or 'xcodon'
```

Run coala's tests: `cd /media/qhu/slim/Workspace/coala && .venv/bin/pytest -q`. Expected: same results as before the change. Commit on a branch:

```bash
git checkout -b xcodon-runner && git add coala/tool_logic.py && git commit -m "feat: xcodon container runner via cwltool user-space docker command"
```

- [ ] **Step 2: coala-runtime — write the failing test**

```python
# tests/test_engine_xcodon.py
import os

import pytest

from coala_runtime.runtime.engine import ContainerEngine, get_engine_from_env, make_container_manager


def test_xcodon_engine_value():
    assert ContainerEngine("xcodon") is ContainerEngine.XCODON


def test_env_selects_xcodon(monkeypatch):
    monkeypatch.setenv("COALA_CONTAINER_ENGINE", "xcodon")
    assert get_engine_from_env() is ContainerEngine.XCODON


def test_make_manager_returns_adapter(monkeypatch, tmp_path):
    pytest.importorskip("xcodon_runtime")
    monkeypatch.setenv("COALA_CONTAINER_ENGINE", "xcodon")
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(tmp_path))
    mgr = make_container_manager()
    assert type(mgr).__name__ == "XcodonContainerManager"
    assert mgr.system_site_packages_writable is True
```

Run: `cd /media/qhu/slim/Workspace/coala-runtime && .venv/bin/pytest tests/test_engine_xcodon.py -q` → FAIL with `AttributeError: XCODON`.

- [ ] **Step 3: coala-runtime — implement**

In `src/coala_runtime/runtime/engine.py`:

1. Add to the enum: `XCODON = "xcodon"`.
2. In `get_engine_from_env`, add `"xcodon": ContainerEngine.XCODON` to the mapping and `xcodon` to the "Valid values" message.
3. In `_autodetect_container_engine`, after the Podman check and before the Apptainer check, insert:

```python
    try:
        import xcodon_runtime  # noqa: F401
        logger.info("COALA_CONTAINER_ENGINE unset; using xcodon (no usable Docker/Podman on this host).")
        return ContainerEngine.XCODON
    except ImportError:
        pass
```

4. In `make_container_manager`, before the Singularity fallback, insert:

```python
    if engine == ContainerEngine.XCODON:
        from xcodon_runtime.coala_adapter import XcodonContainerManager

        return XcodonContainerManager()
```

In `src/coala_runtime/__main__.py`, add `xcodon` to `_ENGINE_CHOICES` and to the `--engine` help text.

In `pyproject.toml`, add an optional dependency group:

```toml
[project.optional-dependencies]
xcodon = ["xcodon-runtime>=0.1"]
```

Install the local package into coala-runtime's venv for testing: `.venv/bin/pip install -e /home/qhu/Workspace/xcodon-runtime`.

Run: `.venv/bin/pytest -q`. Expected: the new tests pass and the existing suite is unchanged.

- [ ] **Step 4: coala-runtime — end-to-end check**

```bash
cd /media/qhu/slim/Workspace/coala-runtime
COALA_CONTAINER_ENGINE=xcodon .venv/bin/python - <<'EOF'
import asyncio
from coala_runtime.runtime.engine import make_container_manager
async def main():
    m = make_container_manager()
    await m.ensure_image("python:3.12-slim")
    c = await m.create_container("python:3.12-slim", volumes={}, working_dir="/workspace")
    await m.start_container(c)
    print(await m.exec_command(c, "python -c 'import sys; print(sys.version)'"))
    print(await m.exec_command(c, "pip install --quiet cowsay && python -c 'import cowsay; print(cowsay.__name__)'"))
    await m.cleanup_all()
asyncio.run(main())
EOF
```
Expected: two tuples with exit code 0. The second proves that a pip install into the image's site-packages persists and imports within the same container.

- [ ] **Step 5: Update MCP_CONFIG.md and commit**

Add `xcodon` to the engine list in `MCP_CONFIG.md` with one sentence: "xcodon: rootless runtime with no daemon; install with `pip install 'coala-runtime[xcodon]'`; autodetected when Docker and Podman are unavailable."

```bash
git checkout -b xcodon-engine
git add src/coala_runtime/runtime/engine.py src/coala_runtime/__main__.py pyproject.toml MCP_CONFIG.md tests/test_engine_xcodon.py
git commit -m "feat: xcodon container engine"
```

---

## Plan Self-Review

**Spec coverage.**

| Spec section | Task |
|---|---|
| 3.1 reference resolution, lookup order | 1, 7, 12 (`resolve_image`, `--pull`) |
| 3.2 registry loader, token auth, platform, digests, zstd | 5, 3 |
| 3.2 daemon loader, nested index | 6 |
| 3.3 storage layout | 2, 7 |
| 3.4 extraction rules | 3 |
| 3.5 flattened rootfs, whiteouts, hardlinks | 4 |
| 3.6 locks | 2, 7 |
| 3.7 inspect JSON | 7, 13 |
| 4.1 container directory | 10, 12 |
| 4.2 spec builder | 8 |
| 4.3 engine interface, resolv.conf and hosts binds | 9, 10, 11 |
| 4.4 ns engine, handshake, remount flags, setns order, no-new-privs | 9, 10 |
| 4.5 proot engine, lookup order, reflink copy | 11 |
| 4.6 engine selection probes, XCODON_ENGINE | 9 |
| 5.1 CLI, ignored flags, exit codes, cidfile | 13 |
| 5.2 Python API | 12 |
| 5.3 coala integration | 16 |
| 5.4 coala-runtime adapter and enum | 14, 16 |
| 5.5 distribution, vendored PRoot, license | 1, 11 |
| 7 errors, atomic state, prune, logging, signal forwarding | 1, 2, 7, 12, 13 |
| 8 tests and CI | every task; 15 |

Gaps closed while reviewing: the spec's `Engine.exec(...) -> int` became `popen(...) -> Popen` plus `Runtime.exec(...) -> ExecResult`, because the API needs both streaming and capture. The spec listed `proot-aarch64`; the plan vendors x86_64 only and routes aarch64 through `XCODON_PROOT`, matching the updated spec text.

**Placeholder scan.** No TBD, TODO, "similar to", or "add error handling" steps remain. Every code step has its code.

**Type consistency.** `Bind(source, target, readonly)` is used identically in Tasks 9 to 14. `Container` fields set in Task 10 are the ones `ContainerStore.create` fills in Task 12 and the adapter reads in Task 14. `Engine.popen(container, argv, env, workdir, **kw)` has the same signature in `NsEngine`, `ProotEngine`, and `Runtime.popen`'s call. `FetchedImage`/`FetchedLayer` from Task 5 are consumed unchanged in Tasks 6, 7, and the Task 12 conftest helper. `run_probes()` returns `{name: {"ok", "error"}}` in Task 9 and is read that way in `select_engine` and conftest.
