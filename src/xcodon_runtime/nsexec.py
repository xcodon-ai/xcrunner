"""Enter a keeper's namespaces and exec a command there.

Run as:  python -m xcodon_runtime.nsexec PID WORKDIR ENV_JSON -- ARGV...

ENV_JSON is a file holding the container environment. It is read and deleted
before entering the namespaces. Exit code: the command's, or 125 when the
container is gone, 126 when the command cannot execute, 127 when not found.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from xcodon_runtime import syscalls as sc

NAMESPACES = ("user", "mnt", "pid", "uts")


def _which(command: str, path_value: str) -> str | None:
    if "/" in command:
        return command if os.access(command, os.X_OK) else None
    for d in path_value.split(":"):
        candidate = os.path.join(d or ".", command)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 4 or argv[3] != "--":
        print("usage: python -m xcodon_runtime.nsexec PID WORKDIR ENV_JSON -- ARGV...", file=sys.stderr)
        return 2
    pid, workdir, env_file, command = int(argv[0]), argv[1], Path(argv[2]), argv[4:]
    env = json.loads(env_file.read_text())
    env_file.unlink(missing_ok=True)

    # Open every namespace file first: after entering the mount namespace, /proc is the sandbox's.
    try:
        fds = [os.open(f"/proc/{pid}/ns/{ns}", os.O_RDONLY) for ns in NAMESPACES]
    except FileNotFoundError:
        print("xcodon: container is not running", file=sys.stderr)
        return 125
    try:
        for fd in fds:
            sc.setns(fd, 0)
            os.close(fd)
    except OSError as e:
        print(f"xcodon: cannot enter container: {e}", file=sys.stderr)
        return 125

    child = os.fork()
    if child == 0:
        try:
            sc.set_no_new_privs()
            os.makedirs(workdir, exist_ok=True)
            os.chdir(workdir)
            exe = _which(command[0], env.get("PATH", ""))
            if exe is None:
                print(f"xcodon: exec: {command[0]}: not found", file=sys.stderr)
                os._exit(127)
            os.execve(exe, command, env)
        except PermissionError as e:
            print(f"xcodon: exec: {command[0]}: {e.strerror}", file=sys.stderr)
            os._exit(126)
        except OSError as e:
            print(f"xcodon: exec: {command[0]}: {e}", file=sys.stderr)
            os._exit(126)

    def forward(signum, frame):
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, forward)
    while True:
        try:
            _, status = os.waitpid(child, 0)
            break
        except InterruptedError:
            continue
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
