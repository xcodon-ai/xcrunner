"""Docker-style progress lines for a pull, written to a stream (the CLI uses stderr).

    latest: Pulling from coala-info/coala-runtime-python
    a1b2c3d4e5f6: Pulling fs layer
    a1b2c3d4e5f6: Downloading [=====>          ]  12.30MB/45.60MB    (terminal only, updated in place)
    a1b2c3d4e5f6: Download complete
    0f1e2d3c4b5a: Already exists
    a1b2c3d4e5f6: Pull complete
    Digest: sha256:...
    Status: Downloaded newer image for ghcr.io/coala-info/coala-runtime-python:latest

Layers are named by the first 12 hex digits of their blob digest, as docker does.
On a terminal the download line is redrawn with a bar; elsewhere (a log, cwltool)
only the step changes are written, so logs stay short.
"""

from __future__ import annotations

from typing import TextIO

from xcodon_runtime.reference import parse_reference

BAR_WIDTH = 50
# Steps that are not written to a log: the terminal shows them as an in-place line only.
_TRANSIENT = {"Extracting"}


def human_size(n: int) -> str:
    """Decimal units, as docker prints them: 999B, 1.00kB, 12.34MB, 2.50GB."""
    if n < 1000:
        return f"{n}B"
    for unit, scale in (("kB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        if n < scale * 1000 or unit == "GB":
            return f"{n / scale:.2f}{unit}"
    return f"{n}B"  # not reached


def short_id(digest: str) -> str:
    return digest.split(":", 1)[-1][:12]


class PullProgress:
    """Reports one pull. Every method is a no-op cost when nobody reads the stream."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        try:
            self.tty = bool(stream.isatty())
        except (AttributeError, ValueError):
            self.tty = False
        self._open_line = False  # a terminal line is being redrawn and has no newline yet

    def _write_line(self, text: str) -> None:
        if self._open_line:
            self.stream.write("\r\033[K")
            self._open_line = False
        self.stream.write(text + "\n")
        self.stream.flush()

    def _redraw(self, text: str) -> None:
        self.stream.write("\r\033[K" + text)
        self.stream.flush()
        self._open_line = True

    def note(self, text: str) -> None:
        self._write_line(text)

    def start(self, ref: str) -> None:
        r = parse_reference(ref)
        self._write_line(f"{r.tag or r.digest}: Pulling from {r.repository}")

    def layer(self, digest: str, status: str) -> None:
        line = f"{short_id(digest)}: {status}"
        if status in _TRANSIENT:
            if self.tty:
                self._redraw(line)
            return
        self._write_line(line)

    def downloading(self, digest: str, done: int, total: int) -> None:
        if not self.tty:
            return
        if total > 0:
            filled = min(BAR_WIDTH, done * BAR_WIDTH // total)
            bar = "=" * BAR_WIDTH if filled >= BAR_WIDTH else "=" * filled + ">" + " " * (BAR_WIDTH - filled - 1)
            amount = f"{human_size(done)}/{human_size(total)}"
        else:
            bar, amount = " " * BAR_WIDTH, human_size(done)
        self._redraw(f"{short_id(digest)}: Downloading [{bar}] {amount:>17}")

    def finish(self, digest: str | None, status: str) -> None:
        if digest:
            self._write_line(f"Digest: {digest}")
        self._write_line(f"Status: {status}")
