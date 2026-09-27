# src/xcodon_runtime/condaroot.py
"""Which conda root prefix a `xcrunner conda` call uses. See spec section 12.3."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import RuntimeHome

ENV_DIR_VAR = "XCRUNNER_ENV_DIR"
ENV_FOLDER_NAME = ".xcrunner-env"
ROOT_DIRNAME = "conda"


@dataclass(frozen=True)
class Root:
    path: Path
    source: str  # "flag", "env", "project", "home", or "lookup" (an existing env found in the home root)


def split_option(tok: str, short_value_letters: str) -> tuple[str, str | None]:
    """An option token's name and the value attached to it, or None when none is.

    A long option's name stops at `=` (`--name=foo` is `--name`, `foo`). A short
    option whose letter is in ``short_value_letters`` takes the rest of the token
    as its value (`-nfoo` is `-n`, `foo`; `-n=foo` is `-n`, `=foo`, as argparse
    reads it). Any other token, such as combined flags (`-yq`), is its own name."""
    if tok.startswith("--"):
        name, sep, value = tok.partition("=")
        return (name, value) if sep else (tok, None)
    if len(tok) > 2 and tok[0] == "-" and tok[1] in short_value_letters:
        return tok[:2], tok[2:]
    return tok, None


def opt_value(tokens: list[str], i: int) -> tuple[str | None, int]:
    """The value of the value-taking option at tokens[i] and the index after it,
    the way argparse (and so conda) reads it: attached to the token (see
    split_option), else the next token. The next token is not taken when it is
    itself an option (`-r -n c`): then the value is None."""
    tok = tokens[i]
    _, attached = split_option(tok, tok[1:2])
    if attached is not None:
        return attached, i + 1
    if i + 1 < len(tokens):
        nxt = tokens[i + 1]
        if nxt.startswith("-") and len(nxt) > 1:
            return None, i + 1
        return nxt, i + 2
    return None, i + 1


def abs_prefix(value: str, cwd: Path) -> Path:
    """A `-p/--prefix` value as an absolute path: a relative one is taken from cwd.
    micromamba 2.9.0 reads a relative value with no `/` as an env name
    (`<root>/envs/NAME`), so xcrunner always hands it this absolute path."""
    p = Path(value)
    return p if p.is_absolute() else cwd / p


def _trusted_env_folder(d: Path) -> bool:
    """A found `.xcrunner-env` is used only when its resolved directory is ours
    and no one else can write to it. In a shared parent such as /tmp, another
    user's folder would otherwise make `conda run` exec their binaries."""
    try:
        st = os.stat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (st.st_mode & 0o022)


def _find_env_folder_with_source(cwd: Path, environ: Mapping[str, str]) -> tuple[Path, str] | None:
    """``find_env_folder``'s result, plus which rule matched: "env" or "project"."""
    env_dir = environ.get(ENV_DIR_VAR)
    if env_dir:
        return Path(env_dir), "env"
    for d in (cwd, *cwd.parents):
        if _trusted_env_folder(d / ENV_FOLDER_NAME):
            return d / ENV_FOLDER_NAME, "project"
    return None


def find_env_folder(cwd: Path, environ: Mapping[str, str]) -> Path | None:
    """XCRUNNER_ENV_DIR, else the nearest trusted .xcrunner-env above cwd, else None."""
    found = _find_env_folder_with_source(cwd, environ)
    return found[0] if found else None


def resolve_root(cwd: Path, environ: Mapping[str, str], home: RuntimeHome,
                 explicit: str | None = None) -> Root:
    if explicit:
        p = Path(explicit)
        return Root(p if p.is_absolute() else cwd / p, "flag")
    found = _find_env_folder_with_source(cwd, environ)
    if found:
        folder, source = found
        return Root(folder / ROOT_DIRNAME, source)
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
    # A fresh root is a base env too, as in real conda: without conda-meta here,
    # a bare `conda list` (which targets the root itself) fails to find an environment.
    try:
        for d in (root / ".home", root / "envs", root / "conda-meta"):
            os.makedirs(d, mode=0o755, exist_ok=True)
    except OSError as e:
        raise XcodonError(f"cannot create the conda root prefix {root}: {e}") from e
