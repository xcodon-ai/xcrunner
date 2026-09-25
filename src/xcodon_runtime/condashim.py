# src/xcodon_runtime/condashim.py
"""`xrunner conda`: conda's command line, answered by a pinned micromamba. See spec section 12."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import ensure_root, lookup_root, opt_value, resolve_root
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.micromamba import find_micromamba

PKGS_DIRNAME = "conda-pkgs"
PRIVATE_HOME = ".home"
EXPLICIT_NAME = "conda-explicit.txt"
DEFAULT_CHANNELS = ("conda-forge", "bioconda")
PASS_VERBS = frozenset({"create", "install", "update", "remove", "uninstall", "list", "search", "info",
                        "clean", "config", "env"})
ENV_SUBVERBS = frozenset({"list", "create", "export", "remove"})
REFUSED_VERBS = frozenset({"activate", "deactivate", "init", "shell"})
CONFIRM = frozenset({"create", "install", "update", "remove", "uninstall", "clean", "env create", "env remove"})
CHANNELS = frozenset({"create", "install", "update", "search", "env create"})
RECORD = frozenset({"create", "install", "update", "remove", "uninstall", "env create"})
DRY_RUN_FLAGS = frozenset({"-d", "--dry-run"})
LOOKUP = frozenset({"list", "env export", "remove", "uninstall", "install", "update", "env remove"})
NO_ROOT_FLAG = frozenset({"clean"})  # micromamba 2.9.0: "clean: The following arguments were not expected: -r"
_DROP = frozenset({"CONDARC", "MAMBARC", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL",
                   "MAMBA_ROOT_PREFIX", "CONDA_ENVS_PATH", "CONDA_ENVS_DIRS", "CONDA_PKGS_DIRS"})
# A combined short-flag token (`-yq`) sets `yes` when it has a `y` and none of these
# option letters, which take a value and so must not be read as bare boolean flags.
_SHORT_COMBO_RE = re.compile(r"^-[a-zA-Z]+$")
_VALUE_LETTERS = frozenset("npcrf")
CONFIG_NOT_SUPPORTED = (
    "conda: xrunner's conda only supports `config list`; channels default to conda-forge "
    "and bioconda, and -c adds more per command. Config changes are not supported.\n"
)

USAGE = """usage: conda COMMAND [OPTIONS]

This conda is xrunner's front end over micromamba. Supported commands:
  create, install, update, remove, uninstall, list, search, info, clean, config,
  env list|create|export|remove, run
Run a tool in an environment with:  conda run -n NAME COMMAND [ARGS...]
Environments live in the project's .xrunner-env/conda folder.
"""
ACTIVATE_MSG = ("conda {verb}: activation changes the calling shell, which xrunner's conda cannot do.\n"
                "Run a tool with `conda run -n NAME CMD`, or call it as <prefix>/bin/CMD.")
# Options micromamba accepts ahead of the verb (spec 12.5: `conda -r ROOT run ...`,
# `conda -q run ...`) that take a separate value, so the pre-verb scan below can
# skip past it without mistaking it for the verb.
_PRE_VERB_VALUE_OPTS = ("-r", "--root-prefix", "--rc-file")


def _split_before_run(argv: Sequence[str]) -> tuple[list[str], list[str]] | None:
    """If the first non-option token in argv is `run`, the option tokens (with
    their values) before it and the tokens after it; else None.

    `run` is otherwise dispatched only when it is argv[0], which rejects
    `conda -r ROOT run ...` and `conda -q run ...` even though options are
    allowed before the verb. `parse_run_args` already reads `-r`, `-q` and
    `--no-rc`, so those tokens are simply forwarded to it unchanged.
    """
    tokens = list(argv)
    before: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return (before, tokens[i + 1:]) if tok == "run" else None
        opt = tok.split("=", 1)[0] if tok.startswith("--") else (tok[:2] if len(tok) > 1 else tok)
        if opt in _PRE_VERB_VALUE_OPTS:
            j = i
            _, i = opt_value(tokens, i)
            before.extend(tokens[j:i])
        else:
            before.append(tok)
            i += 1
    return None


@dataclass
class Parsed:
    verb: str | None = None
    sub: str | None = None
    tokens: list[str] = field(default_factory=list)
    name: str | None = None
    prefix: str | None = None
    file: str | None = None
    root_flag: str | None = None
    yes: bool = False
    channels: list[str] = field(default_factory=list)
    override_channels: bool = False
    help: bool = False
    version: bool = False

    @property
    def key(self) -> str:
        return f"{self.verb} {self.sub}" if self.sub else (self.verb or "")


def parse_args(argv: Sequence[str]) -> Parsed:
    p = Parsed()
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # A long option's name stops at `=`; a short option's is just its two characters
        # (`-cbioconda`, `-r=/x`), so its own attached value is not mistaken for the flag.
        if tok.startswith("--"):
            opt = tok.split("=", 1)[0]
        elif tok.startswith("-") and len(tok) > 1:
            opt = tok[:2]
        else:
            opt = tok
        if p.verb is None and not tok.startswith("-"):
            p.verb = tok
            i += 1
            if tok == "env" and i < len(tokens) and not tokens[i].startswith("-"):
                p.sub = tokens[i]
                i += 1
            continue
        if opt in ("-r", "--root-prefix"):
            p.root_flag, i = opt_value(tokens, i)
            continue
        if opt in ("-n", "--name", "-p", "--prefix", "-c", "--channel", "-f", "--file"):
            value, j = opt_value(tokens, i)
            p.tokens.extend(tokens[i:j])
            if opt in ("-n", "--name"):
                p.name = value
            elif opt in ("-p", "--prefix"):
                p.prefix = value
            elif opt in ("-f", "--file"):
                p.file = value
            elif value is not None:
                p.channels.append(value)
            i = j
            continue
        if tok in ("-y", "--yes"):
            p.yes = True
        elif tok == "--override-channels":
            p.override_channels = True
        elif tok in ("-h", "--help"):
            p.help = True
        elif tok in ("-V", "--version") and p.verb is None:
            p.version = True
        elif len(tok) > 2 and _SHORT_COMBO_RE.match(tok) and not (_VALUE_LETTERS & set(tok[1:])):
            if "y" in tok[1:]:
                p.yes = True
        p.tokens.append(tok)
        i += 1
    return p


def micromamba_env(root: Path, home: RuntimeHome, environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in _DROP}
    private = root / PRIVATE_HOME
    env.update({
        "HOME": str(private),
        "XDG_CACHE_HOME": str(private / ".cache"),
        "XDG_CONFIG_HOME": str(private / ".config"),
        "MAMBA_ROOT_PREFIX": str(root),
        "CONDA_PKGS_DIRS": str(home.path / PKGS_DIRNAME),
    })
    return env


def micromamba_argv(mm: Path, p: Parsed, root: Path) -> list[str]:
    tokens = p.tokens
    # conda's `-d` means `--dry-run`; micromamba 2.9.0 only defines `--dry-run` (no
    # `-d`) and rejects a bare `-d` outright ("The following argument was not
    # expected: -d"), on exactly the verbs conda itself defines -d/--dry-run for --
    # which is also RECORD's verb list, the ones that would modify an env. Elsewhere
    # `-d` is left alone (it is not a recognized conda flag there either).
    if p.key in RECORD and "-d" in tokens:
        tokens = ["--dry-run" if t == "-d" else t for t in tokens]
    argv = [str(mm), p.verb or ""] + ([p.sub] if p.sub else []) + tokens + ["--no-rc"]
    if p.key not in NO_ROOT_FLAG:
        argv += ["-r", str(root)]
    if p.key in CONFIRM and not p.yes:
        argv.append("-y")
    if p.key in CHANNELS and not p.override_channels:
        for ch in DEFAULT_CHANNELS:
            if ch not in p.channels:
                argv += ["-c", ch]
    return argv


def _env_file_name(path: str, cwd: Path) -> str | None:
    """The top-level `name:` of an `env create -f FILE` environment file: a line
    starting at column 0 with `name:`, its value stripped of quotes and a trailing `#`
    comment. No YAML dependency; matches micromamba 2.9.0, which for `env create -f`
    reads only the file's `name:` -- a `prefix:` line (which `conda env export` also
    writes) is ignored, and with neither a name nor -n/-p it exits 1, "No target
    prefix specified"."""
    file_path = Path(path)
    file_path = file_path if file_path.is_absolute() else cwd / file_path
    try:
        text = file_path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("name:"):
            return line[len("name:"):].split("#", 1)[0].strip().strip("'\"")
    return None


def target_prefix(p: Parsed, root: Path, cwd: Path) -> Path:
    if p.prefix:
        pp = Path(p.prefix)
        return pp if pp.is_absolute() else cwd / pp
    if p.name and p.name != "base":
        return root / "envs" / p.name
    if p.key == "env create" and p.file:
        name = _env_file_name(p.file, cwd)
        if name and name != "base":
            return root / "envs" / name
    return root


def _write_record(mm: Path, prefix: Path, root: Path, env: dict[str, str], cwd: Path, err: TextIO) -> None:
    if not (prefix / "conda-meta").is_dir():
        print(f"xrunner: warning: could not record the packages of {prefix}: no environment there", file=err)
        return
    r = subprocess.run([str(mm), "env", "export", "--no-rc", "-r", str(root), "-p", str(prefix), "--explicit"],
                       env=env, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0 or "@EXPLICIT" not in r.stdout:
        print(f"xrunner: warning: could not record the packages of {prefix}: {r.stderr.strip()[:200]}", file=err)
        return
    try:
        tmp = prefix / (EXPLICIT_NAME + ".tmp")
        tmp.write_text(r.stdout)
        os.replace(tmp, prefix / EXPLICIT_NAME)
    except OSError as e:
        print(f"xrunner: warning: could not record the packages of {prefix}: {e}", file=err)


def _micromamba_version(mm: Path) -> str:
    r = subprocess.run([str(mm), "--version"], capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def conda_main(argv: Sequence[str], home: RuntimeHome, cwd: Path | None = None,
               environ: Mapping[str, str] | None = None, err: TextIO | None = None) -> int:
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    environ = dict(os.environ if environ is None else environ)
    err = err if err is not None else sys.stderr
    argv = list(argv)
    split = _split_before_run(argv)
    if split is not None:
        from xcodon_runtime.condarun import run_main

        before, after = split
        return run_main(before + after, home, cwd, environ, err)
    p = parse_args(argv)
    if p.version:
        print(f"conda {_micromamba_version(find_micromamba(home, environ))} (micromamba via xrunner)")
        return 0
    if p.verb is None:
        print(USAGE, end="", file=sys.stdout if p.help else err)
        return 0 if p.help else 2
    if p.verb in REFUSED_VERBS:
        print(ACTIVATE_MSG.format(verb=p.verb), file=err)
        return 1
    if p.verb == "env" and p.sub is None and p.help:
        # `conda env --help`/`-h`: no subcommand to validate against ENV_SUBVERBS, just
        # micromamba's own help for the `env` verb -- but it still needs a root and the
        # same isolated environment as every other micromamba call.
        mm = find_micromamba(home, environ)
        root = resolve_root(cwd, environ, home, p.root_flag)
        ensure_root(root.path)
        if root.source == "home":
            print(f"xrunner: no project env folder found; using {root.path}", file=err)
        env = micromamba_env(root.path, home, environ)
        return subprocess.run([str(mm), "env"] + p.tokens, env=env, cwd=cwd).returncode
    if p.verb not in PASS_VERBS or (p.verb == "env" and p.sub not in ENV_SUBVERBS):
        print(f"conda: '{p.key}' is not supported by xrunner's conda.\n{USAGE}", end="", file=err)
        return 2
    if p.verb == "config":
        # p.tokens can start with a pre-verb option (`conda -q config list` puts `-q`
        # in p.tokens too, ahead of `list`), so the sub-verb is the first token that
        # is not itself an option, not just p.tokens[0].
        sub = next((t for t in p.tokens if not t.startswith("-")), None)
        if sub != "list":
            print(CONFIG_NOT_SUPPORTED, end="", file=err)
            return 2
    mm = find_micromamba(home, environ)
    root = resolve_root(cwd, environ, home, p.root_flag)
    if p.name and p.name != "base" and p.key in LOOKUP:
        root = lookup_root(root, home, p.name)
    ensure_root(root.path)
    if root.source == "home":
        print(f"xrunner: no project env folder found; using {root.path}", file=err)
    env = micromamba_env(root.path, home, environ)
    code = subprocess.run(micromamba_argv(mm, p, root.path), env=env, cwd=cwd).returncode
    if code == 0 and p.key in RECORD and not (DRY_RUN_FLAGS & set(p.tokens)):
        _write_record(mm, target_prefix(p, root.path, cwd), root.path, env, cwd, err)
    return code
