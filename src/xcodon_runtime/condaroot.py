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
