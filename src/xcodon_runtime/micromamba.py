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
# SHA-256 of `bin/micromamba` exactly as stored in the archive. Conda installers
# rewrite a placeholder prefix inside it (info/has_prefix); xrunner uses the
# file as extracted, which is safe because every call passes the root prefix
# explicitly.
MICROMAMBA_BINARY_SHA256 = "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"
MICROMAMBA_ENV = "XRUNNER_MICROMAMBA"
_MEMBER = "bin/micromamba"
_CHUNK = 1 << 20


class MicromambaMissing(XcodonError):
    """No usable micromamba binary."""


def pinned_path(home: RuntimeHome) -> Path:
    """``<home>/bin/micromamba-<version>/micromamba``. The file itself is named
    plain `micromamba` because micromamba names itself after its file in the
    hints and errors it prints ("micromamba run -n ..."), and `micromamba` is
    what the shim serves."""
    return home.path / "bin" / f"micromamba-{MICROMAMBA_VERSION}" / "micromamba"


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
    dest.parent.parent.mkdir(parents=True, exist_ok=True)
    with home.lock("micromamba"):
        if dest.parent.exists() and not dest.parent.is_dir():
            dest.parent.unlink()  # the binary itself, as an earlier layout stored it
        dest.parent.mkdir(exist_ok=True)
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
