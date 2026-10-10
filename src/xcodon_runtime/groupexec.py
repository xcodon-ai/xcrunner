"""Run a command in its own process group and pass signals to the whole group.

Usage: python -m xcodon_runtime.groupexec -- COMMAND...

udocker runs the container command as a child of its own Python process, so a
signal to udocker alone would leave that child running. Exit codes pass
through, and a death by signal N becomes 128+N.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def run(argv: list[str]) -> int:
    child = subprocess.Popen(argv, start_new_session=True)

    def forward(signum, _frame) -> None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    previous = {}
    for sig in _SIGNALS:
        try:
            previous[sig] = signal.signal(sig, forward)
        except ValueError:
            pass  # not the main thread
    try:
        code = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 128 - code if code < 0 else code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "--" or len(argv) < 2:
        print("usage: python -m xcodon_runtime.groupexec -- COMMAND...", file=sys.stderr)
        return 2
    return run(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
