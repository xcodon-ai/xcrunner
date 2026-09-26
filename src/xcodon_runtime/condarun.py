# src/xcodon_runtime/condarun.py
"""`xrunner conda run`: run a command inside a conda env. See spec section 12.5.

micromamba 2.9.0's own `run` fails on this host (`exec: --: invalid option` from
its wrapper script), so xrunner activates the env itself and replaces its own
process with the command. Arguments, stdin, stdout and the exit code are the
command's own.
"""

from __future__ import annotations

import os
import shlex
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import abs_prefix, ensure_root, lookup_root, opt_value, resolve_root, split_option
from xcodon_runtime.home import RuntimeHome

_VALUE_OPTS = ("-n", "--name", "-p", "--prefix", "-r", "--root-prefix", "--cwd")
_IGNORED = frozenset({"--no-capture-output", "--live-stream", "-v", "--verbose", "--dev",
                      "--debug-wrapper-scripts", "-q", "--quiet", "--no-rc", "--json"})
_HELP_OPTS = frozenset({"-h", "--help"})

RUN_USAGE = """usage: conda run [-n NAME | -p PATH] [-r ROOT] [--cwd DIR] COMMAND [ARG...]

Runs COMMAND inside the conda environment: xrunner activates the environment
itself and execs COMMAND, replacing this process. COMMAND's exit status,
stdout, stderr and stdin are used as-is.
"""


class RunUsageError(ValueError):
    """A `conda run` command line xrunner cannot accept."""


@dataclass
class RunArgs:
    name: str | None = None
    prefix: str | None = None
    root_flag: str | None = None
    cwd: str | None = None
    help: bool = False
    command: list[str] = field(default_factory=list)


def parse_run_args(argv: Sequence[str]) -> RunArgs:
    tokens = list(argv)
    a = RunArgs()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        # A long option's name stops at `=`; a short option's is just its two
        # characters (`-ntools`, `-n=foo`), so its own attached value is not
        # mistaken for the flag -- the same reading condashim.parse_args uses.
        opt, _ = split_option(tok, "npr")
        if opt in _VALUE_OPTS:
            value, i = opt_value(tokens, i)
            if value is None:
                raise RunUsageError(f"{opt} needs a value")
            if opt in ("-n", "--name"):
                if "/" in value or value in (".", ".."):
                    raise RunUsageError(f"invalid environment name '{value}'")
                a.name = value
            elif opt in ("-p", "--prefix"):
                a.prefix = value
            elif opt in ("-r", "--root-prefix"):
                a.root_flag = value
            else:
                a.cwd = value
            continue
        if tok in _HELP_OPTS:
            a.help = True
            i += 1
            continue
        if tok in _IGNORED:
            i += 1
            continue
        if tok.startswith("-"):
            raise RunUsageError(f"unknown option {tok}")
        break
    a.command = tokens[i:]
    if a.name and a.prefix:
        raise RunUsageError("use -n NAME or -p PATH, not both")
    return a


def activation_env(prefix: Path, label: str, environ: Mapping[str, str]) -> dict[str, str]:
    env = dict(environ)
    # A missing or empty PATH would leave the command only able to see the
    # env's own bin/, unable to find `sh` or any other ordinary tool.
    existing_path = env.get("PATH") or os.defpath
    env["PATH"] = str(prefix / "bin") + os.pathsep + existing_path
    env["CONDA_PREFIX"] = str(prefix)
    env["CONDA_DEFAULT_ENV"] = label
    env["CONDA_SHLVL"] = "1"
    return env


def activation_shell() -> str:
    """The shell that sources activate.d scripts: bash, as real conda on Linux
    uses (the scripts may rely on bash syntax), else /bin/sh."""
    if os.path.isfile("/bin/bash") and os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    return "/bin/sh"


def exec_argv(scripts: Sequence[Path], command: Sequence[str]) -> list[str]:
    """The argv to exec: the command itself, or, when the env has activate.d
    scripts, a shell that sources them and then execs the command."""
    if not scripts:
        return list(command)
    shell = activation_shell()
    sources = "; ".join(f". {shlex.quote(str(s))}" for s in scripts)
    return [shell, "-c", f'{sources}; exec "$@"', os.path.basename(shell), *command]


def run_main(argv: Sequence[str], home: RuntimeHome, cwd: Path, environ: Mapping[str, str],
             err: TextIO) -> int:
    try:
        a = parse_run_args(argv)
    except RunUsageError as e:
        print(f"conda run: {e}", file=err)
        return 2
    if a.help:
        print(RUN_USAGE, end="")
        return 0
    if not a.command:
        print("conda run: a command is required, for example: conda run -n NAME COMMAND", file=err)
        return 2
    root = resolve_root(cwd, environ, home, a.root_flag)
    if a.prefix:
        prefix = abs_prefix(a.prefix, cwd)
        label = str(prefix)
    elif a.name and a.name != "base":
        root = lookup_root(root, home, a.name)
        prefix = root.path / "envs" / a.name
        label = a.name
    else:
        # The base env, real conda's root itself: it always exists, so a fresh
        # root prefix (no `create` yet) is made into a valid one here, the same
        # way `ensure_root` does for every other conda verb.
        ensure_root(root.path)
        prefix = root.path
        label = "base"
    if not (prefix / "conda-meta").is_dir():
        print(f"EnvironmentLocationNotFound: Not a conda environment: {prefix}", file=err)
        return 1
    env = activation_env(prefix, label, environ)
    workdir = cwd if a.cwd is None else (Path(a.cwd) if Path(a.cwd).is_absolute() else cwd / a.cwd)
    argv_exec = exec_argv(sorted((prefix / "etc" / "conda" / "activate.d").glob("*.sh")), a.command)
    try:
        os.chdir(workdir)
    except OSError as e:
        print(f"conda run: cannot change to {workdir}: {e}", file=err)
        return 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Python sets SIGPIPE and SIGXFSZ to SIG_IGN at startup, and exec keeps
    # ignored dispositions, so an unpatched command inherits an ignored
    # SIGPIPE: `conda run ... | head` then gets a "Broken pipe" write error
    # and exits 1 instead of dying quietly with 141, breaking `pipefail`.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError, OSError):
        pass
    try:
        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    except (AttributeError, ValueError, OSError):
        pass
    try:
        os.execvpe(argv_exec[0], argv_exec, env)
    except FileNotFoundError:
        print(f"xrunner: {a.command[0]}: command not found", file=err)
        return 127
    except PermissionError:
        print(f"xrunner: {a.command[0]}: permission denied", file=err)
        return 126
    return 1  # not reached: execvpe only returns by raising
