"""The VM side of a command forwarded from macOS. See spec section 16.4.

The Mac runs, over SSH::

    python -m xcodon_runtime.forwarded --expect-version V --cwd CWD --token T [--env NAME=VALUE ...] -- ARGV...

This changes to CWD, applies the settings, and runs the Linux CLI on ARGV as a
child. It exits with the child's exit code. While it runs, ``~/.xcrunner-vm/run/T.pid``
holds its pid, so the Mac side can signal it over a second SSH call when the Mac
side is interrupted. Lima's SSH config shares one connection across calls, so a
closed session does not end this process on its own. When this process does get
a new parent, because its own SSH connection ended, it stops the child too.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from xcodon_runtime import __version__
from xcodon_runtime.envdir import ENV_LAYER_DIR_ENV

EXIT_ERROR = 125
POLL_SECONDS = 0.5
KILL_AFTER_SECONDS = 5.0
_FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def run_dir() -> Path:
    return Path.home() / ".xcrunner-vm" / "run"


def default_layer_dir() -> Path:
    """Env folder layers on the VM disk: a shared Mac folder cannot hold an overlay upper layer."""
    return Path.home() / ".xcrunner-vm" / "env-layers"


def _parse(argv: list[str]) -> tuple[str | None, str | None, str | None, dict[str, str], list[str]]:
    expect: str | None = None
    cwd: str | None = None
    token: str | None = None
    env: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return expect, cwd, token, env, argv[i + 1:]
        if tok not in ("--expect-version", "--cwd", "--token", "--env"):
            raise ValueError(f"forwarded: unknown option {tok!r}")
        if i + 1 >= len(argv):
            raise ValueError(f"forwarded: {tok} needs a value")
        value = argv[i + 1]
        if tok == "--expect-version":
            expect = value
        elif tok == "--cwd":
            cwd = value
        elif tok == "--token":
            if not re.fullmatch(r"[0-9a-f]{8,64}", value):
                raise ValueError(f"forwarded: --token must be hex digits, got {value!r}")
            token = value
        else:
            name, sep, val = value.partition("=")
            if not sep or not name:
                raise ValueError(f"forwarded: --env needs NAME=VALUE, got {value!r}")
            env[name] = val
        i += 2
    raise ValueError("forwarded: missing -- before the xcrunner arguments")


def _run(cmd: list[str], env: dict[str, str], poll: float = POLL_SECONDS,
         kill_after: float = KILL_AFTER_SECONDS, pid_file: Path | None = None) -> int:
    """Run ``cmd`` and return its exit code, 128+N for a death by signal N."""
    parent = os.getppid()
    child = subprocess.Popen(cmd, env=env)
    if pid_file is not None:
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(f"{os.getpid()}\n")
        except OSError:
            pid_file = None  # the Mac side's interrupt then falls back to closing SSH

    def forward(signum, _frame) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = {}
    for sig in _FORWARDED_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, forward)
        except ValueError:
            pass  # not the main thread
    try:
        while True:
            try:
                code = child.wait(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                if os.getppid() == parent:
                    continue
                child.terminate()
                try:
                    code = child.wait(timeout=kill_after)
                except subprocess.TimeoutExpired:
                    child.kill()
                    code = child.wait()
                break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
    return 128 - code if code < 0 else code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        expect, cwd, token, passed, rest = _parse(list(argv))
    except ValueError as e:
        print(f"xcrunner: {e}", file=sys.stderr)
        return EXIT_ERROR
    if expect is not None and expect != __version__:
        print(f"xcrunner: the VM has xcrunner {__version__} and the Mac has {expect}; "
              "run `xcrunner machine start`", file=sys.stderr)
        return EXIT_ERROR
    if cwd is not None:
        try:
            os.chdir(cwd)
        except OSError:
            print(f"xcrunner: {cwd} is not a folder in the xcrunner VM; only folders shared from the Mac "
                  "exist there (see `xcrunner machine status`)", file=sys.stderr)
            return EXIT_ERROR
    env = dict(os.environ)
    env.update(passed)
    # Inside the VM the CLI must run locally, never forward again.
    env.pop("XCRUNNER_MACHINE_LINUX_TEST", None)
    env.setdefault(ENV_LAYER_DIR_ENV, str(default_layer_dir()))
    pid_file = run_dir() / f"{token}.pid" if token else None
    return _run([sys.executable, "-m", "xcodon_runtime.cli", *rest], env, pid_file=pid_file)


if __name__ == "__main__":
    sys.exit(main())
