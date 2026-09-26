# Environment Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep, per project, a record of the images a project used and the packages each install added: `<env folder>/environment.json`, plus `xrunner env record|show`.

**Architecture:** `pkgscan.py` finds packages from metadata files, with no commands run in any container. It covers a whole tree, an ns overlay upper applied over a base inventory, and the diff between two package sets. The image store keeps registry digests, docker `RepoDigests` and build Dockerfiles, and caches each image's package inventory. `envrecord.py` owns the record file: locking, image notes and the tag-move warning, layer entries, conda entries and the summary. `Runtime.create`, `Runtime.stop`, the conda shim and the CLI call it. A failure to record only warns.

**Tech Stack:** Python 3.10+, stdlib only. pytest. Fixtures come from tests/conftest.py: `home`, `busybox_image`, `engine_name` and `pack_rootfs_as_image(home, rootfs, ref, config=None)`. The fake registry is in tests/fake_registry.py, and the fake micromamba is in tests/fake_micromamba.py.

**Spec:** `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`, section 13.

## Global Constraints

- Python `>=3.10`, stdlib only. Plain English messages. Every error derives from `XcodonError`.
- The record file is `<env folder>/environment.json`. It is JSON with `sort_keys=True`, `indent=2` and a trailing newline, and it carries `"version": 1`. It has the sections `images`, `layers` and `conda`.
- Paths in the record are relative to the project root, the env folder's parent. Image names and URLs stay as they are.
- `images` is keyed by the ref as the caller wrote it. Each value holds:
  - `id` (`sha256:<hex>`);
  - `source` (`registry`, `daemon` or `build`; a store `commit` maps to `build`);
  - `repo_digests` (a sorted list);
  - `dockerfile` and `parent`, when known;
  - `first_used` and `last_used` (UTC, second precision);
  - `previous_ids`.
- `layers` is keyed by the image id hex, which is the `.xrunner-env/<hex>` folder name. Each value holds:
  - `ref` and `engine`;
  - `packages`, a list of `{"manager", "name", "version", "change", "location"}` plus `"url"` when known. `change` is one of `added`, `changed` or `removed`, sorted by (manager, location, name).
- `conda` is keyed by `name:<NAME>` or `path:<relative path>`. Each value holds:
  - `explicit`, the relative path;
  - `sha256`;
  - `packages`, the count of URL lines.
- Packages come only from metadata files:
  - **pip:** `*.dist-info` and `*.egg-info` whose parent is `site-packages` or `dist-packages`, read from `METADATA` or `PKG-INFO`. `url` comes from `direct_url.json`.
  - **R:** a directory holding both `DESCRIPTION` and `Meta/package.rds`. `url` comes from `Repository:`.
  - **apt:** `/var/lib/dpkg/status`, stanzas with `Status: install ok installed`. The name is `Package`, or `Package:Architecture` when the architecture is not `all`.
  - **conda:** `conda-meta/*.json`.
- Updates hold an exclusive flock on `<env folder>/.environment.lock`. Writes go through a temp file plus `os.replace`.
- A record failure logs a warning and never changes an exit code.
- Tag-move warning, one stderr line: `xrunner: <ref> now points to <new 12-hex>; installs made on <old 12-hex> stay in <env folder name>/<old hex>`.
- Existing behavior stays unchanged. The full suite is green, and `.venv/bin/ruff check src tests` is clean. Branch `feat/env-record`.
- Test commands: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q ...`. Use `/usr/bin/grep`.

## File Structure

```
src/xcodon_runtime/pkgscan.py      Pkg, parsers, scan_tree, ns_layer_packages, changes      (new, Task 1)
src/xcodon_runtime/registry.py     FetchedImage.repo_digests; top-level manifest digest     (Task 2)
src/xcodon_runtime/daemon.py       repo_digests(ref) via docker image inspect              (Task 2)
src/xcodon_runtime/imagestore.py   repo_digests into manifest.json; manifest(); annotate(); package_inventory()  (Task 2)
src/xcodon_runtime/build.py        annotate the final image with its Dockerfile             (Task 2)
src/xcodon_runtime/envrecord.py    load, update, note_image, record_layer, record_conda, record_all, show  (new, Task 3)
src/xcodon_runtime/condaroot.py    find_env_folder(cwd, environ)                            (Task 4)
src/xcodon_runtime/api.py          create/stop hooks                                        (Task 4)
src/xcodon_runtime/condashim.py    conda hook                                               (Task 4)
src/xcodon_runtime/cli.py          `xrunner env record|show`                                (Task 4)
tests/test_pkgscan.py, tests/test_store_metadata.py, tests/test_envrecord.py, tests/test_envrecord_wiring.py  (new)
README.md                                                                                    (Task 4)
```

---

### Task 1: Package scanner

**Files:**
- Create: `src/xcodon_runtime/pkgscan.py`
- Test: `tests/test_pkgscan.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Pkg`, with fields `manager: str`, `name: str`, `version: str`, `location: str`, `path: str` and `url: str | None = None`. Its property `key` returns `(manager, location, name)`.
  - `pkg_to_json(p) -> dict` and `pkg_from_json(d) -> Pkg`. These are for the inventory cache and keep `path`.
  - `parse_dpkg_status(text: str) -> list[Pkg]`.
  - `scan_tree(root: Path) -> list[Pkg]`.
  - `ns_layer_packages(upper: Path, base: list[Pkg]) -> list[Pkg]`.
  - `changes(base: list[Pkg], merged: list[Pkg]) -> list[dict]`.
  - `is_whiteout(path: Path, st: os.stat_result) -> bool` and `is_opaque(path: Path) -> bool`. These are module-level, so tests can monkeypatch them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pkgscan.py
import json
import os
import stat
from pathlib import Path

import pytest

from xcodon_runtime import pkgscan
from xcodon_runtime.pkgscan import Pkg, changes, ns_layer_packages, parse_dpkg_status, scan_tree

SP = "usr/local/lib/python3.12/site-packages"


def put(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def pip_pkg(root: Path, name: str, version: str, sp: str = SP, url: str | None = None) -> None:
    d = f"{sp}/{name}-{version}.dist-info"
    put(root, f"{d}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nbody\n")
    if url:
        put(root, f"{d}/direct_url.json", json.dumps({"url": url}))


def r_pkg(root: Path, lib: str, name: str, version: str) -> None:
    put(root, f"{lib}/{name}/DESCRIPTION", f"Package: {name}\nVersion: {version}\nRepository: CRAN\n")
    put(root, f"{lib}/{name}/Meta/package.rds", "x")


STATUS = """Package: adduser
Status: install ok installed
Architecture: all
Version: 3.134

Package: libc6
Status: install ok installed
Architecture: amd64
Version: 2.36-9

Package: gone
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0
"""


def test_parse_dpkg_status():
    pkgs = parse_dpkg_status(STATUS)
    assert [(p.name, p.version) for p in pkgs] == [("adduser", "3.134"), ("libc6:amd64", "2.36-9")]
    assert {p.location for p in pkgs} == {"/var/lib/dpkg"}


def test_scan_tree_finds_every_manager_and_ignores_caches(tmp_path):
    root = tmp_path / "rootfs"
    pip_pkg(root, "numpy", "2.1.0", url="https://files.pythonhosted.org/numpy.whl")
    pip_pkg(root, "et_xmlfile", "2.0.0", sp="root/.cache/uv/archive-v0/abc")  # a cache, not an install
    put(root, "usr/lib/python3/dist-packages/six-1.16.0.egg-info", "Name: six\nVersion: 1.16.0\n")
    r_pkg(root, "usr/local/lib/R/site-library", "dplyr", "1.1.4")
    put(root, "usr/local/lib/R/site-library/srcpkg/DESCRIPTION", "Package: srcpkg\nVersion: 0.1\n")  # no Meta/
    put(root, "opt/conda/conda-meta/zlib-1.3.1-0.json",
        json.dumps({"name": "zlib", "version": "1.3.1", "url": "https://conda.anaconda.org/conda-forge/zlib.conda"}))
    put(root, "var/lib/dpkg/status", STATUS)
    got = {(p.manager, p.name, p.version, p.location, p.url) for p in scan_tree(root)}
    assert got == {
        ("pip", "numpy", "2.1.0", "/" + SP, "https://files.pythonhosted.org/numpy.whl"),
        ("pip", "six", "1.16.0", "/usr/lib/python3/dist-packages", None),
        ("R", "dplyr", "1.1.4", "/usr/local/lib/R/site-library", "CRAN"),
        ("conda", "zlib", "1.3.1", "/opt/conda", "https://conda.anaconda.org/conda-forge/zlib.conda"),
        ("apt", "adduser", "3.134", "/var/lib/dpkg", None),
        ("apt", "libc6:amd64", "2.36-9", "/var/lib/dpkg", None),
    }


def test_unreadable_metadata_is_skipped(tmp_path):
    root = tmp_path / "rootfs"
    put(root, f"{SP}/broken-1.0.dist-info/METADATA", "no headers here")
    put(root, "opt/conda/conda-meta/bad.json", "{not json")
    assert scan_tree(root) == []


def test_changes_classifies_added_changed_removed():
    base = [Pkg("pip", "a", "1", "/sp", "/sp/a-1.dist-info"), Pkg("pip", "b", "1", "/sp", "/sp/b-1.dist-info")]
    merged = [Pkg("pip", "a", "2", "/sp", "/sp/a-2.dist-info"), Pkg("pip", "c", "3", "/sp", "/sp/c-3.dist-info", "u")]
    assert changes(base, merged) == [
        {"manager": "pip", "name": "a", "version": "2", "change": "changed", "location": "/sp"},
        {"manager": "pip", "name": "b", "version": "1", "change": "removed", "location": "/sp"},
        {"manager": "pip", "name": "c", "version": "3", "change": "added", "location": "/sp", "url": "u"},
    ]


def test_ns_upper_adds_replaces_and_whiteouts(tmp_path, monkeypatch):
    base_root = tmp_path / "base"
    pip_pkg(base_root, "old", "1.0")
    pip_pkg(base_root, "keep", "1.0")
    pip_pkg(base_root, "bump", "1.0")
    put(base_root, "var/lib/dpkg/status", STATUS)
    base = scan_tree(base_root)
    upper = tmp_path / "upper"
    pip_pkg(upper, "new", "2.0")
    pip_pkg(upper, "bump", "1.1")
    put(upper, f"{SP}/old-1.0.dist-info", "")      # stands in for a whiteout (char device 0:0)
    put(upper, f"{SP}/bump-1.0.dist-info", "")     # pip replaced bump 1.0 with 1.1
    put(upper, "var/lib/dpkg/status", STATUS.replace("Version: 3.134", "Version: 3.135"))
    monkeypatch.setattr(pkgscan, "is_whiteout",
                        lambda p, st: stat.S_ISREG(st.st_mode) and st.st_size == 0 and p.name.endswith(".dist-info"))
    got = {(c["manager"], c["name"], c["version"], c["change"]) for c in changes(base, ns_layer_packages(upper, base))}
    assert got == {("pip", "new", "2.0", "added"), ("pip", "bump", "1.1", "changed"),
                   ("pip", "old", "1.0", "removed"), ("apt", "adduser", "3.135", "changed")}


def test_ns_opaque_directory_hides_base_packages(tmp_path):
    base_root = tmp_path / "base"
    pip_pkg(base_root, "old", "1.0")
    upper = tmp_path / "upper"
    pip_pkg(upper, "fresh", "1.0")
    try:
        os.setxattr(upper / SP, "user.overlay.opaque", b"y")
    except OSError:
        pytest.skip("user xattrs unsupported here")
    base = scan_tree(base_root)
    got = {(c["name"], c["change"]) for c in changes(base, ns_layer_packages(upper, base))}
    assert got == {("fresh", "added"), ("old", "removed")}


def test_pkg_json_round_trip():
    p = Pkg("R", "x", "1", "/lib", "/lib/x", "CRAN")
    assert pkgscan.pkg_from_json(pkgscan.pkg_to_json(p)) == p
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_pkgscan.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.pkgscan'`

- [ ] **Step 3: Write pkgscan.py**

```python
# src/xcodon_runtime/pkgscan.py
"""Find installed packages from their metadata files. See spec section 13.3.

Nothing here runs a command in a container: packages are read from pip,
R, dpkg and conda metadata files, so a stopped layer can be read, on either
engine, and code in an image cannot steer the result.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

PIP_PARENTS = ("site-packages", "dist-packages")
DPKG_STATUS = "/var/lib/dpkg/status"
OPAQUE_XATTRS = ("user.overlay.opaque", "trusted.overlay.opaque")
_SKIP_TOP = {"proc", "sys", "dev"}
_MAX_META = 1 << 20


@dataclass(frozen=True)
class Pkg:
    manager: str
    name: str
    version: str
    location: str
    path: str
    url: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.manager, self.location, self.name)


def pkg_to_json(p: Pkg) -> dict:
    return asdict(p)


def pkg_from_json(d: dict) -> Pkg:
    return Pkg(d["manager"], d["name"], d["version"], d["location"], d["path"], d.get("url"))


def is_whiteout(path: Path, st: os.stat_result) -> bool:
    """An overlay whiteout: a character device 0:0."""
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == 0


def is_opaque(path: Path) -> bool:
    for name in OPAQUE_XATTRS:
        try:
            if os.getxattr(path, name, follow_symlinks=False) == b"y":
                return True
        except OSError:
            continue
    return False


def _read(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_META).decode("utf-8", errors="replace")
    except OSError:
        return None


def _headers(text: str | None) -> dict[str, str]:
    """`Key: value` headers up to the first blank line, with indented continuation lines."""
    out: dict[str, str] = {}
    last = None
    for line in (text or "").splitlines():
        if not line.strip():
            break
        if line[0] in " \t" and last:
            out[last] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        if sep and key and " " not in key.strip():
            last = key.strip()
            out.setdefault(last, value.strip())
    return out


def _parent(cpath: str) -> str:
    return str(PurePosixPath(cpath).parent)


def _pip(host: Path, cpath: str) -> Pkg | None:
    if host.is_dir():
        meta = host / ("METADATA" if host.name.endswith(".dist-info") else "PKG-INFO")
    else:
        meta = host
    h = _headers(_read(meta))
    if not h.get("Name") or not h.get("Version"):
        log.debug("skipping unreadable pip metadata %s", host)
        return None
    url = None
    direct = host / "direct_url.json"
    if host.is_dir() and direct.is_file():
        try:
            url = json.loads(_read(direct) or "{}").get("url")
        except ValueError:
            url = None
    return Pkg("pip", h["Name"], h["Version"], _parent(cpath), cpath, url)


def _r(host: Path, cpath: str) -> Pkg | None:
    h = _headers(_read(host / "DESCRIPTION"))
    if not h.get("Package") or not h.get("Version"):
        log.debug("skipping unreadable R DESCRIPTION in %s", host)
        return None
    return Pkg("R", h["Package"], h["Version"], _parent(cpath), cpath, h.get("Repository"))


def _conda(host: Path, cpath: str) -> Pkg | None:
    try:
        d = json.loads(_read(host) or "")
    except ValueError:
        log.debug("skipping unreadable conda metadata %s", host)
        return None
    if not isinstance(d, dict) or not d.get("name") or not d.get("version"):
        return None
    return Pkg("conda", str(d["name"]), str(d["version"]), _parent(_parent(cpath)), cpath, d.get("url"))


def parse_dpkg_status(text: str) -> list[Pkg]:
    out = []
    for stanza in text.split("\n\n"):
        h = _headers(stanza.strip("\n"))
        if h.get("Status") != "install ok installed" or not h.get("Package") or not h.get("Version"):
            continue
        arch = h.get("Architecture", "")
        name = h["Package"] if arch in ("", "all") else f"{h['Package']}:{arch}"
        out.append(Pkg("apt", name, h["Version"], _parent(DPKG_STATUS), DPKG_STATUS))
    return out


def _classify(host: Path, cpath: str, is_dir: bool) -> list[Pkg] | None:
    """Packages described by this entry, [] for unreadable metadata, None if it is not metadata."""
    p = PurePosixPath(cpath)
    if p.parent.name in PIP_PARENTS and (p.name.endswith(".dist-info") or p.name.endswith(".egg-info")):
        pkg = _pip(host, cpath)
        return [pkg] if pkg else []
    if is_dir and (host / "DESCRIPTION").is_file() and (host / "Meta" / "package.rds").is_file():
        pkg = _r(host, cpath)
        return [pkg] if pkg else []
    if not is_dir and p.parent.name == "conda-meta" and p.name.endswith(".json"):
        pkg = _conda(host, cpath)
        return [pkg] if pkg else []
    if not is_dir and cpath == DPKG_STATUS:
        return parse_dpkg_status(_read(host) or "")
    return None


def _cjoin(rel_dir: str, name: str) -> str:
    return (rel_dir.rstrip("/") or "") + "/" + name


def scan_tree(root: Path) -> list[Pkg]:
    """Every package whose metadata sits under ``root``, a whole rootfs."""
    found: dict[tuple[str, str, str], Pkg] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        rel_dir = "/" if rel == "." else "/" + rel
        if rel_dir == "/":
            dirnames[:] = [d for d in dirnames if d not in _SKIP_TOP]
        dirnames.sort()
        for name in list(dirnames) + sorted(filenames):
            host = Path(dirpath) / name
            is_dir = name in dirnames and not host.is_symlink()
            if host.is_symlink():
                continue
            pkgs = _classify(host, _cjoin(rel_dir, name), is_dir)
            if pkgs is None:
                continue
            for pkg in pkgs:
                found[pkg.key] = pkg
            if is_dir:
                dirnames.remove(name)
    return sorted(found.values(), key=lambda p: p.key)


def _drop_under(merged: dict, cprefix: str) -> None:
    prefix = cprefix.rstrip("/") + "/"
    for key, pkg in list(merged.items()):
        if pkg.path == cprefix or pkg.path.startswith(prefix):
            del merged[key]


def ns_layer_packages(upper: Path, base: list[Pkg]) -> list[Pkg]:
    """The packages visible through an overlay: ``base`` with the upper's changes applied."""
    merged = {p.key: p for p in base}
    for dirpath, dirnames, filenames in os.walk(upper, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, upper)
        rel_dir = "/" if rel == "." else "/" + rel
        if rel_dir != "/" and is_opaque(Path(dirpath)):
            _drop_under(merged, rel_dir)
        dirnames.sort()
        for name in list(dirnames) + sorted(filenames):
            host = Path(dirpath) / name
            cpath = _cjoin(rel_dir, name)
            try:
                st = os.lstat(host)
            except OSError:
                continue
            if is_whiteout(host, st):
                _drop_under(merged, cpath)
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            pkgs = _classify(host, cpath, is_dir)
            if pkgs is None:
                continue
            _drop_under(merged, cpath)
            for pkg in pkgs:
                merged[pkg.key] = pkg
            if is_dir:
                dirnames.remove(name)
    return sorted(merged.values(), key=lambda p: p.key)


def _entry(p: Pkg, change: str) -> dict:
    e = {"manager": p.manager, "name": p.name, "version": p.version, "change": change, "location": p.location}
    if p.url:
        e["url"] = p.url
    return e


def changes(base: list[Pkg], merged: list[Pkg]) -> list[dict]:
    b = {p.key: p for p in base}
    m = {p.key: p for p in merged}
    out = []
    for key in sorted(set(b) | set(m)):
        if key not in b:
            out.append(_entry(m[key], "added"))
        elif key not in m:
            out.append(_entry(b[key], "removed"))
        elif b[key].version != m[key].version:
            out.append(_entry(m[key], "changed"))
    return out
```

Note on the whiteout test: the test stands in for whiteouts with empty regular files by monkeypatching `is_whiteout`. That check runs before classification, so the stand-in is dropped and never read as metadata. Real char devices are covered by Task 4's container test.

- [ ] **Step 4: Run the tests, the full suite, ruff; commit**

```bash
git add src/xcodon_runtime/pkgscan.py tests/test_pkgscan.py
git commit -m "feat: find pip, R, apt and conda packages from metadata files in a tree or an overlay upper"
```

---

### Task 2: Image metadata in the store

**Files:**
- Modify: `src/xcodon_runtime/registry.py`, `src/xcodon_runtime/daemon.py`, `src/xcodon_runtime/imagestore.py`, `src/xcodon_runtime/build.py`
- Test: `tests/test_store_metadata.py`

**Interfaces:**
- Consumes: `pkgscan.scan_tree`, `pkg_to_json`, `pkg_from_json`, `Pkg`.
- Produces:
  - `FetchedImage.repo_digests: list[str]`, which defaults to empty.
  - `DaemonSource.repo_digests(ref) -> list[str]`.
  - `ImageStore.manifest(image: Image) -> dict`.
  - `ImageStore.annotate(image_id: str, **fields) -> None`. `repo_digests` values are merged as a sorted unique list; any other field is replaced.
  - `ImageStore.package_inventory(image: Image) -> list[Pkg]`, cached at `<image dir>/packages.json` as `{"version": 1, "packages": [...]}`.
  - `manifest.json` gains three keys: `repo_digests` (a list), `dockerfile` (for the final image of `xrunner build`), and the existing `parent` for commits.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_metadata.py
import hashlib
import json

import pytest

from tests.fake_registry import FakeRegistry, digest_of
from tests.test_registry import config_for, layer_bytes
from xcodon_runtime.api import Runtime
from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform, Reference
from xcodon_runtime.registry import RegistryClient


@pytest.fixture
def reg():
    with FakeRegistry() as r:
        yield r


def test_registry_fetch_reports_the_tags_manifest_digest(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    client = RegistryClient(home, scheme="http")
    f1 = client.fetch(Reference(reg.host, "lib/hello", "latest"), Platform("linux", "amd64"))
    assert f1.repo_digests == [f"{reg.host}/lib/hello@{digest_of(reg.manifests[('lib/hello', 'latest')][0])}"]
    f2 = client.fetch(Reference(reg.host, "lib/multi", "v1"), Platform("linux", "amd64"))
    index_digest = digest_of(reg.manifests[("lib/multi", "v1")][0])
    assert f2.repo_digests == [f"{reg.host}/lib/multi@{index_digest}"], "the tag's own (index) digest, as docker records it"


def test_pull_keeps_repo_digests_in_the_manifest(home, reg):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/hello", "latest", config_for(layers), layers)
    st = ImageStore(home, sources=[RegistryClient(home, scheme="http")])
    img = st.pull(f"{reg.host}/lib/hello:latest")
    assert st.manifest(img)["repo_digests"] == [f"{reg.host}/lib/hello@{digest_of(reg.manifests[('lib/hello', 'latest')][0])}"]


def test_annotate_merges_repo_digests_and_replaces_other_fields(home, busybox_image):
    st = ImageStore(home, sources=[])
    st.annotate(busybox_image.id, repo_digests=["r/x@sha256:" + "b" * 64])
    st.annotate(busybox_image.id, repo_digests=["r/x@sha256:" + "a" * 64, "r/x@sha256:" + "b" * 64], dockerfile="FROM x\n")
    st.annotate(busybox_image.id, dockerfile="FROM y\n")
    m = st.manifest(busybox_image)
    assert m["repo_digests"] == ["r/x@sha256:" + "a" * 64, "r/x@sha256:" + "b" * 64]
    assert m["dockerfile"] == "FROM y\n"
    assert m["config"].endswith(busybox_image.id), "existing fields are kept"


def test_daemon_repo_digests(home, monkeypatch):
    src = DaemonSource(home)
    monkeypatch.setattr(src, "_exe", lambda: "/usr/bin/docker")

    class R:
        returncode = 0
        stdout = '["docker.io/hubentu/coala-runtime-python@sha256:' + "c" * 64 + '"]\n'

    monkeypatch.setattr("xcodon_runtime.daemon.subprocess.run", lambda *a, **k: R())
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == \
        ["docker.io/hubentu/coala-runtime-python@sha256:" + "c" * 64]
    R.stdout = "null\n"
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == []
    R.returncode = 1
    assert src.repo_digests(Reference("docker.io", "library/x", "latest")) == []


def test_build_records_its_dockerfile(home, busybox_image, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    text = "FROM xcodon-test/busybox\nENV A=1\n"
    (ctx / "Dockerfile").write_text(text)
    img = rt.build(ctx, tags=["xcodon-test/rec:1"])
    m = rt.images.manifest(img)
    assert m["dockerfile"] == text and m["source"] == "commit"


def test_package_inventory_is_cached(home, tmp_path):
    from tests.conftest import pack_rootfs_as_image

    root = tmp_path / "rootfs"
    meta = root / "usr/lib/python3/site-packages/demo-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Name: demo\nVersion: 1.0\n")
    img = pack_rootfs_as_image(home, root, "xcodon-test/inv:1")
    st = ImageStore(home, sources=[])
    first = st.package_inventory(img)
    assert [(p.manager, p.name, p.version) for p in first] == [("pip", "demo", "1.0")]
    cache = json.loads((img.dir / "packages.json").read_text())
    assert cache["version"] == 1 and cache["packages"][0]["path"].endswith("demo-1.0.dist-info")
    (img.rootfs / "usr/lib/python3/site-packages/demo-1.0.dist-info/METADATA").unlink()
    assert st.package_inventory(img) == first, "the cached inventory is used; the rootfs is not scanned again"
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_store_metadata.py`
Expected: failures on `repo_digests`, `manifest` and `annotate` attributes.

- [ ] **Step 3: Registry**

In `registry.py`, add the field to `FetchedImage`:

```python
    # Where this image lives in a registry, as `name@sha256:<manifest digest>`:
    # the digest of the tag's own manifest (an index for multi-arch images, as
    # docker's RepoDigests records it). Empty when unknown.
    repo_digests: list[str] = field(default_factory=list)
```

Replace `_manifest` with a pair:

```python
    def _manifest_and_digest(self, ref: Reference, manifest_ref: str) -> tuple[dict, str]:
        with self._get(ref, f"manifests/{manifest_ref}", MANIFEST_ACCEPT) as r:
            body = r.read()
            media_type = r.headers.get("Content-Type", "").split(";")[0].strip()
            digest = r.headers.get("Docker-Content-Digest") or ""
        if not digest.startswith("sha256:"):
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
        data = json.loads(body)
        data.setdefault("mediaType", media_type)
        return data, digest

    def _manifest(self, ref: Reference, manifest_ref: str) -> dict:
        return self._manifest_and_digest(ref, manifest_ref)[0]
```

In `fetch`, take the first manifest with `manifest, top_digest = self._manifest_and_digest(ref, ref.manifest_ref)`. Return with `FetchedImage(..., source=self.name, repo_digests=[f"{ref.registry}/{ref.repository}@{top_digest}"])`. Import `hashlib` if it is not already imported. Check that `Reference` has `.registry` and `.repository`, as `Reference.name` uses them.

- [ ] **Step 4: Daemon**

In `daemon.py`, add the following next to `image_id`:

```python
    def repo_digests(self, ref: Reference) -> list[str]:
        """docker's RepoDigests for a tag: registry copies of this image. Empty when none or unknown."""
        exe = self._exe()
        if not exe:
            return []
        try:
            r = subprocess.run([exe, "image", "inspect", "--format", "{{json .RepoDigests}}", ref.name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        try:
            value = json.loads(r.stdout.strip() or "null")
        except ValueError:
            return []
        return sorted({v for v in value or [] if isinstance(v, str) and "@sha256:" in v})
```

In `fetch`, next to `fetched.daemon_id = daemon_id`, set `fetched.repo_digests = self.repo_digests(ref)`. Read it before `docker save`, alongside `daemon_id`. Import `json` if it is not already imported.

- [ ] **Step 5: Store**

In `imagestore.py`:

```python
    def manifest(self, image: Image) -> dict:
        try:
            return json.loads((image.dir / "manifest.json").read_text())
        except (OSError, ValueError):
            return {}

    def annotate(self, image_id: str, **fields) -> None:
        """Merge fields into an image's manifest.json. repo_digests merge; other fields replace."""
        image_dir = self.home.images / image_id
        with self.home.lock(f"image-{image_id}"):
            path = image_dir / "manifest.json"
            manifest = json.loads(path.read_text())
            for key, value in fields.items():
                if key == "repo_digests":
                    manifest[key] = sorted(set(manifest.get(key, [])) | set(value))
                else:
                    manifest[key] = value
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(manifest, indent=2))
            os.replace(tmp, path)

    def package_inventory(self, image: Image) -> list[Pkg]:
        """The packages in an image's rootfs, scanned once and cached in packages.json."""
        cache = image.dir / "packages.json"
        try:
            data = json.loads(cache.read_text())
            if data.get("version") == 1:
                return [pkg_from_json(d) for d in data["packages"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        pkgs = scan_tree(image.rootfs)
        with self.home.lock(f"image-{image.id}"):
            tmp = cache.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": 1, "packages": [pkg_to_json(p) for p in pkgs]}, indent=2))
            os.replace(tmp, cache)
        return pkgs
```

Import `from xcodon_runtime.pkgscan import Pkg, pkg_from_json, pkg_to_json, scan_tree`, and `os` if it is missing. In `import_fetched`:
- when writing a new manifest, add `if fetched.repo_digests: manifest["repo_digests"] = sorted(set(fetched.repo_digests))`;
- when the image dir already exists and `fetched.repo_digests` is non-empty, call `self.annotate(image_id, repo_digests=fetched.repo_digests)` after the image lock is released. `annotate` takes the same lock, so do not call it while holding it.

- [ ] **Step 6: Build**

In `build.py` `Builder.build`, right before the loop that tags the final image, add `self.rt.images.annotate(image.id, dockerfile=dockerfile_text)`. Re-read the image afterwards if later code uses its manifest.

- [ ] **Step 7: Run the tests, the full suite, ruff; commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_store_metadata.py tests/test_registry.py tests/test_imagestore.py tests/test_daemon.py tests/test_build.py`, then the full suite and ruff.

```bash
git add src/xcodon_runtime/registry.py src/xcodon_runtime/daemon.py src/xcodon_runtime/imagestore.py src/xcodon_runtime/build.py tests/test_store_metadata.py
git commit -m "feat: the image store keeps registry digests, docker RepoDigests, build Dockerfiles and a package inventory"
```

---

### Task 3: The record file

**Files:**
- Create: `src/xcodon_runtime/envrecord.py`
- Test: `tests/test_envrecord.py`

**Interfaces:**
- Consumes:
  - `ImageStore.manifest`, `ImageStore.package_inventory` and `ImageStore.get`;
  - `pkgscan.ns_layer_packages`, `scan_tree` and `changes`;
  - `envdir.ENV_INFO_NAME` (`"image.json"`).
- Produces:
  - constants `RECORD_NAME = "environment.json"`, `LOCK_NAME = ".environment.lock"`, `VERSION = 1`;
  - `empty() -> dict`;
  - `load(env_dir: Path) -> dict`;
  - `update(env_dir: Path, fn: Callable[[dict], None]) -> Path`;
  - `image_entry(store, image) -> dict`;
  - `note_image(env_dir: Path, ref: str, image, store, err: TextIO | None = None) -> None`;
  - `record_layer(env_dir: Path, image_id: str, store) -> None`;
  - `record_conda(env_dir: Path) -> None`;
  - `record_all(env_dir: Path, store) -> Path`;
  - `show(env_dir: Path) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_envrecord.py
import hashlib
import io
import json
import threading
from pathlib import Path

import pytest

from tests.conftest import pack_rootfs_as_image
from xcodon_runtime import envrecord
from xcodon_runtime.imagestore import ImageStore

SP = "usr/lib/python3/site-packages"


def pip_meta(root: Path, name: str, version: str) -> None:
    d = root / SP / f"{name}-{version}.dist-info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")


@pytest.fixture
def img(home, tmp_path):
    root = tmp_path / "rootfs"
    pip_meta(root, "base", "1.0")
    return pack_rootfs_as_image(home, root, "xcodon-test/rec:latest")


@pytest.fixture
def env_dir(tmp_path):
    d = tmp_path / "proj" / ".xrunner-env"
    d.mkdir(parents=True)
    return d


def make_layer(env_dir: Path, image, ref: str, engine: str = "ns") -> Path:
    layer = env_dir / image.id
    (layer / ("upper" if engine == "ns" else "rootfs")).mkdir(parents=True)
    (layer / "image.json").write_text(json.dumps({"image_ref": ref, "image_id": image.id, "engine": engine,
                                                  "first_used": "2026-09-25T00:00:00+00:00"}))
    return layer


def test_empty_and_load_tolerate_missing_or_corrupt(env_dir):
    assert envrecord.load(env_dir) == envrecord.empty() == {"version": 1, "images": {}, "layers": {}, "conda": {}}
    (env_dir / "environment.json").write_text("{broken")
    assert envrecord.load(env_dir) == envrecord.empty()


def test_update_writes_sorted_json_atomically(env_dir):
    path = envrecord.update(env_dir, lambda doc: doc["conda"].update({"name:b": {"x": 1}, "name:a": {"x": 2}}))
    text = path.read_text()
    assert path == env_dir / "environment.json" and text.endswith("\n")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    assert not list(env_dir.glob("*.tmp")) and not list(env_dir.glob(".environment-*"))


def test_concurrent_updates_do_not_lose_writes(env_dir):
    def add(i):
        envrecord.update(env_dir, lambda doc: doc["conda"].__setitem__(f"name:e{i}", {"n": i}))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(envrecord.load(env_dir)["conda"]) == 8


def test_note_image_and_tag_move(home, img, env_dir, tmp_path):
    st = ImageStore(home, sources=[])
    st.annotate(img.id, repo_digests=["docker.io/x/rec@sha256:" + "d" * 64])
    err = io.StringIO()
    envrecord.note_image(env_dir, "rec:latest", img, st, err=err)
    e = envrecord.load(env_dir)["images"]["rec:latest"]
    assert e["id"] == "sha256:" + img.id and e["source"] in ("daemon", "registry", "build")
    assert e["repo_digests"] == ["docker.io/x/rec@sha256:" + "d" * 64]
    assert e["previous_ids"] == [] and e["first_used"] <= e["last_used"] and err.getvalue() == ""
    root2 = tmp_path / "rootfs2"
    pip_meta(root2, "base", "2.0")
    img2 = pack_rootfs_as_image(home, root2, "xcodon-test/rec:latest")
    envrecord.note_image(env_dir, "rec:latest", img2, st, err=err)
    e = envrecord.load(env_dir)["images"]["rec:latest"]
    assert e["id"] == "sha256:" + img2.id and e["previous_ids"] == ["sha256:" + img.id]
    assert err.getvalue() == (f"xrunner: rec:latest now points to {img2.id[:12]}; installs made on "
                              f"{img.id[:12]} stay in .xrunner-env/{img.id}\n")


def test_image_entry_maps_commit_to_build(home, img):
    st = ImageStore(home, sources=[])
    st.annotate(img.id, source="commit", parent="ab" * 32, dockerfile="FROM x\n")
    e = envrecord.image_entry(st, img)
    assert (e["source"], e["parent"], e["dockerfile"]) == ("build", "sha256:" + "ab" * 32, "FROM x\n")


def test_record_layer_ns(home, img, env_dir):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    envrecord.record_layer(env_dir, img.id, st)
    entry = envrecord.load(env_dir)["layers"][img.id]
    assert entry["ref"] == "rec:latest" and entry["engine"] == "ns"
    assert entry["packages"] == [{"manager": "pip", "name": "added", "version": "3.0", "change": "added",
                                  "location": "/" + SP}]


def test_record_layer_proot(home, img, env_dir):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "proot")
    pip_meta(layer / "rootfs", "base", "1.0")
    pip_meta(layer / "rootfs", "extra", "0.1")
    envrecord.record_layer(env_dir, img.id, st)
    assert [(p["name"], p["change"]) for p in envrecord.load(env_dir)["layers"][img.id]["packages"]] == [("extra", "added")]


def test_record_layer_with_a_missing_image_warns_and_keeps_going(home, env_dir, caplog):
    st = ImageStore(home, sources=[])
    fake_id = "f" * 64
    (env_dir / fake_id / "upper").mkdir(parents=True)
    envrecord.record_layer(env_dir, fake_id, st)
    assert fake_id not in envrecord.load(env_dir)["layers"]
    assert "not in the image store" in caplog.text


def explicit(prefix: Path, urls: list[str]) -> str:
    (prefix / "conda-meta").mkdir(parents=True, exist_ok=True)
    text = "# platform: linux-64\n@EXPLICIT\n" + "".join(u + "\n" for u in urls)
    (prefix / "conda-explicit.txt").write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def test_record_conda(env_dir, tmp_path):
    proj = env_dir.parent
    root = env_dir / "conda"
    outside = tmp_path / "outside-env"
    s1 = explicit(root / "envs" / "bwa_env", ["https://c/a.conda#1", "https://c/b.conda#2"])
    s2 = explicit(proj / "workspace" / "conda_env", ["https://c/a.conda#1"])
    explicit(outside, ["https://c/z.conda#9"])
    (root / ".home" / ".conda").mkdir(parents=True)
    (root / ".home" / ".conda" / "environments.txt").write_text(
        f"{proj / 'workspace' / 'conda_env'}\n{outside}\n")
    envrecord.record_conda(env_dir)
    assert envrecord.load(env_dir)["conda"] == {
        "name:bwa_env": {"explicit": ".xrunner-env/conda/envs/bwa_env/conda-explicit.txt", "sha256": s1, "packages": 2},
        "path:workspace/conda_env": {"explicit": "workspace/conda_env/conda-explicit.txt", "sha256": s2, "packages": 1},
    }


def test_record_all_rebuilds_from_disk_and_show(home, img, env_dir, tmp_path):
    st = ImageStore(home, sources=[])
    layer = make_layer(env_dir, img, "rec:latest", "ns")
    pip_meta(layer / "upper", "added", "3.0")
    explicit(env_dir / "conda" / "envs" / "tools", ["https://c/a.conda#1"])
    path = envrecord.record_all(env_dir, st)
    doc = json.loads(path.read_text())
    assert set(doc["images"]) == {"rec:latest"} and set(doc["layers"]) == {img.id} and set(doc["conda"]) == {"name:tools"}
    text = envrecord.show(env_dir)
    assert "rec:latest" in text and img.id[:12] in text and "added 3.0" in text and "name:tools" in text
```

`pack_rootfs_as_image` imports through `import_fetched`, so the image's `source` is whatever that helper sets. The test accepts any of the three values.

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_envrecord.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.envrecord'`

- [ ] **Step 3: Write envrecord.py**

```python
# src/xcodon_runtime/envrecord.py
"""The project's environment record, <env folder>/environment.json. See spec section 13."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from xcodon_runtime.envdir import ENV_INFO_NAME
from xcodon_runtime.pkgscan import changes, ns_layer_packages, scan_tree

log = logging.getLogger(__name__)

RECORD_NAME = "environment.json"
LOCK_NAME = ".environment.lock"
VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCES = {"registry": "registry", "daemon": "daemon", "commit": "build"}


def empty() -> dict:
    return {"version": VERSION, "images": {}, "layers": {}, "conda": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(env_dir: Path) -> dict:
    path = Path(env_dir) / RECORD_NAME
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable environment record %s: %s", path, e)
        return empty()
    base = empty()
    if isinstance(doc, dict):
        for key in ("images", "layers", "conda"):
            if isinstance(doc.get(key), dict):
                base[key] = doc[key]
    return base


@contextmanager
def _locked(env_dir: Path) -> Iterator[None]:
    fd = os.open(Path(env_dir) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def update(env_dir: Path, fn: Callable[[dict], None]) -> Path:
    env_dir = Path(env_dir)
    path = env_dir / RECORD_NAME
    with _locked(env_dir):
        doc = load(env_dir)
        fn(doc)
        doc["version"] = VERSION
        fd, tmp = tempfile.mkstemp(dir=env_dir, prefix=".environment-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return path


def image_entry(store, image) -> dict:
    m = store.manifest(image)
    source = m.get("source") or "unknown"
    e = {"id": "sha256:" + image.id, "source": _SOURCES.get(source, source),
         "repo_digests": sorted(set(m.get("repo_digests", [])))}
    if m.get("dockerfile"):
        e["dockerfile"] = m["dockerfile"]
    if m.get("parent"):
        parent = m["parent"]
        e["parent"] = parent if parent.startswith("sha256:") else "sha256:" + parent
    return e


def note_image(env_dir: Path, ref: str, image, store, err: TextIO | None = None) -> None:
    env_dir = Path(env_dir)
    new = image_entry(store, image)
    now = _now()

    def fn(doc: dict) -> None:
        cur = doc["images"].get(ref)
        if cur and cur.get("id") and cur["id"] != new["id"]:
            prev = list(cur.get("previous_ids", []))
            if cur["id"] not in prev:
                prev.append(cur["id"])
            old_hex = cur["id"].split(":", 1)[-1]
            print(f"xrunner: {ref} now points to {image.id[:12]}; installs made on {old_hex[:12]} "
                  f"stay in {env_dir.name}/{old_hex}", file=err or sys.stderr)
            new["previous_ids"] = prev
            new["first_used"] = now
        elif cur:
            new["previous_ids"] = list(cur.get("previous_ids", []))
            new["first_used"] = cur.get("first_used", now)
        else:
            new["previous_ids"] = []
            new["first_used"] = now
        new["last_used"] = now
        doc["images"][ref] = new

    update(env_dir, fn)


def _layer_info(layer: Path) -> dict:
    try:
        return json.loads((layer / ENV_INFO_NAME).read_text())
    except (OSError, ValueError):
        return {}


def record_layer(env_dir: Path, image_id: str, store) -> None:
    env_dir = Path(env_dir)
    layer = env_dir / image_id
    info = _layer_info(layer)
    image = store.get(image_id)
    if image is None:
        log.warning("image %s of env layer %s is not in the image store; skipping its packages",
                    image_id[:12], layer)
        return
    base = store.package_inventory(image)
    if (layer / "upper").is_dir():
        merged, engine = ns_layer_packages(layer / "upper", base), "ns"
    elif (layer / "rootfs").is_dir():
        merged, engine = scan_tree(layer / "rootfs"), "proot"
    else:
        return
    entry = {"ref": info.get("image_ref"), "engine": info.get("engine") or engine,
             "packages": changes(base, merged)}
    update(env_dir, lambda doc: doc["layers"].__setitem__(image_id, entry))


def _explicit_entry(prefix: Path, project: Path) -> dict | None:
    f = prefix / "conda-explicit.txt"
    try:
        data = f.read_bytes()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"explicit": os.path.relpath(f, project), "sha256": hashlib.sha256(data).hexdigest(),
            "packages": sum(1 for line in lines if line.startswith(("http://", "https://", "file://")))}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def record_conda(env_dir: Path) -> None:
    env_dir = Path(env_dir)
    project = env_dir.parent
    root = env_dir / "conda"
    entries: dict[str, dict] = {}
    base = _explicit_entry(root, project)
    if base:
        entries["name:base"] = base
    envs = root / "envs"
    if envs.is_dir():
        for d in sorted(envs.iterdir()):
            e = _explicit_entry(d, project) if d.is_dir() else None
            if e:
                entries[f"name:{d.name}"] = e
    registry = root / ".home" / ".conda" / "environments.txt"
    try:
        listed = [Path(line.strip()) for line in registry.read_text().splitlines() if line.strip()]
    except OSError:
        listed = []
    for prefix in listed:
        if not _inside(prefix, project) or _inside(prefix, root):
            continue
        e = _explicit_entry(prefix, project)
        if e:
            entries[f"path:{os.path.relpath(prefix, project)}"] = e
    update(env_dir, lambda doc: doc.__setitem__("conda", entries))


def record_all(env_dir: Path, store) -> Path:
    env_dir = Path(env_dir)
    layers = sorted(d for d in env_dir.iterdir() if d.is_dir() and _HEX64.match(d.name)) if env_dir.is_dir() else []
    by_ref: dict[str, list[tuple[str, str]]] = {}
    for d in layers:
        info = _layer_info(d)
        if info.get("image_ref"):
            by_ref.setdefault(info["image_ref"], []).append((info.get("first_used", ""), d.name))
    for ref, items in sorted(by_ref.items()):
        items.sort()
        current = store.get(items[-1][1])
        if current is None:
            continue
        older = ["sha256:" + hexid for _, hexid in items[:-1]]
        entry = image_entry(store, current)

        def fn(doc: dict, ref=ref, entry=entry, older=older, first=items[-1][0]) -> None:
            cur = doc["images"].get(ref, {})
            prev = list(cur.get("previous_ids", []))
            for old in older + ([cur["id"]] if cur.get("id") and cur["id"] != entry["id"] else []):
                if old not in prev and old != entry["id"]:
                    prev.append(old)
            entry["previous_ids"] = prev
            entry["first_used"] = cur.get("first_used") if cur.get("id") == entry["id"] else (first or _now())
            entry["last_used"] = cur.get("last_used") or entry["first_used"]
            doc["images"][ref] = entry

        update(env_dir, fn)
    for d in layers:
        record_layer(env_dir, d.name, store)
    record_conda(env_dir)
    return env_dir / RECORD_NAME


def show(env_dir: Path) -> str:
    doc = load(env_dir)
    out = [f"Environment record: {Path(env_dir) / RECORD_NAME}", "", "Images:"]
    for ref, e in sorted(doc["images"].items()):
        digest = e.get("repo_digests", [None])[0] if e.get("repo_digests") else "no registry digest"
        out.append(f"  {ref}  {e.get('id', '?')[7:19]}  {e.get('source', '?')}  {digest}")
        if e.get("previous_ids"):
            out.append(f"    earlier: {', '.join(i[7:19] for i in e['previous_ids'])}")
    out += ["", "Container layers:"]
    for hexid, e in sorted(doc["layers"].items()):
        out.append(f"  {hexid[:12]}  {e.get('ref')}  ({e.get('engine')})")
        pkgs = e.get("packages", [])
        if not pkgs:
            out.append("    no package changes")
        for p in pkgs:
            out.append(f"    {p['manager']:<6} {p['name']}  {p['change']} {p['version']}")
    out += ["", "Conda environments:"]
    for key, e in sorted(doc["conda"].items()):
        out.append(f"  {key}  {e.get('packages')} packages  {e.get('explicit')}")
    return "\n".join(out) + "\n"
```

- [ ] **Step 4: Run the tests, the full suite, ruff; commit**

```bash
git add src/xcodon_runtime/envrecord.py tests/test_envrecord.py
git commit -m "feat: the per-project environment record: images, layer packages, conda envs"
```

---

### Task 4: Wiring, CLI and docs

**Files:**
- Modify: `src/xcodon_runtime/condaroot.py` (`find_env_folder`), `src/xcodon_runtime/api.py` (`create`, `stop`), `src/xcodon_runtime/condashim.py` (conda hook), `src/xcodon_runtime/cli.py` (`env record|show`), `README.md`
- Test: `tests/test_envrecord_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 1 through 3.
- Produces:
  - `condaroot.find_env_folder(cwd: Path, environ: Mapping[str, str]) -> Path | None`. It returns `XRUNNER_ENV_DIR`, else the nearest trusted `.xrunner-env` found as in `resolve_root`. `resolve_root` reuses it.
  - `Runtime._record(fn: Callable[[], None]) -> None`. It runs `fn` and turns any exception into a logged warning.
  - CLI `xrunner env record [--env-dir DIR]` and `xrunner env show [--env-dir DIR] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_envrecord_wiring.py
import io
import json
from pathlib import Path

import pytest

from tests.conftest import pack_rootfs_as_image
from tests.fake_micromamba import make_fake_micromamba
from xcodon_runtime import cli, envrecord
from xcodon_runtime.api import Runtime
from xcodon_runtime.condashim import conda_main

SP = "usr/lib/python3/site-packages"


@pytest.fixture
def pyimage(home, busybox_rootfs):
    meta = busybox_rootfs / SP / "old-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Name: old\nVersion: 1.0\n")
    return pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/py:latest")


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".xrunner-env").mkdir(parents=True)
    (proj / ".xrunner-env").chmod(0o755)
    return proj


def test_container_stop_records_real_layer_changes(home, pyimage, engine_name, project):
    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    c = rt.create("xcodon-test/py:latest", env_dir=env_dir)
    try:
        rt.start(c)
        script = (f"mkdir -p /{SP}/new-2.0.dist-info && echo 'Name: new' > /{SP}/new-2.0.dist-info/METADATA && "
                  f"echo 'Version: 2.0' >> /{SP}/new-2.0.dist-info/METADATA && rm -rf /{SP}/old-1.0.dist-info")
        assert rt.exec(c, ["/bin/sh", "-c", script]).code == 0
        rt.stop(c)
    finally:
        rt.remove(c, force=True)
    doc = envrecord.load(env_dir)
    assert doc["images"]["xcodon-test/py:latest"]["id"] == "sha256:" + pyimage.id
    pkgs = {(p["name"], p["change"]) for p in doc["layers"][pyimage.id]["packages"]}
    assert pkgs == {("new", "added"), ("old", "removed")}


def test_run_rm_records_too(home, pyimage, engine_name, project):
    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    script = (f"mkdir -p /{SP}/x-1.dist-info && echo 'Name: x' > /{SP}/x-1.dist-info/METADATA && "
              f"echo 'Version: 1' >> /{SP}/x-1.dist-info/METADATA")  # the busybox fixture has no printf applet
    assert rt.run("xcodon-test/py:latest", command=["/bin/sh", "-c", script], rm=True, env_dir=env_dir) == 0
    assert ("x", "added") in {(p["name"], p["change"]) for p in envrecord.load(env_dir)["layers"][pyimage.id]["packages"]}


def test_record_failures_never_break_a_run(home, pyimage, engine_name, project, monkeypatch, caplog):
    env_dir = project / ".xrunner-env"
    monkeypatch.setattr(envrecord, "record_layer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rt = Runtime(home.path, engine=engine_name)
    assert rt.run("xcodon-test/py:latest", command=["/bin/true"], rm=True, env_dir=env_dir) == 0
    assert "could not update the environment record" in caplog.text


def test_conda_changes_update_the_record(home, project, tmp_path):
    mm = make_fake_micromamba(tmp_path / "fakebin")
    environ = {"PATH": "/usr/bin:/bin", "XRUNNER_MICROMAMBA": str(mm), "HOME": str(tmp_path / "u")}
    assert conda_main(["create", "-n", "tools", "x"], home, cwd=project, environ=environ, err=io.StringIO()) == 0
    doc = envrecord.load(project / ".xrunner-env")
    assert doc["conda"]["name:tools"]["explicit"] == ".xrunner-env/conda/envs/tools/conda-explicit.txt"
    assert doc["conda"]["name:tools"]["packages"] == 1


def test_cli_env_record_and_show(home, pyimage, project, monkeypatch, capfd):
    env_dir = project / ".xrunner-env"
    (env_dir / pyimage.id / "upper").mkdir(parents=True)
    (env_dir / pyimage.id / "image.json").write_text(json.dumps(
        {"image_ref": "xcodon-test/py:latest", "image_id": pyimage.id, "engine": "ns", "first_used": "t"}))
    monkeypatch.chdir(project / ".xrunner-env")
    assert cli.main(["env", "record"]) == 0
    assert capfd.readouterr().out.strip() == str(env_dir / "environment.json")
    assert cli.main(["env", "show"]) == 0
    assert "xcodon-test/py:latest" in capfd.readouterr().out
    assert cli.main(["env", "show", "--json", "--env-dir", str(env_dir)]) == 0
    assert json.loads(capfd.readouterr().out)["version"] == 1
    monkeypatch.chdir(Path("/"))
    monkeypatch.delenv("XRUNNER_ENV_DIR", raising=False)
    assert cli.main(["env", "show"]) == 125
    assert "--env-dir" in capfd.readouterr().err
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_envrecord_wiring.py`
Expected: failures, because the record is never written and `env` is not a CLI command.

- [ ] **Step 3: condaroot.find_env_folder**

Move the `XRUNNER_ENV_DIR` branch and the upward trusted search out of `resolve_root` into:

```python
def find_env_folder(cwd: Path, environ: Mapping[str, str]) -> Path | None:
    """XRUNNER_ENV_DIR, else the nearest trusted .xrunner-env above cwd, else None."""
```

`resolve_root` then becomes: the flag first, then `find_env_folder(...)`, which gives `Root(folder / ROOT_DIRNAME, "env" or "project")`, then the home root. Keep the existing sources and behavior exactly. The existing tests in tests/test_condashim.py must pass unchanged.

- [ ] **Step 4: Runtime hooks**

In `api.py`:

```python
    def _record(self, fn) -> None:
        """Update the project's environment record; a failure only warns (spec 13.4)."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - recording must never break a run
            log.warning("could not update the environment record: %s", e)
```

- In `create`, after the env dir `mkdir`, when `env_dir_s` is set: `self._record(lambda: envrecord.note_image(Path(env_dir_s), ref, image, self.images))`.
- In `stop`, after `c.save()`, when `c.env_dir` is set: `self._record(lambda: envrecord.record_layer(Path(c.env_dir), c.image_id, self.images))`.

Import `from xcodon_runtime import envrecord` at module level, so tests can monkeypatch `envrecord.record_layer`. Check that api.py has a module `log`.

- [ ] **Step 5: Conda hook**

In `condashim.conda_main`, after the micromamba call, when `code == 0` and the key is in RECORD, is `env remove`, or is a `remove`/`uninstall` with `--all`, and `root.source` is `"env"` or `"project"`, run:

```python
        try:
            from xcodon_runtime.envrecord import record_conda

            record_conda(root.path.parent)
        except Exception as e:  # noqa: BLE001 - recording must never change the exit code
            print(f"xrunner: warning: could not update the environment record: {e}", file=err)
```

Do this after the `conda-explicit.txt` write or deletion, so the record sees the new file.

- [ ] **Step 6: CLI**

```python
def _env_folder(args) -> Path:
    from xcodon_runtime.condaroot import find_env_folder

    if args.env_dir:
        return Path(args.env_dir).expanduser().resolve()
    found = find_env_folder(Path.cwd(), os.environ)
    if found is None:
        raise UsageError("no env folder found here; pass --env-dir DIR or set XRUNNER_ENV_DIR")
    return found


def cmd_env(rt: Runtime, args) -> int:
    from xcodon_runtime import envrecord

    env_dir = _env_folder(args)
    if args.env_cmd == "record":
        print(envrecord.record_all(env_dir, rt.images))
    elif args.json:
        print(json.dumps(envrecord.load(env_dir), indent=2, sort_keys=True))
    else:
        print(envrecord.show(env_dir), end="")
    return 0
```

Parser:

```python
    s = sub.add_parser("env", help="the project's environment record (images and package lists)")
    esub = s.add_subparsers(dest="env_cmd", required=True)
    i = esub.add_parser("record", help="rebuild .xrunner-env/environment.json from what is on disk")
    i.add_argument("--env-dir")
    i.set_defaults(func=cmd_env, json=False)
    i = esub.add_parser("show", help="print the environment record")
    i.add_argument("--env-dir")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_env)
```

In `test_cli_env_record_and_show`, the working directory is the `.xrunner-env` folder itself. The upward search reaches its parent, which holds `.xrunner-env`, so `find_env_folder` finds it.

- [ ] **Step 7: README**

Add an "Environment record" section after "Tools without conda". It says four things:
- xrunner keeps `.xrunner-env/environment.json`, with the images a project used (id, source, registry digests, Dockerfiles) and the packages each install added, changed or removed.
- The record updates automatically after conda changes and when containers stop.
- `xrunner env show` prints it, and `xrunner env record` rebuilds it.
- It is a record, not a restore mechanism.

- [ ] **Step 8: Run the tests, the full suite, ruff; commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_envrecord_wiring.py tests/test_condashim.py tests/test_env_dir.py tests/test_containers_api.py`, then the full suite, ruff, and `pgrep -af 'xcodon_runtime[.]keeper'` (it must print nothing).

```bash
git add src/xcodon_runtime/condaroot.py src/xcodon_runtime/api.py src/xcodon_runtime/condashim.py src/xcodon_runtime/cli.py README.md tests/test_envrecord_wiring.py
git commit -m "feat: containers and conda keep the project's environment record; xrunner env record|show"
```

---

## Plan Self-Review

**Spec coverage:**

| Spec section | Where the plan covers it |
|---|---|
| 13.2 record file | Task 3 (format, sections, relative paths) |
| 13.3 reading packages | Task 1 (managers, cache rule, R rule, dpkg compare, change kinds, skip unreadable) |
| 13.4 triggers | Task 4 (create, stop, run --rm, conda, `env record`) and Task 3 (lock, atomic write) |
| 13.4 failures only warn | Task 4 `_record` and the conda hook |
| 13.5 store metadata | Task 2 |
| 13.6 tag move | Task 3 `note_image` |
| 13.7 interfaces | Tasks 3 and 4 |
| 13.8 testing | Tasks 1 through 4 |

**Consistency check.** Names are used the same way across tasks:
- **Task 1:** `Pkg`, `scan_tree`, `ns_layer_packages`, `changes`, `pkg_to_json`, `pkg_from_json`.
- **Task 2:** `ImageStore.manifest`, `annotate`, `package_inventory`, `FetchedImage.repo_digests`, `DaemonSource.repo_digests`.
- **Task 3:** `envrecord.load`, `update`, `note_image`, `record_layer`, `record_conda`, `record_all`, `show`.
- **Task 4:** `condaroot.find_env_folder`.

**Known judgment call.** The spec's 13.3 wording for R treats `DESCRIPTION` plus `Meta/package.rds` as the package marker. `Repository:` is optional, and base R's own packages list `Repository` only when they came from CRAN.
