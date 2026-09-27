"""The pinned micromamba binary behind `xcrunner conda`. See spec section 12.7.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import RuntimeHome

MICROMAMBA_VERSION = "2.9.0"
MICROMAMBA_URL = "https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.9.0-0.tar.bz2"
MICROMAMBA_ARCHIVE_SHA256 = "8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd"
# SHA-256 of `bin/micromamba` exactly as stored in the archive. Conda installers
# rewrite a placeholder prefix inside it (info/has_prefix); xcrunner uses the
# file as extracted, which is safe because every call passes the root prefix
# explicitly.
MICROMAMBA_BINARY_SHA256 = "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"
# The same micromamba release for Apple silicon Macs (spec 16.6). The binary is
# signed; it is used exactly as extracted, which keeps the signature valid.
MICROMAMBA_OSX_ARM64_URL = "https://conda.anaconda.org/conda-forge/osx-arm64/micromamba-2.9.0-0.tar.bz2"
MICROMAMBA_OSX_ARM64_ARCHIVE_SHA256 = "500f5074feb8d02c4296ef9921c3650ed2874171805a9fbb8fbb53896433646b"
MICROMAMBA_OSX_ARM64_BINARY_SHA256 = "ec2a072f028e1a7cf20f3e2e74d5a8127cf5a5f27636375b5359811565f4e5be"
MICROMAMBA_ENV = "XCRUNNER_MICROMAMBA"
_SUBDIRS = {("Linux", "x86_64"): "linux-64", ("Linux", "amd64"): "linux-64", ("Linux", "AMD64"): "linux-64",
            ("Darwin", "arm64"): "osx-arm64"}
SUPPORTED_SUBDIRS = ("linux-64", "osx-arm64")
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


@dataclass(frozen=True)
class Pin:
    subdir: str
    url: str
    archive_sha256: str
    binary_sha256: str


def host_subdir() -> str | None:
    """This host's conda platform subdir, when the conda shim supports it."""
    return _SUBDIRS.get((platform.system(), platform.machine()))


def _pin() -> Pin:
    """The pin for this host, read at call time from the module constants."""
    subdir = host_subdir()
    if subdir == "linux-64":
        return Pin(subdir, MICROMAMBA_URL, MICROMAMBA_ARCHIVE_SHA256, MICROMAMBA_BINARY_SHA256)
    if subdir == "osx-arm64":
        return Pin(subdir, MICROMAMBA_OSX_ARM64_URL, MICROMAMBA_OSX_ARM64_ARCHIVE_SHA256,
                   MICROMAMBA_OSX_ARM64_BINARY_SHA256)
    raise XcodonError(f"the conda shim supports {', '.join(SUPPORTED_SUBDIRS)} only; "
                      f"this host is {platform.system()} {platform.machine()}")


def _check_platform() -> None:
    _pin()


def find_micromamba(home: RuntimeHome, environ: Mapping[str, str] | None = None) -> Path:
    """The micromamba to run: $XCRUNNER_MICROMAMBA, else the pinned binary under the home."""
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
    raise MicromambaMissing("micromamba is not installed; run: xcrunner shim install conda")


def install_micromamba(home: RuntimeHome, source: Path | None = None,
                       opener=urllib.request.urlopen) -> Path:
    """Put micromamba at pinned_path(home): copy ``source``, or download and verify the pin."""
    pin = _pin()
    dest = pinned_path(home)
    if source is not None and not Path(source).is_file():
        raise XcodonError(f"--micromamba {source} is not a file")
    if source is None and dest.is_file() and _sha256(dest) == pin.binary_sha256:
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
                _download_binary(work, binary, opener, pin)
            binary.chmod(0o755)
            os.replace(binary, dest)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return dest


def _download_binary(work: Path, binary: Path, opener, pin: Pin) -> None:
    archive = work / "micromamba.tar.bz2"
    try:
        with opener(pin.url, timeout=120) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out, _CHUNK)
    except OSError as e:
        raise XcodonError(
            f"could not download micromamba from {pin.url}: {e}; "
            f"on a host without access, pass --micromamba PATH") from e
    got = _sha256(archive)
    if got != pin.archive_sha256:
        raise XcodonError(f"micromamba archive checksum mismatch: got {got}, want {pin.archive_sha256}")
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
    if got != pin.binary_sha256:
        raise XcodonError(f"micromamba binary checksum mismatch: got {got}, want {pin.binary_sha256}")
