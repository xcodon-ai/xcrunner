"""Extract one image layer tar as an unprivileged user."""

from __future__ import annotations

import gzip
import logging
import os
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
    # Sniff magic bytes to detect compression format.
    with open(path, "rb") as f:
        head = f.read(4)

    if head.startswith(GZIP_MAGIC):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    if head.startswith(ZSTD_MAGIC):
        try:
            import zstandard
        except ImportError:
            raise UnsupportedLayer(
                f"{path.name} is zstd-compressed; install the extra: pip install 'xcodon-runtime[zstd]'"
            ) from None
        return zstandard.ZstdDecompressor().stream_reader(  # type: ignore[return-value]
            open(path, "rb")
        )
    return open(path, "rb")


def _hardlink_escapes(linkname: str, dest: str) -> bool:
    """True when a hardlink target would land outside ``dest``.

    The stdlib ``tar`` filter does not look at a hardlink's linkname, so
    without this check ``tarfile`` would link a host file into the layer and
    then rewrite that host inode's mode and times.
    """
    if os.path.isabs(linkname):
        return True
    dest_real = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(dest_real, linkname))
    return target != dest_real and not target.startswith(dest_real + os.sep)


def _layer_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """The stdlib ``tar`` filter, plus owner read/write so later entries can land inside."""
    if member.islnk():
        if _hardlink_escapes(member.linkname, dest):
            raise tarfile.LinkOutsideDestinationError(member, member.linkname)
        if not os.path.exists(os.path.join(dest, member.linkname)):
            # The target is not on disk, so tarfile would scan the rest of the
            # archive for it. On a stream that reads the whole tar away and
            # leaves nothing for the members after this one.
            raise tarfile.FilterError(
                f"{member.name!r} is a hardlink to {member.linkname!r}, which was not extracted"
            )
    member = tarfile.tar_filter(member, dest)
    # Clear ownership (tar ownership is ignored; files belong to the invoking user).
    mode = member.mode if member.mode is not None else (0o755 if member.isdir() else 0o644)
    if member.isdir():
        return member.replace(
            uid=None, gid=None, uname=None, gname=None,
            mode=mode | 0o700
        )
    if member.isreg():
        return member.replace(
            uid=None, gid=None, uname=None, gname=None,
            mode=mode | 0o600
        )
    return member.replace(uid=None, gid=None, uname=None, gname=None)


def extract_layer(stream: BinaryIO, dest: Path) -> int:
    """Extract a layer tar into ``dest``. Returns how many entries were skipped.

    Ownership in the tar is ignored: every file belongs to the invoking user.
    Setuid, setgid, sticky, and group/other write bits are dropped by the filter.
    Device nodes, sockets, and fifos are skipped. Entries that would escape
    ``dest`` are refused by the filter and skipped, including hardlinks whose
    target lies outside ``dest`` and hardlinks with no target at all.
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
            except KeyError as e:
                # A hardlink whose target is neither on disk nor an earlier member.
                log.warning("skip %s: %s", member.name, e)
                skipped += 1
            except (PermissionError, OSError) as e:
                log.warning("skip %s: %s", member.name, e)
                skipped += 1
    return skipped
