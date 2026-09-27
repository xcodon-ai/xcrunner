"""Make a project an xcrunner sandbox for an agent. See spec section 14.

``activate(env_dir)`` writes ``docker``, ``conda``, ``mamba`` and ``micromamba``
shims into ``<env_dir>/bin``, makes sure the pinned micromamba is present, and
returns the two environment variables that route an agent's tools through
xcrunner: ``PATH`` with that ``bin`` folder first, and ``XCRUNNER_ENV_DIR``. The
caller applies them to its own process, so every command it starts inherits
them. Nothing outside the env folder and the xcrunner home is written.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Mapping

from xcodon_runtime.condaroot import ENV_DIR_VAR
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.micromamba import MicromambaMissing, find_micromamba, install_micromamba
from xcodon_runtime.shim import CONDA_MARKER, CONDA_NAMES, DOCKER_MARKER, write_shim

log = logging.getLogger(__name__)

BIN_DIRNAME = "bin"

__all__ = ["BIN_DIRNAME", "MicromambaMissing", "XcodonError", "activate"]


def _real_dir(path: Path) -> None:
    """Create ``path`` as a directory, or accept it if it already is one; never follow a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if not stat.S_ISDIR(st.st_mode):
        raise XcodonError(f"{path} is not a directory; remove it and try again")


def _ensure_micromamba(home: RuntimeHome, environ: Mapping[str, str]) -> None:
    try:
        find_micromamba(home, environ)
        return
    except MicromambaMissing:
        pass
    try:
        install_micromamba(home)
    except XcodonError as e:
        log.warning("could not install micromamba for the conda shim: %s; conda commands will fail "
                    "until `xcrunner shim install conda` succeeds", e)


def activate(env_dir: str | os.PathLike, home: RuntimeHome | None = None,
             environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Write the project's shims and return ``{"PATH": ..., "XCRUNNER_ENV_DIR": ...}`` to apply.

    ``env_dir`` is the project's env folder, an absolute path. Calling this again is
    safe: the shims are rewritten and the returned PATH lists the shim folder once,
    first. A micromamba that cannot be downloaded only logs a warning.
    """
    environ = os.environ if environ is None else environ
    env_path = Path(env_dir)
    if not env_path.is_absolute():
        raise XcodonError(f"the env folder must be an absolute path: {env_dir}")
    env_path.mkdir(parents=True, exist_ok=True)
    env_path = env_path.resolve()
    bin_dir = env_path / BIN_DIRNAME
    _real_dir(bin_dir)
    write_shim(bin_dir, "docker", DOCKER_MARKER, "docker")
    for name in CONDA_NAMES:
        write_shim(bin_dir, name, CONDA_MARKER, "conda")
    _ensure_micromamba(home if home is not None else RuntimeHome(), environ)
    rest = [p for p in environ.get("PATH", "").split(os.pathsep) if p and p != str(bin_dir)]
    return {"PATH": os.pathsep.join([str(bin_dir), *rest]), ENV_DIR_VAR: str(env_path)}
