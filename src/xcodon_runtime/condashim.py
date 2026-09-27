# src/xcodon_runtime/condashim.py
"""`xrunner conda`: conda's command line, answered by a pinned micromamba. See spec section 12."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from xcodon_runtime.condaroot import abs_prefix, ensure_root, lookup_root, opt_value, resolve_root, split_option
from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.micromamba import find_micromamba, host_subdir

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
_VALUE_LETTERS = "npcrf"
_MUST_HAVE_VALUE = frozenset({"-r", "--root-prefix", "-n", "--name", "-p", "--prefix"})
# `remove --all`/`-a` (and `uninstall`) empties the env; like `env remove`, it
# leaves nothing to record, and a record left from before would be stale.
REMOVE_ALL_VERBS = frozenset({"remove", "uninstall"})
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
_PRE_VERB_VALUE_OPTS = ("-r", "--root-prefix")


# Some bioconda tools have no macOS ARM build. Micromamba's output streams
# straight through, so the shim does not read it; it only adds this line after a
# failed install on osx-arm64 (spec 16.6).
OSX_ARM_HINT = ("xrunner: hint: if a package was not found, it may have no macOS ARM build; "
                "`--platform osx-64` installs x86_64 builds that run under Rosetta 2")


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
        opt, _ = split_option(tok, "r")
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
    remove_all: bool = False
    missing_value: str | None = None  # the first -r/-n/-p given without a value

    @property
    def key(self) -> str:
        return f"{self.verb} {self.sub}" if self.sub else (self.verb or "")


def _with_abs_prefix(tokens: list[str], value: str) -> list[str]:
    """The -p/--prefix tokens ``tokens``, in whichever form they were given, with
    ``value`` (the absolute path) in place of the value they carried."""
    if len(tokens) == 2:
        return [tokens[0], value]
    opt, _ = split_option(tokens[0], "p")
    return [f"{opt}={value}" if opt.startswith("--") else opt + value]


def parse_args(argv: Sequence[str], cwd: Path | None = None) -> Parsed:
    """Read conda's command line. With ``cwd``, a relative -p/--prefix value is
    made absolute from it, in p.prefix and in the tokens micromamba gets
    (spec 12.3): micromamba 2.9.0 would read one with no `/` as an env name."""
    p = Parsed()
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # A long option's name stops at `=`; a short option's is just its two characters
        # (`-cbioconda`, `-r=/x`), so its own attached value is not mistaken for the flag.
        opt, _ = split_option(tok, _VALUE_LETTERS)
        if p.verb is None and not tok.startswith("-"):
            p.verb = tok
            i += 1
            if tok == "env" and i < len(tokens) and not tokens[i].startswith("-"):
                p.sub = tokens[i]
                i += 1
            continue
        if opt in ("-r", "--root-prefix"):
            p.root_flag, i = opt_value(tokens, i)
            if p.root_flag is None and p.missing_value is None:
                p.missing_value = opt
            continue
        if opt in ("-n", "--name", "-p", "--prefix", "-c", "--channel", "-f", "--file"):
            value, j = opt_value(tokens, i)
            if value is None and opt in _MUST_HAVE_VALUE and p.missing_value is None:
                p.missing_value = opt
            if value and cwd is not None and opt in ("-p", "--prefix"):
                value = str(abs_prefix(value, cwd))
                p.tokens.extend(_with_abs_prefix(tokens[i:j], value))
            else:
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
        elif tok in ("-a", "--all"):
            p.remove_all = True
        elif len(tok) > 2 and _SHORT_COMBO_RE.match(tok) and not (set(_VALUE_LETTERS) & set(tok[1:])):
            if "y" in tok[1:]:
                p.yes = True
            if "a" in tok[1:]:
                p.remove_all = True
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
        return abs_prefix(p.prefix, cwd)
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
        fd, tmp = tempfile.mkstemp(dir=prefix, prefix=".conda-explicit-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(r.stdout)
            os.chmod(tmp, 0o644)
            os.replace(tmp, prefix / EXPLICIT_NAME)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"xrunner: warning: could not record the packages of {prefix}: {e}", file=err)


def _drop_record(prefix: Path, err: TextIO) -> None:
    """After `remove --all` or `env remove`: the env holds no packages, so any
    record left in it would be stale."""
    try:
        (prefix / EXPLICIT_NAME).unlink(missing_ok=True)
    except OSError as e:
        print(f"xrunner: warning: could not remove {prefix / EXPLICIT_NAME}: {e}", file=err)


def _warn_home_root(root: Path, err: TextIO) -> None:
    print(f"xrunner: no project env folder found; using {root} "
          "(set XRUNNER_ENV_DIR or create .xrunner-env in the project)", file=err)


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
    p = parse_args(argv, cwd)
    if p.missing_value is not None:
        print(f"conda: {p.missing_value} needs a value", file=err)
        return 2
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
            _warn_home_root(root.path, err)
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
        _warn_home_root(root.path, err)
    env = micromamba_env(root.path, home, environ)
    code = subprocess.run(micromamba_argv(mm, p, root.path), env=env, cwd=cwd).returncode
    if code != 0 and p.verb in ("create", "install", "update") and host_subdir() == "osx-arm64":
        print(OSX_ARM_HINT, file=err)
    if code != 0 or DRY_RUN_FLAGS & set(p.tokens):
        return code
    is_drop = p.key == "env remove" or (p.key in REMOVE_ALL_VERBS and p.remove_all)
    if is_drop:
        _drop_record(target_prefix(p, root.path, cwd), err)
    elif p.key in RECORD:
        _write_record(mm, target_prefix(p, root.path, cwd), root.path, env, cwd, err)
    if (is_drop or p.key in RECORD) and root.source in ("env", "project"):
        try:
            from xcodon_runtime.envrecord import record_conda

            record_conda(root.path.parent)
        except Exception as e:  # noqa: BLE001 - recording must never change the exit code
            print(f"xrunner: warning: could not update the environment record: {e}", file=err)
    return code
