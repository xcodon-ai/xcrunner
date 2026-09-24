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
EXIT_NO_CONTAINER = 125
EXIT_CANNOT_EXEC = 126
EXIT_NOT_FOUND = 127


def _resolve(command: str, path_value: str) -> tuple[str | None, int]:
    """Find the command on the container PATH.

    Returns the path to exec and 0, or None with the exit code to use: 126 when
    a matching file exists but cannot be executed, 127 when nothing matches.
    """
    candidates = [command] if "/" in command else [
        os.path.join(d or ".", command) for d in path_value.split(":")
    ]
    exists = False
    for candidate in candidates:
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate, 0
        if os.path.exists(candidate):
            exists = True
    return None, EXIT_CANNOT_EXEC if exists else EXIT_NOT_FOUND


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
        print("xrunner: container is not running", file=sys.stderr)
        return EXIT_NO_CONTAINER
    except OSError as e:
        print(f"xrunner: cannot open the container namespaces: {e}", file=sys.stderr)
        return EXIT_NO_CONTAINER
    try:
        for fd in fds:
            sc.setns(fd, 0)
            os.close(fd)
    except OSError as e:
        print(f"xrunner: cannot enter container: {e}", file=sys.stderr)
        return EXIT_NO_CONTAINER

    child = os.fork()
    if child == 0:
        try:
            sc.set_no_new_privs()
            # PDEATHSIG is cleared on execve only for setuid binaries;
            # no-new-privs above makes that moot, so this survives the exec.
            sc.set_parent_death_signal(signal.SIGKILL)
            os.makedirs(workdir, exist_ok=True)
            os.chdir(workdir)
            exe, code = _resolve(command[0], env.get("PATH", ""))
            if exe is None:
                why = "permission denied" if code == EXIT_CANNOT_EXEC else "not found"
                print(f"xrunner: exec: {command[0]}: {why}", file=sys.stderr)
                os._exit(code)
            os.execve(exe, command, env)
        except PermissionError as e:
            print(f"xrunner: exec: {command[0]}: {e.strerror}", file=sys.stderr)
            os._exit(EXIT_CANNOT_EXEC)
        except OSError as e:
            print(f"xrunner: exec: {command[0]}: {e}", file=sys.stderr)
            os._exit(EXIT_CANNOT_EXEC)

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
