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
