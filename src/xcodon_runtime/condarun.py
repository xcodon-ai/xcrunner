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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import lookup_root, opt_value, resolve_root
from xcodon_runtime.home import RuntimeHome

_VALUE_OPTS = ("-n", "--name", "-p", "--prefix", "-r", "--root-prefix", "--cwd")
_IGNORED = frozenset({"--no-capture-output", "--live-stream", "-v", "--verbose", "--dev",
                      "--debug-wrapper-scripts", "-q", "--quiet", "--no-rc"})


class RunUsageError(ValueError):
    """A `conda run` command line xrunner cannot accept."""


@dataclass
class RunArgs:
    name: str | None = None
    prefix: str | None = None
    root_flag: str | None = None
    cwd: str | None = None
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
        if tok.startswith("--"):
            opt = tok.split("=", 1)[0]
        elif tok.startswith("-") and len(tok) > 2 and tok[:2] in ("-n", "-p", "-r"):
            opt = tok[:2]
        else:
            opt = tok
        if opt in _VALUE_OPTS:
            value, i = opt_value(tokens, i)
            if value is None:
                raise RunUsageError(f"{opt} needs a value")
            if opt in ("-n", "--name"):
                a.name = value
            elif opt in ("-p", "--prefix"):
                a.prefix = value
            elif opt in ("-r", "--root-prefix"):
                a.root_flag = value
            else:
                a.cwd = value
            continue
        if tok in _IGNORED:
            i += 1
            continue
        if tok.startswith("-"):
            raise RunUsageError(f"unknown option {tok}")
        break
    a.command = tokens[i:]
    return a


def activation_env(prefix: Path, label: str, environ: Mapping[str, str]) -> dict[str, str]:
    env = dict(environ)
    env["PATH"] = str(prefix / "bin") + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    env["CONDA_PREFIX"] = str(prefix)
    env["CONDA_DEFAULT_ENV"] = label
    env["CONDA_SHLVL"] = "1"
    return env


def run_main(argv: Sequence[str], home: RuntimeHome, cwd: Path, environ: Mapping[str, str],
             err: TextIO) -> int:
    try:
        a = parse_run_args(argv)
    except RunUsageError as e:
        print(f"conda run: {e}", file=err)
        return 2
    if not a.command:
        print("conda run: a command is required, for example: conda run -n NAME COMMAND", file=err)
        return 2
    root = resolve_root(cwd, environ, home, a.root_flag)
    if a.prefix:
        prefix = Path(a.prefix) if Path(a.prefix).is_absolute() else cwd / a.prefix
        label = str(prefix)
    elif a.name and a.name != "base":
        root = lookup_root(root, home, a.name)
        prefix = root.path / "envs" / a.name
        label = a.name
    else:
        prefix = root.path
        label = "base"
    if not (prefix / "conda-meta").is_dir():
        print(f"EnvironmentLocationNotFound: Not a conda environment: {prefix}", file=err)
        return 1
    env = activation_env(prefix, label, environ)
    workdir = cwd if a.cwd is None else (Path(a.cwd) if Path(a.cwd).is_absolute() else cwd / a.cwd)
    scripts = sorted((prefix / "etc" / "conda" / "activate.d").glob("*.sh"))
    if scripts:
        sources = "; ".join(f". {shlex.quote(str(s))}" for s in scripts)
        argv_exec = ["/bin/sh", "-c", f'{sources}; exec "$@"', "sh", *a.command]
    else:
        argv_exec = list(a.command)
    try:
        os.chdir(workdir)
    except OSError as e:
        print(f"conda run: cannot change to {workdir}: {e}", file=err)
        return 1
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(argv_exec[0], argv_exec, env)
    except FileNotFoundError:
        print(f"xrunner: {a.command[0]}: command not found", file=err)
        return 127
    except PermissionError:
        print(f"xrunner: {a.command[0]}: permission denied", file=err)
        return 126
    return 1  # not reached: execvpe only returns by raising
