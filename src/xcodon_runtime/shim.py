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
