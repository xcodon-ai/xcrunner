# Conda Shim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an unchanged agent's `conda create/install/run` calls work on a host with no conda. xrunner answers them with a pinned micromamba and project-local environments.

**Architecture:** `micromamba.py` pins, downloads, verifies and locates the micromamba binary. `condaroot.py` decides which root prefix a call uses. `condashim.py` turns a conda command line into a micromamba one with isolated settings and writes the rerun record. `condarun.py` implements `conda run` itself, because micromamba 2.9.0's `run` is broken on this host. `shim.py` holds the shared shim writer used by `xrunner shim install docker|conda`. The CLI gains an `xrunner conda ...` verb that passes its arguments through untouched.

**Tech Stack:** Python 3.10+, stdlib only (`urllib.request`, `tarfile`, `hashlib`, `subprocess`, `os.execvpe`). pytest. The existing fixture `home` (a fresh `RuntimeHome` that also sets `XCODON_RUNTIME_HOME`).

**Spec:** `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`, section 12.

## Global Constraints

- Python `>=3.10`, stdlib only. Plain English docstrings and messages. Every error derives from `XcodonError`; the CLI maps an uncaught `XcodonError` to exit 125.
- Pinned micromamba: version `2.9.0`; URL `https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.9.0-0.tar.bz2`; archive SHA-256 `8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd`; `bin/micromamba` SHA-256 `366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3`. Stored at `<home>/bin/micromamba-2.9.0`. linux-64 only.
- `XRUNNER_MICROMAMBA` overrides the binary at call time. `xrunner conda` never downloads; a missing binary raises with `micromamba is not installed; run: xrunner shim install conda`.
- Root prefix order: `-r/--root-prefix` flag, then `<XRUNNER_ENV_DIR>/conda`, then `<nearest .xrunner-env>/conda` searching upward from the working directory, then `<home>/conda`. The home fallback prints one stderr line: `xrunner: no project env folder found; using <root>`.
- Every micromamba process gets `HOME=<root>/.home`, `XDG_CACHE_HOME=<root>/.home/.cache`, `XDG_CONFIG_HOME=<root>/.home/.config`, `MAMBA_ROOT_PREFIX=<root>`, `CONDA_PKGS_DIRS=<home>/conda-pkgs`, `--no-rc`, and `-r <root>`, except `clean`, which micromamba 2.9.0 rejects `-r` for and which gets the root from `MAMBA_ROOT_PREFIX` alone. The user's real `~/.conda` is never read or written.
- Default channels `conda-forge`, `bioconda`, appended after the command's own `-c` channels without duplicates, unless `--override-channels`. `-y` is added for `create`, `install`, `update`, `remove`, `uninstall`, `clean`, `env create`, `env remove`.
- `conda run` is implemented by xrunner. It sets `PATH=<prefix>/bin:$PATH`, `CONDA_PREFIX`, `CONDA_DEFAULT_ENV`, `CONDA_SHLVL=1`, keeps the real HOME, sources `<prefix>/etc/conda/activate.d/*.sh` when present, and replaces itself with the command. Missing env: exit 1 with `EnvironmentLocationNotFound: Not a conda environment: <path>`. Missing command: exit 127.
- `activate`, `deactivate`, `init`, `shell` exit 1 with a message pointing to `conda run -n NAME CMD`. Unsupported verbs exit 2. `--version` prints `conda <micromamba version> (micromamba via xrunner)`.
- Rerun record: `<prefix>/conda-explicit.txt` from `micromamba env export --explicit` after a successful `create`, `install`, `update`, `remove`, `uninstall`, `env create`. A failed export only warns.
- Shims: `conda`, `mamba`, `micromamba`, each `#!/bin/sh`, marker line `# conda shim installed by xrunner`, body `exec <absolute xrunner> conda "$@"`. Refuse, unless `--force`, when a real one is on PATH or DIR holds a non-shim under one of the names. Write via temp file plus `os.replace`.
- Existing behavior unchanged, including `xrunner shim install` with no kind (docker). Full suite green; `.venv/bin/ruff check src tests` clean. Branch `feat/conda-shim`.
- Test commands run as `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q ...`. Use `/usr/bin/grep` (plain `grep` is aliased to ugrep on this host).

## File Structure

```
src/xcodon_runtime/micromamba.py     pin, install (download or copy), find            (new, Task 1)
src/xcodon_runtime/condaroot.py      Root, resolve_root, lookup_root, ensure_root      (new, Task 2)
src/xcodon_runtime/condashim.py      parse_args, micromamba_argv, conda_main           (new, Task 2)
src/xcodon_runtime/condarun.py       parse_run_args, run_main                          (new, Task 3)
src/xcodon_runtime/shim.py           shared shim writer and checks                     (new, Task 4)
src/xcodon_runtime/cli.py            `conda` verb (Task 2); `shim install [docker|conda]` (Task 4)
tests/fake_micromamba.py             fake micromamba script for tests                  (new, Task 2)
tests/test_micromamba.py, tests/test_condashim.py, tests/test_condarun.py,
tests/test_shim_conda.py, tests/test_conda_e2e.py                                      (new)
README.md, spec section 12 wording (Task 4)
```

---

### Task 1: The micromamba binary

**Files:**
- Create: `src/xcodon_runtime/micromamba.py`
- Test: `tests/test_micromamba.py`

**Interfaces:**
- Produces: constants `MICROMAMBA_VERSION`, `MICROMAMBA_URL`, `MICROMAMBA_ARCHIVE_SHA256`, `MICROMAMBA_BINARY_SHA256`, `MICROMAMBA_ENV = "XRUNNER_MICROMAMBA"`; `class MicromambaMissing(XcodonError)`; `pinned_path(home: RuntimeHome) -> Path`; `find_micromamba(home: RuntimeHome, environ: Mapping[str, str] | None = None) -> Path`; `install_micromamba(home: RuntimeHome, source: Path | None = None, opener=urllib.request.urlopen) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_micromamba.py
import hashlib
import io
import os
import tarfile

import pytest

from xcodon_runtime import micromamba as mm
from xcodon_runtime.errors import XcodonError

FAKE_BINARY = b"#!/bin/sh\necho 2.9.0\n"


def _archive(binary: bytes = FAKE_BINARY) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        info = tarfile.TarInfo("bin/micromamba")
        info.size = len(binary)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(binary))
        other = tarfile.TarInfo("info/index.json")
        other.size = 2
        tar.addfile(other, io.BytesIO(b"{}"))
    return buf.getvalue()


@pytest.fixture
def pinned_fake(monkeypatch):
    data = _archive()
    monkeypatch.setattr(mm, "MICROMAMBA_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(mm, "MICROMAMBA_BINARY_SHA256", hashlib.sha256(FAKE_BINARY).hexdigest())
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        return io.BytesIO(data)

    return opener, calls


def test_pinned_values_are_the_conda_forge_2_9_0_package():
    assert mm.MICROMAMBA_VERSION == "2.9.0"
    assert mm.MICROMAMBA_URL == "https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.9.0-0.tar.bz2"
    assert mm.MICROMAMBA_ARCHIVE_SHA256 == "8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd"
    assert mm.MICROMAMBA_BINARY_SHA256 == "790cbf43cb101027c6b7d483903fa155c69bd2ddf8ae03a11a796675a1008575"


def test_install_downloads_verifies_and_extracts(home, pinned_fake):
    opener, calls = pinned_fake
    path = mm.install_micromamba(home, opener=opener)
    assert path == home.path / "bin" / "micromamba-2.9.0" == mm.pinned_path(home)
    assert path.read_bytes() == FAKE_BINARY
    assert os.access(path, os.X_OK)
    assert calls == [mm.MICROMAMBA_URL]
    assert mm.install_micromamba(home, opener=opener) == path
    assert calls == [mm.MICROMAMBA_URL], "a verified binary is not downloaded again"
    assert [p.name for p in path.parent.iterdir()] == ["micromamba-2.9.0"], "no temp files left"


def test_install_rejects_a_wrong_archive_checksum(home, pinned_fake, monkeypatch):
    opener, _ = pinned_fake
    monkeypatch.setattr(mm, "MICROMAMBA_ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(XcodonError, match="checksum"):
        mm.install_micromamba(home, opener=opener)
    assert not mm.pinned_path(home).exists()


def test_install_rejects_a_wrong_binary_checksum(home, pinned_fake, monkeypatch):
    opener, _ = pinned_fake
    monkeypatch.setattr(mm, "MICROMAMBA_BINARY_SHA256", "0" * 64)
    with pytest.raises(XcodonError, match="checksum"):
        mm.install_micromamba(home, opener=opener)
    assert not mm.pinned_path(home).exists()


def test_install_reports_download_errors(home):
    def broken(url, timeout=None):
        raise OSError("network down")

    with pytest.raises(XcodonError, match="--micromamba"):
        mm.install_micromamba(home, opener=broken)


def test_install_copies_a_given_binary(home, tmp_path):
    src = tmp_path / "my-micromamba"
    src.write_bytes(b"#!/bin/sh\necho mine\n")
    path = mm.install_micromamba(home, source=src)
    assert path.read_bytes() == src.read_bytes() and os.access(path, os.X_OK)
    with pytest.raises(XcodonError, match="not a file"):
        mm.install_micromamba(home, source=tmp_path / "missing")


def test_install_refuses_other_platforms(home, monkeypatch, pinned_fake):
    monkeypatch.setattr(mm.platform, "machine", lambda: "aarch64")
    with pytest.raises(XcodonError, match="linux-64"):
        mm.install_micromamba(home, opener=pinned_fake[0])


def test_find_prefers_the_environment_variable(home, tmp_path):
    exe = tmp_path / "mm"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert mm.find_micromamba(home, {"XRUNNER_MICROMAMBA": str(exe)}) == exe
    with pytest.raises(mm.MicromambaMissing, match="XRUNNER_MICROMAMBA"):
        mm.find_micromamba(home, {"XRUNNER_MICROMAMBA": str(tmp_path / "nope")})


def test_find_uses_the_pinned_binary_or_explains(home, pinned_fake):
    with pytest.raises(mm.MicromambaMissing, match="run: xrunner shim install conda"):
        mm.find_micromamba(home, {})
    path = mm.install_micromamba(home, opener=pinned_fake[0])
    assert mm.find_micromamba(home, {}) == path
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_micromamba.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.micromamba'`

- [ ] **Step 3: Write micromamba.py**

```python
# src/xcodon_runtime/micromamba.py
"""The pinned micromamba binary behind `xrunner conda`. See spec section 12.7.

The binary comes from conda-forge's own micromamba package, the channel the
conda shim needs anyway. The archive and the extracted binary are both checked
against pinned SHA-256 values before the binary is used.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Mapping

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import RuntimeHome

MICROMAMBA_VERSION = "2.9.0"
MICROMAMBA_URL = "https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.9.0-0.tar.bz2"
MICROMAMBA_ARCHIVE_SHA256 = "8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd"
MICROMAMBA_BINARY_SHA256 = "790cbf43cb101027c6b7d483903fa155c69bd2ddf8ae03a11a796675a1008575"
MICROMAMBA_ENV = "XRUNNER_MICROMAMBA"
_MEMBER = "bin/micromamba"
_CHUNK = 1 << 20


class MicromambaMissing(XcodonError):
    """No usable micromamba binary."""


def pinned_path(home: RuntimeHome) -> Path:
    return home.path / "bin" / f"micromamba-{MICROMAMBA_VERSION}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_platform() -> None:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64", "AMD64"):
        raise XcodonError(
            f"the conda shim supports linux-64 only; this host is {platform.system()} {platform.machine()}")


def find_micromamba(home: RuntimeHome, environ: Mapping[str, str] | None = None) -> Path:
    """The micromamba to run: $XRUNNER_MICROMAMBA, else the pinned binary under the home."""
    environ = os.environ if environ is None else environ
    override = environ.get(MICROMAMBA_ENV)
    if override:
        p = Path(override)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        raise MicromambaMissing(f"{MICROMAMBA_ENV}={override} is not an executable file")
    p = pinned_path(home)
    if p.is_file() and os.access(p, os.X_OK):
        return p
    raise MicromambaMissing("micromamba is not installed; run: xrunner shim install conda")


def install_micromamba(home: RuntimeHome, source: Path | None = None,
                       opener=urllib.request.urlopen) -> Path:
    """Put micromamba at pinned_path(home): copy ``source``, or download and verify the pin."""
    _check_platform()
    dest = pinned_path(home)
    if source is not None and not Path(source).is_file():
        raise XcodonError(f"--micromamba {source} is not a file")
    if source is None and dest.is_file() and _sha256(dest) == MICROMAMBA_BINARY_SHA256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with home.lock("micromamba"):
        work = Path(tempfile.mkdtemp(prefix=".micromamba-", dir=dest.parent))
        try:
            binary = work / "micromamba"
            if source is not None:
                shutil.copyfile(source, binary)
            else:
                _download_binary(work, binary, opener)
            binary.chmod(0o755)
            os.replace(binary, dest)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return dest


def _download_binary(work: Path, binary: Path, opener) -> None:
    archive = work / "micromamba.tar.bz2"
    try:
        with opener(MICROMAMBA_URL, timeout=120) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out, _CHUNK)
    except OSError as e:
        raise XcodonError(
            f"could not download micromamba from {MICROMAMBA_URL}: {e}; "
            f"on a host without access, pass --micromamba PATH") from e
    got = _sha256(archive)
    if got != MICROMAMBA_ARCHIVE_SHA256:
        raise XcodonError(f"micromamba archive checksum mismatch: got {got}, want {MICROMAMBA_ARCHIVE_SHA256}")
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            member = tar.getmember(_MEMBER)
            src = tar.extractfile(member)
            if src is None:
                raise XcodonError(f"{_MEMBER} in the micromamba archive is not a regular file")
            with src, open(binary, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
    except (tarfile.TarError, KeyError) as e:
        raise XcodonError(f"cannot read {_MEMBER} from the micromamba archive: {e}") from e
    got = _sha256(binary)
    if got != MICROMAMBA_BINARY_SHA256:
        raise XcodonError(f"micromamba binary checksum mismatch: got {got}, want {MICROMAMBA_BINARY_SHA256}")
```

- [ ] **Step 4: Run the tests, then the full suite, then commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_micromamba.py` (expect 9 passed), then `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q` and `.venv/bin/ruff check src tests`.

```bash
git add src/xcodon_runtime/micromamba.py tests/test_micromamba.py
git commit -m "feat: pinned micromamba from conda-forge, downloaded and checksum-verified"
```

---

### Task 2: Root prefix, micromamba pass-through, and the `xrunner conda` verb

**Files:**
- Create: `src/xcodon_runtime/condaroot.py`, `src/xcodon_runtime/condashim.py`, `tests/fake_micromamba.py`
- Modify: `src/xcodon_runtime/cli.py` (`_split_argv`, a `cmd_conda` function, the `conda` subparser)
- Test: `tests/test_condashim.py`

**Interfaces:**
- Consumes: `micromamba.find_micromamba(home, environ) -> Path`, `micromamba.MicromambaMissing`.
- Produces (condaroot.py): constants `ENV_DIR_VAR = "XRUNNER_ENV_DIR"`, `ENV_FOLDER_NAME = ".xrunner-env"`, `ROOT_DIRNAME = "conda"`; `@dataclass(frozen=True) class Root: path: Path; source: str` where source is one of `"flag"`, `"env"`, `"project"`, `"home"`; `resolve_root(cwd: Path, environ: Mapping[str, str], home: RuntimeHome, explicit: str | None = None) -> Root`; `lookup_root(root: Root, home: RuntimeHome, name: str) -> Root`; `ensure_root(root: Path) -> None`; `opt_value(tokens: list[str], i: int) -> tuple[str | None, int]`.
- Produces (condashim.py): `PKGS_DIRNAME = "conda-pkgs"`, `PRIVATE_HOME = ".home"`, `EXPLICIT_NAME = "conda-explicit.txt"`, `DEFAULT_CHANNELS = ("conda-forge", "bioconda")`; `@dataclass class Parsed`; `parse_args(argv) -> Parsed`; `micromamba_env(root: Path, home: RuntimeHome, environ) -> dict[str, str]`; `micromamba_argv(mm: Path, p: Parsed, root: Path) -> list[str]`; `target_prefix(p: Parsed, root: Path, cwd: Path) -> Path`; `conda_main(argv: Sequence[str], home: RuntimeHome, cwd: Path | None = None, environ: Mapping[str, str] | None = None, err: TextIO | None = None) -> int`.
- Produces (tests/fake_micromamba.py): `make_fake_micromamba(bin_dir: Path) -> Path`; `read_log(log: Path) -> list[dict]`.
- Produces (cli.py): `xrunner conda ARGS...` passes ARGS untouched to `conda_main(ARGS, rt.home)`.

- [ ] **Step 1: Write the fake micromamba helper**

```python
# tests/fake_micromamba.py
"""A stand-in for micromamba: records each call and fakes just enough behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPT = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_MM_LOG")
keys = ["HOME", "MAMBA_ROOT_PREFIX", "CONDA_PKGS_DIRS", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "CONDARC", "CONDA_PREFIX"]
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                            "env": {k: os.environ.get(k) for k in keys}}) + "\n")
if args == ["--version"]:
    print("2.9.0")
    sys.exit(0)
if args[:2] == ["env", "export"]:
    code = int(os.environ.get("FAKE_MM_EXPORT_EXIT", "0"))
    if code == 0:
        print("@EXPLICIT")
        print("https://conda.anaconda.org/bioconda/linux-64/seqtk-1.5-h577a1d6_1.tar.bz2#0bc157aea007a7895e6f2e8f44a0b407")
    sys.exit(code)
code = int(os.environ.get("FAKE_MM_EXIT", "0"))
if code == 0 and args and args[0] in ("create", "install"):
    root = os.environ["MAMBA_ROOT_PREFIX"]
    prefix = root
    for i, a in enumerate(args):
        if a in ("-p", "--prefix"):
            prefix = os.path.join(os.getcwd(), args[i + 1])
        elif a in ("-n", "--name") and args[i + 1] != "base":
            prefix = os.path.join(root, "envs", args[i + 1])
    os.makedirs(os.path.join(prefix, "conda-meta"), exist_ok=True)
    os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
sys.exit(code)
'''


def make_fake_micromamba(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "micromamba"
    path.write_text(f"#!{sys.executable}\n{_SCRIPT}")
    path.chmod(0o755)
    return path


def read_log(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_condashim.py
import io
from pathlib import Path

import pytest

from tests.fake_micromamba import make_fake_micromamba, read_log
from xcodon_runtime import cli
from xcodon_runtime.condaroot import lookup_root, resolve_root
from xcodon_runtime.condashim import conda_main, parse_args
from xcodon_runtime.micromamba import MicromambaMissing


@pytest.fixture
def fake(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin"), tmp_path / "mm.log"


def _env(fake, **extra):
    mm, log = fake
    return {"PATH": "/usr/bin:/bin", "HOME": str(Path("/nonexistent-user-home")),
            "XRUNNER_MICROMAMBA": str(mm), "FAKE_MM_LOG": str(log), **extra}


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".xrunner-env").mkdir(parents=True)
    (proj / "workspace").mkdir()
    return proj


def test_resolve_root_order(tmp_path, home, project):
    sub = project / "workspace"
    assert resolve_root(sub, {"XRUNNER_ENV_DIR": str(tmp_path / "e")}, home).path == tmp_path / "e" / "conda"
    r = resolve_root(sub, {}, home)
    assert (r.path, r.source) == (project / ".xrunner-env" / "conda", "project")
    r = resolve_root(tmp_path / "elsewhere", {}, home)
    assert (r.path, r.source) == (home.path / "conda", "home")
    r = resolve_root(sub, {"XRUNNER_ENV_DIR": "/e"}, home, explicit="r2")
    assert (r.path, r.source) == (sub / "r2", "flag")


def test_lookup_root_falls_back_to_the_home_root(home, project):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    root = resolve_root(project, {}, home)
    found = lookup_root(root, home, "old")
    assert (found.path, found.source) == (home.path / "conda", "lookup")
    assert lookup_root(root, home, "new").path == root.path


def test_parse_args():
    p = parse_args(["--json", "create", "-n", "a", "-c", "bioconda", "--channel=conda-forge", "bwa", "-y"])
    assert (p.verb, p.name, p.channels, p.yes) == ("create", "a", ["bioconda", "conda-forge"], True)
    assert p.tokens == ["--json", "-n", "a", "-c", "bioconda", "--channel=conda-forge", "bwa", "-y"]
    p = parse_args(["env", "export", "-p", "x", "-r", "/root2", "--explicit"])
    assert (p.verb, p.sub, p.prefix, p.root_flag, p.key) == ("env", "export", "x", "/root2", "env export")
    assert "-r" not in p.tokens and "/root2" not in p.tokens
    assert parse_args(["--version"]).version and parse_args(["-h"]).help


def test_create_runs_micromamba_with_isolated_settings(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["create", "-n", "bwa_env", "-c", "bioconda", "bwa"], home, cwd=project,
                      environ=_env(fake), err=err) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"] == ["create", "-n", "bwa_env", "-c", "bioconda", "bwa",
                                "--no-rc", "-r", str(root), "-y", "-c", "conda-forge"]
    assert calls[0]["cwd"] == str(project)
    assert calls[0]["env"] == {
        "HOME": str(root / ".home"), "MAMBA_ROOT_PREFIX": str(root),
        "CONDA_PKGS_DIRS": str(home.path / "conda-pkgs"),
        "XDG_CACHE_HOME": str(root / ".home" / ".cache"),
        "XDG_CONFIG_HOME": str(root / ".home" / ".config"),
        "CONDARC": None, "CONDA_PREFIX": None,
    }
    prefix = root / "envs" / "bwa_env"
    assert calls[1]["argv"] == ["env", "export", "--no-rc", "-r", str(root), "-p", str(prefix), "--explicit"]
    assert (prefix / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert err.getvalue() == ""


def test_user_conda_variables_are_dropped(home, project, fake):
    env = _env(fake, CONDARC="/home/u/.condarc", CONDA_PREFIX="/home/u/miniconda3", MAMBA_ROOT_PREFIX="/x")
    assert conda_main(["list"], home, cwd=project, environ=env, err=io.StringIO()) == 0
    call = read_log(fake[1])[0]
    assert call["env"]["CONDARC"] is None and call["env"]["CONDA_PREFIX"] is None
    assert call["env"]["MAMBA_ROOT_PREFIX"] == str(project / ".xrunner-env" / "conda")


def test_read_only_verbs_get_no_yes_and_no_channels(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["--json", "list", "-n", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["list", "--json", "-n", "x", "--no-rc", "-r", str(root)]


def test_yes_and_channels_are_not_duplicated(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    argv = ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools",
                                            "--no-rc", "-r", str(root)]


def test_override_channels_adds_no_defaults(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    argv = ["create", "-p", "workspace/env", "--override-channels", "-c", "bioconda", "bwa"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == argv + ["--no-rc", "-r", str(root), "-y"]
    assert (project / "workspace" / "env" / "conda-explicit.txt").exists()


def test_env_subcommands_and_clean_confirmation(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["env", "remove", "-n", "a"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert conda_main(["clean", "-a"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"] == ["env", "remove", "-n", "a", "--no-rc", "-r", str(root), "-y"]
    assert calls[1]["argv"] == ["clean", "-a", "--no-rc", "-y"], "micromamba's clean rejects -r"
    assert calls[1]["env"]["MAMBA_ROOT_PREFIX"] == str(root)


def test_root_flag_replaces_the_resolved_root(home, project, fake, tmp_path):
    custom = tmp_path / "custom"
    assert conda_main(["create", "-r", str(custom), "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake), err=io.StringIO()) == 0
    argv = read_log(fake[1])[0]["argv"]
    assert argv.count("-r") == 1 and argv[argv.index("-r") + 1] == str(custom)
    assert (custom / ".home").is_dir()


def test_failed_command_keeps_its_exit_code_and_writes_no_record(home, project, fake):
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project, environ=_env(fake, FAKE_MM_EXIT="3"),
                      err=io.StringIO()) == 3
    assert len(read_log(fake[1])) == 1
    assert not (project / ".xrunner-env" / "conda" / "envs" / "a" / "conda-explicit.txt").exists()


def test_a_failed_export_only_warns(home, project, fake):
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake, FAKE_MM_EXPORT_EXIT="1"), err=err) == 0
    assert "could not record the packages" in err.getvalue()


def test_base_installs_record_at_the_root(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["install", "-n", "base", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert (root / "conda-explicit.txt").exists()


def test_home_fallback_warns_once(home, tmp_path, fake):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=lonely, environ=_env(fake), err=err) == 0
    assert err.getvalue() == f"xrunner: no project env folder found; using {home.path / 'conda'}\n"
    assert (home.path / "conda" / "envs" / "a" / "conda-meta").is_dir()


def test_lookups_find_an_env_in_the_home_root(home, project, fake):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    err = io.StringIO()
    assert conda_main(["list", "-n", "old"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert conda_main(["create", "-n", "old", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"][calls[0]["argv"].index("-r") + 1] == str(home.path / "conda")
    assert "no project env folder" not in err.getvalue()
    assert calls[1]["argv"][calls[1]["argv"].index("-r") + 1] == str(project / ".xrunner-env" / "conda")


def test_refused_and_unsupported_verbs(home, project, fake):
    err = io.StringIO()
    assert conda_main(["activate", "bwa_env"], home, cwd=project, environ=_env(fake), err=err) == 1
    assert "conda run -n NAME" in err.getvalue()
    err = io.StringIO()
    assert conda_main(["build", "recipe/"], home, cwd=project, environ=_env(fake), err=err) == 2
    assert "not supported" in err.getvalue()
    err = io.StringIO()
    assert conda_main(["env", "update", "-f", "x.yml"], home, cwd=project, environ=_env(fake), err=err) == 2
    assert read_log(fake[1]) == []


def test_version_and_usage(home, project, fake, capsys):
    assert conda_main(["--version"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert capsys.readouterr().out == "conda 2.9.0 (micromamba via xrunner)\n"
    assert conda_main(["--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert "conda run -n NAME" in capsys.readouterr().out
    err = io.StringIO()
    assert conda_main([], home, cwd=project, environ=_env(fake), err=err) == 2


def test_missing_micromamba_raises(home, project):
    with pytest.raises(MicromambaMissing, match="run: xrunner shim install conda"):
        conda_main(["list"], home, cwd=project, environ={"PATH": "/usr/bin:/bin"}, err=io.StringIO())


def test_cli_conda_verb_passes_arguments_through(home, fake, monkeypatch, capfd):
    monkeypatch.setenv("XRUNNER_MICROMAMBA", str(fake[0]))
    assert cli.main(["-v", "conda", "--version"]) == 0
    assert capfd.readouterr().out == "conda 2.9.0 (micromamba via xrunner)\n"
    monkeypatch.delenv("XRUNNER_MICROMAMBA")
    assert cli.main(["conda", "list"]) == 125
    assert "xrunner shim install conda" in capfd.readouterr().err
```

- [ ] **Step 3: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_condashim.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.condaroot'`

- [ ] **Step 4: Write condaroot.py**

```python
# src/xcodon_runtime/condaroot.py
"""Which conda root prefix a `xrunner conda` call uses. See spec section 12.3."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import RuntimeHome

ENV_DIR_VAR = "XRUNNER_ENV_DIR"
ENV_FOLDER_NAME = ".xrunner-env"
ROOT_DIRNAME = "conda"


@dataclass(frozen=True)
class Root:
    path: Path
    source: str  # "flag", "env", "project", "home", or "lookup" (an existing env found in the home root)


def opt_value(tokens: list[str], i: int) -> tuple[str | None, int]:
    """The value of the option at tokens[i] (`--opt=value` or `--opt value`) and the next index."""
    tok = tokens[i]
    if tok.startswith("--") and "=" in tok:
        return tok.split("=", 1)[1], i + 1
    if i + 1 < len(tokens):
        return tokens[i + 1], i + 2
    return None, i + 1


def resolve_root(cwd: Path, environ: Mapping[str, str], home: RuntimeHome,
                 explicit: str | None = None) -> Root:
    if explicit:
        p = Path(explicit)
        return Root(p if p.is_absolute() else cwd / p, "flag")
    env_dir = environ.get(ENV_DIR_VAR)
    if env_dir:
        return Root(Path(env_dir) / ROOT_DIRNAME, "env")
    for d in (cwd, *cwd.parents):
        if (d / ENV_FOLDER_NAME).is_dir():
            return Root(d / ENV_FOLDER_NAME / ROOT_DIRNAME, "project")
    return Root(home.path / ROOT_DIRNAME, "home")


def lookup_root(root: Root, home: RuntimeHome, name: str) -> Root:
    """For an existing named env: the resolved root if it has it, else the home root if that has it."""
    if (root.path / "envs" / name).is_dir():
        return root
    alt = home.path / ROOT_DIRNAME
    if alt != root.path and (alt / "envs" / name).is_dir():
        return Root(alt, "lookup")
    return root


def ensure_root(root: Path) -> None:
    try:
        (root / ".home").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise XcodonError(f"cannot create the conda root prefix {root}: {e}") from e
    os.makedirs(root / "envs", exist_ok=True)
```

- [ ] **Step 5: Write condashim.py**

```python
# src/xcodon_runtime/condashim.py
"""`xrunner conda`: conda's command line, answered by a pinned micromamba. See spec section 12."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import ensure_root, lookup_root, opt_value, resolve_root
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.micromamba import find_micromamba

PKGS_DIRNAME = "conda-pkgs"
PRIVATE_HOME = ".home"
EXPLICIT_NAME = "conda-explicit.txt"
DEFAULT_CHANNELS = ("conda-forge", "bioconda")
PASS_VERBS = frozenset({"create", "install", "update", "remove", "uninstall", "list", "search", "info",
                        "clean", "config", "env"})
ENV_SUBVERBS = frozenset({"list", "create", "export", "remove"})
REFUSED_VERBS = frozenset({"activate", "deactivate", "init", "shell"})
CONFIRM = frozenset({"create", "install", "update", "remove", "uninstall", "clean", "env create", "env remove"})
CHANNELS = frozenset({"create", "install", "update", "search", "env create"})
RECORD = frozenset({"create", "install", "update", "remove", "uninstall", "env create"})
LOOKUP = frozenset({"list", "env export", "remove", "uninstall", "install", "update"})
NO_ROOT_FLAG = frozenset({"clean"})  # micromamba 2.9.0: "clean: The following arguments were not expected: -r"
_DROP = frozenset({"CONDARC", "MAMBARC", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL",
                   "MAMBA_ROOT_PREFIX", "CONDA_ENVS_PATH", "CONDA_ENVS_DIRS", "CONDA_PKGS_DIRS"})

USAGE = """usage: conda COMMAND [OPTIONS]

This conda is xrunner's front end over micromamba. Supported commands:
  create, install, update, remove, uninstall, list, search, info, clean, config,
  env list|create|export|remove, run
Run a tool in an environment with:  conda run -n NAME COMMAND [ARGS...]
Environments live in the project's .xrunner-env/conda folder.
"""
ACTIVATE_MSG = ("conda {verb}: activation changes the calling shell, which xrunner's conda cannot do.\n"
                "Run a tool with `conda run -n NAME CMD`, or call it as <prefix>/bin/CMD.")


@dataclass
class Parsed:
    verb: str | None = None
    sub: str | None = None
    tokens: list[str] = field(default_factory=list)
    name: str | None = None
    prefix: str | None = None
    root_flag: str | None = None
    yes: bool = False
    channels: list[str] = field(default_factory=list)
    override_channels: bool = False
    help: bool = False
    version: bool = False

    @property
    def key(self) -> str:
        return f"{self.verb} {self.sub}" if self.sub else (self.verb or "")


def parse_args(argv: Sequence[str]) -> Parsed:
    p = Parsed()
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        opt = tok.split("=", 1)[0]
        if p.verb is None and not tok.startswith("-"):
            p.verb = tok
            i += 1
            if tok == "env" and i < len(tokens) and not tokens[i].startswith("-"):
                p.sub = tokens[i]
                i += 1
            continue
        if opt in ("-r", "--root-prefix"):
            p.root_flag, i = opt_value(tokens, i)
            continue
        if opt in ("-n", "--name", "-p", "--prefix", "-c", "--channel"):
            value, j = opt_value(tokens, i)
            p.tokens.extend(tokens[i:j])
            if opt in ("-n", "--name"):
                p.name = value
            elif opt in ("-p", "--prefix"):
                p.prefix = value
            elif value is not None:
                p.channels.append(value)
            i = j
            continue
        if tok in ("-y", "--yes"):
            p.yes = True
        elif tok == "--override-channels":
            p.override_channels = True
        elif tok in ("-h", "--help"):
            p.help = True
        elif tok in ("-V", "--version") and p.verb is None:
            p.version = True
        p.tokens.append(tok)
        i += 1
    return p


def micromamba_env(root: Path, home: RuntimeHome, environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in _DROP}
    private = root / PRIVATE_HOME
    env.update({
        "HOME": str(private),
        "XDG_CACHE_HOME": str(private / ".cache"),
        "XDG_CONFIG_HOME": str(private / ".config"),
        "MAMBA_ROOT_PREFIX": str(root),
        "CONDA_PKGS_DIRS": str(home.path / PKGS_DIRNAME),
    })
    return env


def micromamba_argv(mm: Path, p: Parsed, root: Path) -> list[str]:
    argv = [str(mm), p.verb or ""] + ([p.sub] if p.sub else []) + p.tokens + ["--no-rc"]
    if p.key not in NO_ROOT_FLAG:
        argv += ["-r", str(root)]
    if p.key in CONFIRM and not p.yes:
        argv.append("-y")
    if p.key in CHANNELS and not p.override_channels:
        for ch in DEFAULT_CHANNELS:
            if ch not in p.channels:
                argv += ["-c", ch]
    return argv


def target_prefix(p: Parsed, root: Path, cwd: Path) -> Path:
    if p.prefix:
        pp = Path(p.prefix)
        return pp if pp.is_absolute() else cwd / pp
    if p.name and p.name != "base":
        return root / "envs" / p.name
    return root


def _write_record(mm: Path, prefix: Path, root: Path, env: dict[str, str], cwd: Path, err: TextIO) -> None:
    if not (prefix / "conda-meta").is_dir():
        return
    r = subprocess.run([str(mm), "env", "export", "--no-rc", "-r", str(root), "-p", str(prefix), "--explicit"],
                       env=env, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 or "@EXPLICIT" not in r.stdout:
        print(f"xrunner: warning: could not record the packages of {prefix}: {r.stderr.strip()[:200]}", file=err)
        return
    try:
        tmp = prefix / (EXPLICIT_NAME + ".tmp")
        tmp.write_text(r.stdout)
        os.replace(tmp, prefix / EXPLICIT_NAME)
    except OSError as e:
        print(f"xrunner: warning: could not record the packages of {prefix}: {e}", file=err)


def _micromamba_version(mm: Path) -> str:
    r = subprocess.run([str(mm), "--version"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def conda_main(argv: Sequence[str], home: RuntimeHome, cwd: Path | None = None,
               environ: Mapping[str, str] | None = None, err: TextIO | None = None) -> int:
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    environ = dict(os.environ if environ is None else environ)
    err = err if err is not None else sys.stderr
    p = parse_args(argv)
    if p.version:
        print(f"conda {_micromamba_version(find_micromamba(home, environ))} (micromamba via xrunner)")
        return 0
    if p.verb is None:
        print(USAGE, end="", file=sys.stdout if p.help else err)
        return 0 if p.help else 2
    if p.verb in REFUSED_VERBS:
        print(ACTIVATE_MSG.format(verb=p.verb), file=err)
        return 1
    if p.verb not in PASS_VERBS or (p.verb == "env" and p.sub not in ENV_SUBVERBS):
        print(f"conda: '{p.key}' is not supported by xrunner's conda.\n{USAGE}", end="", file=err)
        return 2
    mm = find_micromamba(home, environ)
    root = resolve_root(cwd, environ, home, p.root_flag)
    if p.name and p.name != "base" and p.key in LOOKUP:
        root = lookup_root(root, home, p.name)
    ensure_root(root.path)
    if root.source == "home":
        print(f"xrunner: no project env folder found; using {root.path}", file=err)
    env = micromamba_env(root.path, home, environ)
    code = subprocess.run(micromamba_argv(mm, p, root.path), env=env, cwd=cwd).returncode
    if code == 0 and p.key in RECORD:
        _write_record(mm, target_prefix(p, root.path, cwd), root.path, env, cwd, err)
    return code
```

The warning prints only when resolution fell through to the home root (source `"home"`). A lookup that deliberately found an existing env in the home root has source `"lookup"` and prints nothing.

- [ ] **Step 6: Wire the `conda` verb into cli.py**

In `_split_argv`, next to the `run`/`create` branch, add a branch that hands everything after `conda` to the command untouched:

```python
        if tok == "conda":
            return argv[: i + 1], argv[i + 1 :]
```

Add the command function near `cmd_docker`:

```python
def cmd_conda(rt: Runtime, args) -> int:
    from xcodon_runtime.condashim import conda_main

    return conda_main(list(args.rest), rt.home)
```

Register the parser next to the `docker` parser:

```python
    s = sub.add_parser("conda", help="conda's command line over a pinned micromamba (see `xrunner conda --help`)",
                       add_help=False)
    s.set_defaults(func=cmd_conda, rest=[])
```

`main` already sets `args.rest = rest` when `_split_argv` returns a tail.

- [ ] **Step 7: Run the tests, the full suite, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_condashim.py tests/test_cli.py tests/test_cli_docker.py`, then the full suite and ruff.

```bash
git add src/xcodon_runtime/condaroot.py src/xcodon_runtime/condashim.py src/xcodon_runtime/cli.py tests/fake_micromamba.py tests/test_condashim.py
git commit -m "feat: xrunner conda passes conda commands to micromamba with project-local, isolated settings"
```

---

### Task 3: `conda run`

**Files:**
- Create: `src/xcodon_runtime/condarun.py`
- Modify: `src/xcodon_runtime/condashim.py` (dispatch `run`; add `run` to the usage text, which already lists it)
- Test: `tests/test_condarun.py`

**Interfaces:**
- Consumes: `condaroot.resolve_root`, `condaroot.lookup_root`, `condaroot.opt_value`.
- Produces: `class RunUsageError(ValueError)`; `@dataclass class RunArgs: name: str | None; prefix: str | None; root_flag: str | None; cwd: str | None; command: list[str]`; `parse_run_args(argv: Sequence[str]) -> RunArgs`; `activation_env(prefix: Path, label: str, environ: Mapping[str, str]) -> dict[str, str]`; `run_main(argv: Sequence[str], home: RuntimeHome, cwd: Path, environ: Mapping[str, str], err: TextIO) -> int` (does not return on success: it replaces the process).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_condarun.py
import subprocess
import sys
from pathlib import Path

import pytest

from xcodon_runtime.condarun import RunUsageError, parse_run_args


def _xr(args, cwd, env, stdin=None):
    return subprocess.run([sys.executable, "-m", "xcodon_runtime.cli", *args], cwd=cwd, env=env,
                          input=stdin, capture_output=True, text=True, timeout=60)


def make_env(prefix: Path, tools: dict[str, str]) -> Path:
    (prefix / "conda-meta").mkdir(parents=True, exist_ok=True)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        tool = prefix / "bin" / name
        tool.write_text("#!/bin/sh\n" + body + "\n")
        tool.chmod(0o755)
    return prefix


TOOLS = {
    "echoargs": 'printf "%s|" "$@"; echo; echo "HOME=$HOME PREFIX=$CONDA_PREFIX ENV=$CONDA_DEFAULT_ENV LVL=$CONDA_SHLVL"',
    "rc": "exit 7",
    "catin": "cat",
}


@pytest.fixture
def project(tmp_path, home):
    proj = tmp_path / "proj"
    (proj / "workspace").mkdir(parents=True)
    make_env(proj / ".xrunner-env" / "conda" / "envs" / "tools", TOOLS)
    return proj


@pytest.fixture
def env(home):
    return {"PATH": "/usr/bin:/bin", "HOME": "/home/tester", "XCODON_RUNTIME_HOME": str(home.path)}


def test_parse_run_args():
    a = parse_run_args(["--no-capture-output", "-n", "tools", "--live-stream", "--cwd=w", "bwa", "-n", "x"])
    assert (a.name, a.cwd, a.command) == ("tools", "w", ["bwa", "-n", "x"])
    assert parse_run_args(["-p", "p", "--", "-weird"]).command == ["-weird"]
    with pytest.raises(RunUsageError):
        parse_run_args(["--bogus", "x"])
    with pytest.raises(RunUsageError):
        parse_run_args(["-n"])


def test_arguments_and_activation_variables(project, env):
    r = _xr(["conda", "run", "-n", "tools", "echoargs", "a b", "$HOME", ">x"], project, env)
    assert r.returncode == 0, r.stderr
    prefix = project / ".xrunner-env" / "conda" / "envs" / "tools"
    assert r.stdout.splitlines() == ["a b|$HOME|>x|", f"HOME=/home/tester PREFIX={prefix} ENV=tools LVL=1"]
    assert not (project / "x").exists()


def test_exit_code_stdin_and_path(project, env):
    assert _xr(["conda", "run", "-n", "tools", "rc"], project, env).returncode == 7
    assert _xr(["conda", "run", "-n", "tools", "catin"], project, env, stdin="hello\n").stdout == "hello\n"
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "command -v echoargs"], project, env)
    assert r.stdout.strip() == str(project / ".xrunner-env" / "conda" / "envs" / "tools" / "bin" / "echoargs")


def test_activate_d_scripts_are_sourced(project, env):
    d = project / ".xrunner-env" / "conda" / "envs" / "tools" / "etc" / "conda" / "activate.d"
    d.mkdir(parents=True)
    (d / "java.sh").write_text("export FROM_ACTIVATE=yes\n")
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "echo $FROM_ACTIVATE"], project, env)
    assert r.stdout == "yes\n"
    r = _xr(["conda", "run", "-n", "tools", "echoargs", "a b", "|"], project, env)
    assert r.stdout.splitlines()[0] == "a b|||"
    assert _xr(["conda", "run", "-n", "tools", "rc"], project, env).returncode == 7


def test_missing_env_and_missing_command(project, env):
    r = _xr(["conda", "run", "-n", "nope", "true"], project, env)
    assert r.returncode == 1 and "EnvironmentLocationNotFound: Not a conda environment:" in r.stderr
    r = _xr(["conda", "run", "-n", "tools", "no-such-tool"], project, env)
    assert r.returncode == 127 and "no-such-tool" in r.stderr


def test_prefix_env_cwd_and_ignored_flags(project, env):
    make_env(project / "workspace" / "penv", {"hello": "echo hi from $CONDA_DEFAULT_ENV"})
    r = _xr(["conda", "run", "-p", "workspace/penv", "hello"], project, env)
    assert r.stdout == f"hi from {project / 'workspace' / 'penv'}\n"
    r = _xr(["conda", "run", "-n", "tools", "--cwd", "workspace", "sh", "-c", "pwd"], project, env)
    assert r.stdout.strip() == str(project / "workspace")
    r = _xr(["conda", "run", "--no-capture-output", "--live-stream", "-n", "tools", "echoargs", "z"], project, env)
    assert r.stdout.splitlines()[0] == "z|"
    r = _xr(["conda", "run", "-n", "tools", "--", "echoargs", "-n"], project, env)
    assert r.stdout.splitlines()[0] == "-n|"


def test_usage_errors_exit_2(project, env):
    assert _xr(["conda", "run", "-n", "tools"], project, env).returncode == 2
    assert _xr(["conda", "run", "--bogus", "x"], project, env).returncode == 2


def test_env_in_the_home_root_is_found(project, env, home):
    make_env(home.path / "conda" / "envs" / "old", {"hello": "echo old"})
    assert _xr(["conda", "run", "-n", "old", "hello"], project, env).stdout == "old\n"


def test_base_env_is_the_root(project, env):
    make_env(project / ".xrunner-env" / "conda", {"basetool": "echo base"})
    assert _xr(["conda", "run", "basetool"], project, env).stdout == "base\n"
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_condarun.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.condarun'`

- [ ] **Step 3: Write condarun.py**

```python
# src/xcodon_runtime/condarun.py
"""`xrunner conda run`: run a command inside a conda env. See spec section 12.5.

micromamba 2.9.0's own `run` fails on this host (`exec: --: invalid option` from
its wrapper script), so xrunner activates the env itself and replaces its own
process with the command. Arguments, stdin, stdout and the exit code are the
command's own.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import lookup_root, opt_value, resolve_root
from xcodon_runtime.home import RuntimeHome

_VALUE_OPTS = ("-n", "--name", "-p", "--prefix", "-r", "--root-prefix", "--cwd")
_IGNORED = frozenset({"--no-capture-output", "--live-stream", "-v", "--verbose", "--dev",
                      "--debug-wrapper-scripts", "-q", "--quiet", "--no-rc"})


class RunUsageError(ValueError):
    """A `conda run` command line xrunner cannot accept."""


@dataclass
class RunArgs:
    name: str | None = None
    prefix: str | None = None
    root_flag: str | None = None
    cwd: str | None = None
    command: list[str] = field(default_factory=list)


def parse_run_args(argv: Sequence[str]) -> RunArgs:
    tokens = list(argv)
    a = RunArgs()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        opt = tok.split("=", 1)[0]
        if tok == "--":
            i += 1
            break
        if opt in _VALUE_OPTS:
            value, i = opt_value(tokens, i)
            if value is None:
                raise RunUsageError(f"{opt} needs a value")
            if opt in ("-n", "--name"):
                a.name = value
            elif opt in ("-p", "--prefix"):
                a.prefix = value
            elif opt in ("-r", "--root-prefix"):
                a.root_flag = value
            else:
                a.cwd = value
            continue
        if tok in _IGNORED:
            i += 1
            continue
        if tok.startswith("-"):
            raise RunUsageError(f"unknown option {tok}")
        break
    a.command = tokens[i:]
    return a


def activation_env(prefix: Path, label: str, environ: Mapping[str, str]) -> dict[str, str]:
    env = dict(environ)
    env["PATH"] = str(prefix / "bin") + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    env["CONDA_PREFIX"] = str(prefix)
    env["CONDA_DEFAULT_ENV"] = label
    env["CONDA_SHLVL"] = "1"
    return env


def run_main(argv: Sequence[str], home: RuntimeHome, cwd: Path, environ: Mapping[str, str],
             err: TextIO) -> int:
    try:
        a = parse_run_args(argv)
    except RunUsageError as e:
        print(f"conda run: {e}", file=err)
        return 2
    if not a.command:
        print("conda run: a command is required, for example: conda run -n NAME COMMAND", file=err)
        return 2
    root = resolve_root(cwd, environ, home, a.root_flag)
    if a.prefix:
        prefix = Path(a.prefix) if Path(a.prefix).is_absolute() else cwd / a.prefix
        label = str(prefix)
    elif a.name and a.name != "base":
        root = lookup_root(root, home, a.name)
        prefix = root.path / "envs" / a.name
        label = a.name
    else:
        prefix = root.path
        label = "base"
    if not (prefix / "conda-meta").is_dir():
        print(f"EnvironmentLocationNotFound: Not a conda environment: {prefix}", file=err)
        return 1
    env = activation_env(prefix, label, environ)
    workdir = cwd if a.cwd is None else (Path(a.cwd) if Path(a.cwd).is_absolute() else cwd / a.cwd)
    scripts = sorted((prefix / "etc" / "conda" / "activate.d").glob("*.sh"))
    if scripts:
        sources = "; ".join(f". {shlex.quote(str(s))}" for s in scripts)
        argv_exec = ["/bin/sh", "-c", f'{sources}; exec "$@"', "sh", *a.command]
    else:
        argv_exec = list(a.command)
    try:
        os.chdir(workdir)
    except OSError as e:
        print(f"conda run: cannot change to {workdir}: {e}", file=err)
        return 1
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(argv_exec[0], argv_exec, env)
    except FileNotFoundError:
        print(f"xrunner: {a.command[0]}: command not found", file=err)
        return 127
    except PermissionError:
        print(f"xrunner: {a.command[0]}: permission denied", file=err)
        return 126
    return 1  # not reached: execvpe only returns by raising
```

- [ ] **Step 4: Dispatch `run` from conda_main**

At the top of `conda_main`, right after `err` is set and before `parse_args`, add:

```python
    argv = list(argv)
    if argv[:1] == ["run"]:
        from xcodon_runtime.condarun import run_main

        return run_main(argv[1:], home, cwd, environ, err)
```

- [ ] **Step 5: Run the tests, the full suite, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_condarun.py tests/test_condashim.py`, then the full suite and ruff.

```bash
git add src/xcodon_runtime/condarun.py src/xcodon_runtime/condashim.py tests/test_condarun.py
git commit -m "feat: xrunner conda run activates the env itself and execs the command"
```

---

### Task 4: `xrunner shim install conda`, the shared shim writer, and docs

**Files:**
- Create: `src/xcodon_runtime/shim.py`
- Modify: `src/xcodon_runtime/cli.py` (`cmd_shim`, the `shim install` parser)
- Modify: `README.md`; `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md` (two sentences in 12.3 and 12.7)
- Test: `tests/test_shim_conda.py`

**Interfaces:**
- Consumes: `daemon.SHIM_MARKER`; `micromamba.install_micromamba(home, source=None) -> Path`; `micromamba.MICROMAMBA_VERSION`.
- Produces: `shim.CONDA_MARKER = "# conda shim installed by xrunner"`, `shim.CONDA_NAMES = ("conda", "mamba", "micromamba")`, `class ShimRefused(XcodonError)`, `is_xrunner_shim(path) -> bool`, `resolve_real(name: str, path_value: str | None = None) -> str | None`, `xrunner_executable() -> str`, `check_install(target_dir: Path, names: Sequence[str], force: bool) -> None`, `write_shim(target_dir: Path, name: str, marker: str, subcommand: str, xrunner: str | None = None) -> Path`, `path_hint(target_dir: Path) -> str | None`. CLI: `xrunner shim install [docker|conda] [--dir DIR] [--force] [--micromamba PATH]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shim_conda.py
import os
import subprocess
import sys

import pytest

from tests.fake_micromamba import make_fake_micromamba
from xcodon_runtime import cli
from xcodon_runtime.shim import CONDA_MARKER, is_xrunner_shim, resolve_real


@pytest.fixture
def fake_mm(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin")


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    d = tmp_path / "empty-path"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def test_install_writes_three_forwarding_shims(home, tmp_path, fake_mm, empty_path, capfd):
    shim_dir = tmp_path / "shim"
    assert cli.main(["shim", "install", "conda", "--dir", str(shim_dir), "--micromamba", str(fake_mm)]) == 0
    out = capfd.readouterr().out
    for name in ("conda", "mamba", "micromamba"):
        p = shim_dir / name
        text = p.read_text()
        assert text.startswith("#!/bin/sh\n") and CONDA_MARKER in text
        assert text.rstrip().endswith('conda "$@"') and "xrunner" in text
        assert os.access(p, os.X_OK) and is_xrunner_shim(p)
        assert f"installed {p}" in out
    assert (home.path / "bin" / "micromamba-2.9.0").read_bytes() == fake_mm.read_bytes()
    assert "add it to PATH" in out
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path), "HOME": str(tmp_path)}
    r = subprocess.run(["mamba", "--version"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "conda 2.9.0 (micromamba via xrunner)\n"


def test_install_refuses_a_real_conda_on_path(home, tmp_path, fake_mm, monkeypatch):
    real = tmp_path / "realbin"
    real.mkdir()
    (real / "mamba").write_text("#!/bin/sh\necho real\n")
    (real / "mamba").chmod(0o755)
    monkeypatch.setenv("PATH", str(real))
    assert resolve_real("mamba") == str(real / "mamba")
    args = ["shim", "install", "conda", "--dir", str(tmp_path / "shim"), "--micromamba", str(fake_mm)]
    assert cli.main(args) == 125
    assert not (tmp_path / "shim" / "conda").exists()
    assert cli.main(args + ["--force"]) == 0


def test_install_never_writes_through_a_link_or_over_a_user_file(home, tmp_path, fake_mm, empty_path):
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    target = tmp_path / "user-conda"
    target.write_text("#!/bin/sh\necho mine\n")
    (shim_dir / "conda").symlink_to(target)
    args = ["shim", "install", "conda", "--dir", str(shim_dir), "--micromamba", str(fake_mm)]
    assert cli.main(args) == 125
    assert cli.main(args + ["--force"]) == 0
    assert target.read_text() == "#!/bin/sh\necho mine\n"
    assert not (shim_dir / "conda").is_symlink() and is_xrunner_shim(shim_dir / "conda")
    assert cli.main(args) == 0, "replacing its own shims needs no --force"


def test_a_conda_shim_on_path_is_not_a_real_conda(home, tmp_path, fake_mm, empty_path, monkeypatch):
    first = tmp_path / "first"
    assert cli.main(["shim", "install", "conda", "--dir", str(first), "--micromamba", str(fake_mm)]) == 0
    monkeypatch.setenv("PATH", f"{first}:{empty_path}")
    assert resolve_real("conda") is None
    assert cli.main(["shim", "install", "conda", "--dir", str(tmp_path / "second"),
                     "--micromamba", str(fake_mm)]) == 0


def test_docker_is_still_the_default_kind(home, tmp_path, empty_path):
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "d")]) == 0
    assert (tmp_path / "d" / "docker").exists() and not (tmp_path / "d" / "conda").exists()
    assert cli.main(["shim", "install", "docker", "--dir", str(tmp_path / "d"), "--micromamba", "x"]) == 125
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_shim_conda.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.shim'`

- [ ] **Step 3: Write shim.py**

```python
# src/xcodon_runtime/shim.py
"""The small executables `xrunner shim install` puts on PATH. See spec sections 11.4 and 12.8."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from xcodon_runtime.daemon import SHIM_MARKER as DOCKER_MARKER
from xcodon_runtime.errors import XcodonError

CONDA_MARKER = "# conda shim installed by xrunner"
MARKERS = (DOCKER_MARKER, CONDA_MARKER)
CONDA_NAMES = ("conda", "mamba", "micromamba")


class ShimRefused(XcodonError):
    """Installing a shim would hide or overwrite something that is not ours."""


def is_xrunner_shim(path: str | os.PathLike) -> bool:
    """True when ``path`` is a file whose first 512 bytes carry an xrunner shim marker."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return False
    return any(m.encode() in head for m in MARKERS)


def resolve_real(name: str, path_value: str | None = None) -> str | None:
    """Like shutil.which, but skip xrunner shims."""
    value = os.environ.get("PATH", "") if path_value is None else path_value
    for d in value.split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and not is_xrunner_shim(candidate):
            return candidate
    return None


def xrunner_executable() -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), "xrunner")
    if os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("xrunner") or "xrunner"


def check_install(target_dir: Path, names: Sequence[str], force: bool) -> None:
    if not force:
        for name in names:
            real = resolve_real(name)
            if real:
                raise ShimRefused(f"a real {name} is on PATH at {real}; pass --force to install the shim anyway")
        for name in names:
            path = target_dir / name
            if os.path.lexists(path) and not is_xrunner_shim(path):
                raise ShimRefused(f"{path} already exists and is not an xrunner shim; pass --force to overwrite it")
    for name in names:
        path = target_dir / name
        if path.is_dir() and not path.is_symlink():
            raise ShimRefused(f"{path} is a directory; remove it first")


def write_shim(target_dir: Path, name: str, marker: str, subcommand: str, xrunner: str | None = None) -> Path:
    """Write ``target_dir/name`` atomically; a link there is replaced, never written through."""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    script = f"#!/bin/sh\n{marker}\nexec {shlex.quote(xrunner or xrunner_executable())} {subcommand} \"$@\"\n"
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=f".{name}-shim-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(tmp_name, 0o755)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def path_hint(target_dir: Path) -> str | None:
    if str(target_dir) in os.environ.get("PATH", "").split(os.pathsep):
        return None
    return f'add it to PATH: export PATH="{target_dir}:$PATH"'
```

- [ ] **Step 4: Rewrite cmd_shim and its parser in cli.py**

Replace the body of `cmd_shim` with:

```python
def cmd_shim(rt: Runtime, args) -> int:
    from xcodon_runtime.micromamba import install_micromamba
    from xcodon_runtime.shim import CONDA_MARKER, CONDA_NAMES, check_install, path_hint, write_shim

    target_dir = Path(args.dir or os.path.dirname(sys.executable)).expanduser().resolve()
    if args.kind == "docker":
        if args.micromamba:
            raise UsageError("--micromamba only applies to `xrunner shim install conda`")
        check_install(target_dir, [SHIM_NAME], args.force)
        written = [write_shim(target_dir, SHIM_NAME, SHIM_MARKER, "docker")]
    else:
        check_install(target_dir, CONDA_NAMES, args.force)
        source = Path(args.micromamba).expanduser() if args.micromamba else None
        print(f"micromamba at {install_micromamba(rt.home, source=source)}")
        written = [write_shim(target_dir, name, CONDA_MARKER, "conda") for name in CONDA_NAMES]
    for path in written:
        print(f"installed {path}")
    hint = path_hint(target_dir)
    if hint:
        print(hint)
    return 0
```

Replace the `shim` parser block with:

```python
    s = sub.add_parser("shim", help="install docker or conda commands that forward to xrunner")
    ssub = s.add_subparsers(dest="shim_cmd", required=True)
    i = ssub.add_parser("install", help="write docker (default) or conda/mamba/micromamba scripts")
    i.add_argument("kind", nargs="?", choices=("docker", "conda"), default="docker")
    i.add_argument("--dir", help="where to write them (default: beside the xrunner executable)")
    i.add_argument("--force", action="store_true", help="install even if a real one is already on PATH")
    i.add_argument("--micromamba", help="conda only: copy this micromamba binary instead of downloading the pinned one")
    i.set_defaults(func=cmd_shim)
```

Remove imports in cli.py that become unused (`shlex`, `tempfile`, `is_shim`, `resolve_docker`, ...); keep any still used elsewhere in the file. The existing docker shim tests in `tests/test_cli_docker.py` must pass unchanged: the messages `a real docker is on PATH at ...`, `... already exists and is not an xrunner shim; pass --force to overwrite it` and `... is a directory; remove it first` keep their exact wording through `check_install`.

- [ ] **Step 5: Docs**

In `README.md`, after the "Build and commit" section, add:

```markdown
## Tools without conda

Agents often install command-line tools with `conda create`, `conda install` and
`conda run`. On a host with no conda, xrunner can answer those calls itself:

    xrunner shim install conda --dir ~/.xcodon/shim
    export PATH="$HOME/.xcodon/shim:$PATH"

This downloads a pinned micromamba (2.9.0, from conda-forge, checksum-verified) and
writes `conda`, `mamba` and `micromamba` scripts that forward to `xrunner conda`.
Offline, pass `--micromamba PATH` to use a binary you already have.

- Environments live in the project's `.xrunner-env/conda` (found from the working
  directory, or from `XRUNNER_ENV_DIR`), else under the xrunner home. Downloads are
  cached once under the xrunner home.
- Your own `~/.conda` and `~/.condarc` are never read or written.
- `conda run -n NAME CMD` runs CMD with the env on PATH; `conda activate` is not
  supported, because it changes the calling shell.
- Each env gets `conda-explicit.txt`, listing every package URL and checksum, so it
  can be rebuilt with `conda create -p PATH --file conda-explicit.txt`.
- The shim refuses to install while a real conda, mamba or micromamba is on PATH,
  unless you pass `--force`.
```

In the spec, section 12.3, change "Lookups of an existing `-n NAME` env, for `run`, `list`, `env export` and `remove`," to "Lookups of an existing `-n NAME` env, for `run`, `list`, `env export`, `remove`, `uninstall`, `install` and `update`,". In section 12.7, change the first bullet to: "xrunner pins conda-forge's micromamba 2.9.0 package for linux-64: its URL, the archive's SHA-256 and the SHA-256 of `bin/micromamba` live in `micromamba.py`. Other platforms are an error." and change the last bullet to "The pinned binary needs glibc 2.17 or newer and links only against glibc." In section 12.4, change the `-y` bullet's verb list to "`create`, `install`, `update`, `remove`, `uninstall`, `clean`, `env create`, `env remove`" and add a bullet: "`-r <root>` on every command except `clean`, which micromamba 2.9.0 rejects it for; `clean` gets the root from `MAMBA_ROOT_PREFIX`."

- [ ] **Step 6: Run the tests, the full suite, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_shim_conda.py tests/test_cli_docker.py tests/test_daemon.py`, then the full suite and ruff.

```bash
git add src/xcodon_runtime/shim.py src/xcodon_runtime/cli.py tests/test_shim_conda.py README.md docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md
git commit -m "feat: xrunner shim install conda writes conda/mamba/micromamba shims over the pinned micromamba"
```

---

### Task 5: End to end with the real micromamba

**Files:**
- Test: `tests/test_conda_e2e.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the test**

```python
# tests/test_conda_e2e.py
"""The agent's conda calls, through the shim, with the real pinned micromamba and bioconda."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.network


def _run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)


def test_conda_shim_end_to_end(tmp_path):
    home = tmp_path / "rt-home"
    shim_dir = tmp_path / "shim"
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    project = tmp_path / "project"
    (project / ".xrunner-env").mkdir(parents=True)
    xrunner = Path(sys.executable).with_name("xrunner")
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(user_home), "XCODON_RUNTIME_HOME": str(home)}

    r = _run([str(xrunner), "shim", "install", "conda", "--dir", str(shim_dir)], tmp_path, {**env, "PATH": str(empty)})
    assert r.returncode == 0, r.stdout + r.stderr

    r = _run(["conda", "create", "-n", "bwa_env", "-c", "bioconda", "seqtk"], project, env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    env_prefix = project / ".xrunner-env" / "conda" / "envs" / "bwa_env"
    assert (env_prefix / "bin" / "seqtk").exists()
    assert "seqtk" in (env_prefix / "conda-explicit.txt").read_text()

    r = _run(["conda", "run", "-n", "bwa_env", "seqtk"], project, env)
    assert "Usage" in r.stdout + r.stderr
    assert _run(["conda", "run", "-n", "bwa_env", "sh", "-c", "exit 3"], project, env).returncode == 3

    r = _run(["conda", "create", "-p", "workspace/conda_env", "-c", "bioconda", "seqtk"], project, env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    record = (project / "workspace" / "conda_env" / "conda-explicit.txt").read_text()
    assert "@EXPLICIT" in record and "seqtk" in record
    assert _run([str(project / "workspace" / "conda_env" / "bin" / "seqtk")], project, env).returncode == 1

    r = _run(["conda", "env", "list"], project, env)
    assert r.returncode == 0 and "bwa_env" in r.stdout and "conda_env" in r.stdout

    assert _run(["mamba", "--version"], project, env).stdout.startswith("conda 2.9.0")
    r = _run(["conda", "activate", "bwa_env"], project, env)
    assert r.returncode == 1 and "conda run" in r.stderr

    assert not (user_home / ".conda").exists() and not (user_home / ".cache").exists()
    assert (home / "conda-pkgs").is_dir()
```

- [ ] **Step 2: Run it**

Run: `XCODON_TEST_NETWORK=1 PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_conda_e2e.py -v` (it downloads micromamba and seqtk, a few MB; allow several minutes). Then the full suite without the variable, which must skip it cleanly.

- [ ] **Step 3: Commit**

```bash
git add tests/test_conda_e2e.py
git commit -m "test: the agent's conda calls through the shim with the real micromamba"
```

---

## Plan Self-Review

- Spec coverage. 12.2 shape: Tasks 2 and 4. 12.3 root order, fallback warning and lookups: Task 2. 12.4 isolation settings, `--no-rc`, channels and `-y`: Task 2. 12.5 command surface: Task 2 (pass-through, refused, unsupported, version, leading options), Task 3 (`run`). 12.6 rerun record: Task 2. 12.7 binary: Task 1 (pin, download, verify, copy, env override, never downloading at call time), Task 4 (docs wording). 12.8 shim install: Task 4. 12.9 errors: Tasks 1-3 (125 via `XcodonError`, 1 and 127 in run, 2 for usage). 12.10 testing: Tasks 1-5. 12.11: nothing added.
- Deliberate additions to the spec, recorded in Task 4's spec edit: lookups also cover `install`, `update` and `uninstall`, so an env created under the home root before `.xrunner-env` existed can still be changed; `clean` gets `-y` and no `-r`, because micromamba 2.9.0 rejects `-r` for `clean` (checked on this host).
- Names are consistent across tasks: `find_micromamba`, `install_micromamba`, `pinned_path`, `MicromambaMissing`; `Root`, `resolve_root`, `lookup_root`, `ensure_root`, `opt_value`; `parse_args`, `Parsed.key`, `micromamba_env`, `micromamba_argv`, `target_prefix`, `conda_main`; `parse_run_args`, `RunArgs`, `RunUsageError`, `activation_env`, `run_main`; `CONDA_MARKER`, `CONDA_NAMES`, `ShimRefused`, `is_xrunner_shim`, `resolve_real`, `check_install`, `write_shim`, `path_hint`.
