"""Docker-compatible command line over the Runtime API."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from xcodon_runtime import __version__
from xcodon_runtime.api import Runtime
from xcodon_runtime.imagestore import env_to_dict
from xcodon_runtime.daemon import SHIM_MARKER, is_shim, resolve_docker
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.keeper import KEEPER_LOG
from xcodon_runtime.reference import parse_platform

log = logging.getLogger("xrunner")

EXIT_RUNTIME_ERROR = 125


class UsageError(XcodonError):
    """Bad command-line input."""


# Flags docker or cwltool may pass that we accept and ignore. True = takes a value.
IGNORED_FLAGS = {
    "--memory": True, "-m": True, "--memory-swap": True, "--cpus": True, "--cpu-shares": True,
    "--gpus": True, "--net": True, "--network": True, "--read-only": False, "--log-driver": True,
    "--userns": True, "--security-opt": True, "-t": False, "--tty": False, "--init": False,
    "--detach-keys": True, "--platform": True,
}
# Flags we honor. True = takes a value.
RUN_FLAGS = {
    "--mount": True, "-v": True, "--volume": True, "-w": True, "--workdir": True, "-e": True, "--env": True,
    "--entrypoint": True, "-u": True, "--user": True, "--name": True, "--rm": False, "-i": False,
    "--interactive": False, "--cidfile": True, "--pull": True, "--env-dir": True,
}


@dataclass
class RunOptions:
    image: str = ""
    command: list[str] = field(default_factory=list)
    binds: list[Bind] = field(default_factory=list)
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    entrypoint: list[str] | None = None
    user: str | None = None
    name: str | None = None
    rm: bool = False
    pull: str = "missing"
    cidfile: str | None = None
    env_dir: str | None = None
    ignored: list[str] = field(default_factory=list)


def _mount_flag(kv: dict[str, str], key: str) -> bool:
    """A --mount flag is on when it is bare or set to true. `readonly=false` is off."""
    if key not in kv:
        return False
    return kv[key] == "" or kv[key].lower() == "true"


def _parse_mount(value: str) -> Bind:
    fields = next(csv.reader(StringIO(value)))
    kv: dict[str, str] = {}
    for f in fields:
        k, _, v = f.partition("=")
        kv[k.strip()] = v.strip()
    if kv.get("type", "bind") != "bind":
        raise UsageError(f"--mount type {kv.get('type')!r} is not supported; only bind mounts")
    src = kv.get("source") or kv.get("src")
    dst = kv.get("target") or kv.get("destination") or kv.get("dst")
    if not src or not dst:
        raise UsageError(f"--mount needs source and target: {value}")
    readonly = _mount_flag(kv, "readonly") or _mount_flag(kv, "ro")
    return Bind(os.path.abspath(src), dst, readonly)


def _parse_volume(value: str) -> Bind:
    parts = value.split(":")
    if len(parts) < 2:
        raise UsageError(f"-v needs host:container[:ro|rw]: {value}")
    readonly = len(parts) > 2 and "ro" in parts[2].split(",")
    return Bind(os.path.abspath(parts[0]), parts[1], readonly)


def _parse_env_item(item: str) -> tuple[str, str]:
    """Split a `-e`/`--env` value. `-e K=V` sets V; `-e K` copies K from our own environment,
    as docker does. Shared by ``parse_run_args`` and ``cmd_exec`` so both agree."""
    k, sep, v = item.partition("=")
    return k, v if sep else os.environ.get(k, "")


def _write_cidfile(path: str, cid: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(cid)
    except OSError as e:
        raise XcodonError(f"cannot write cidfile {path}: {e}") from e


def parse_run_args(tokens: list[str]) -> RunOptions:
    """Hand-rolled so that everything after IMAGE is the command, as docker does."""
    opts = RunOptions()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or tok == "-":
            opts.image = tok
            opts.command = tokens[i + 1 :]
            return opts
        flag, has_eq, inline = tok.partition("=")
        table = RUN_FLAGS if flag in RUN_FLAGS else IGNORED_FLAGS if flag in IGNORED_FLAGS else None
        if table is None:
            raise UsageError(f"unknown option {flag}; xrunner supports a docker subset (see xrunner run --help)")
        takes_value = table[flag]
        value = None
        if takes_value:
            if has_eq:
                value = inline
            else:
                i += 1
                if i >= len(tokens):
                    raise UsageError(f"option {flag} needs a value")
                value = tokens[i]
        i += 1
        if table is IGNORED_FLAGS:
            opts.ignored.append(flag)
            continue
        if flag == "--mount":
            opts.binds.append(_parse_mount(value))
        elif flag in ("-v", "--volume"):
            opts.binds.append(_parse_volume(value))
        elif flag in ("-w", "--workdir"):
            opts.workdir = value
        elif flag in ("-e", "--env"):
            k, v = _parse_env_item(value)
            opts.env[k] = v
        elif flag == "--entrypoint":
            opts.entrypoint = [value] if value else []
        elif flag in ("-u", "--user"):
            opts.user = value
        elif flag == "--name":
            opts.name = value
        elif flag == "--rm":
            opts.rm = True
        elif flag == "--cidfile":
            opts.cidfile = value
        elif flag == "--pull":
            if value not in ("missing", "always", "never"):
                raise UsageError("--pull must be missing, always, or never")
            opts.pull = value
        elif flag == "--env-dir":
            if not value:
                raise UsageError("--env-dir needs a directory, not an empty value")
            opts.env_dir = os.path.abspath(os.path.expanduser(value))
        # -i / --interactive: stdin always passes through
    raise UsageError("no image given: usage: xrunner run [OPTIONS] IMAGE [COMMAND...]")


RUN_USAGE = ("xrunner {cmd} [--mount=... | -v HOST:CONTAINER[:ro]] [-w DIR] [-e K=V] [--entrypoint E] "
             "[-u USER] [--name N] [--rm] [-i] [--cidfile F] [--pull missing|always|never] [--env-dir DIR] IMAGE [COMMAND...]")

_GLOBAL_OPTIONS_WITH_VALUE = {"--engine", "--home"}


def _split_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split at a `run` or `create` subcommand. argparse's REMAINDER rejects leading options,
    and a global `-v` must not swallow a `-v` inside the container command.

    `docker` is transparent: `docker run`/`docker create` split the same way, one token
    later. Any other verb after `docker` is left whole (`(argv, None)`) for `main` to
    carve up itself, since a docker verb's own flags (`docker image inspect --format ...`)
    must not be mistaken for xrunner's global options either.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok == "docker":
            if i + 1 < len(argv) and argv[i + 1] in ("run", "create"):
                return argv[: i + 2], argv[i + 2 :]
            return argv, None
        if tok in ("run", "create"):
            return argv[: i + 1], argv[i + 1 :]
        return argv, None
    return argv, None


def _split_at_docker(head: list[str]) -> tuple[list[str], list[str]] | None:
    """Where in `head` (already split by `_split_argv`) the `docker` verb sits, skipping
    global options the same way `_split_argv` does. None when `head` is not a docker
    invocation at all. Used by `main` to carve the docker verb's own argv (build flags,
    image inspect flags, ...) away from xrunner's own argparse parser."""
    i = 0
    while i < len(head):
        tok = head[i]
        if tok in _GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok == "docker":
            return head[: i + 1], head[i + 1 :]
        return None
    return None


# SHIM_NAME is the file `xrunner shim install` writes: a `docker` that forwards to
# `xrunner docker`, so tools that shell out to a real docker binary (build, image
# inspect, ...) work on a host that has no docker at all.
SHIM_NAME = "docker"

# Maps a docker verb to the xrunner verb (as argv) that implements it.
DOCKER_VERBS = {
    "build": ["build"], "pull": ["pull"], "images": ["images"], "rmi": ["rmi"], "tag": ["tag"],
    "run": ["run"], "create": ["create"], "start": ["start"], "exec": ["exec"], "stop": ["stop"],
    "rm": ["rm"], "ps": ["ps"], "logs": ["logs"], "commit": ["commit"], "inspect": ["inspect"],
    "version": ["info"], "info": ["info"],
}
# `docker image <verb>` has its own, smaller, vocabulary.
DOCKER_IMAGE_VERBS = {"inspect": ["inspect"], "ls": ["images"], "list": ["images"], "rm": ["rmi"], "remove": ["rmi"]}
# `docker ... inspect --type ...` disambiguates container vs image; xrunner's inspect is
# always an image, so that flag (and its `=value` form) is dropped. `--format`/`-f` is
# NOT dropped here: cmd_inspect understands the exact `{{.Id}}` format itself (see below).
_INSPECT_DROP = {"--type"}


def translate_docker_argv(argv: list[str]) -> list[str]:
    """Map a docker CLI invocation's argv (verb + its own args, no leading `docker`) to
    xrunner's. Raises UsageError for a verb xrunner does not offer."""
    if not argv:
        raise UsageError("docker: a verb is required (build, image inspect, run, ...)")
    verb, rest = argv[0], list(argv[1:])
    if verb == "image":
        if not rest or rest[0] not in DOCKER_IMAGE_VERBS:
            sub_verb = rest[0] if rest else ""
            raise UsageError(f"docker image {sub_verb}: not supported by xrunner")
        head = DOCKER_IMAGE_VERBS[rest[0]]
        rest = rest[1:]
        verb = "inspect" if head == ["inspect"] else head[0]
    elif verb in DOCKER_VERBS:
        head = DOCKER_VERBS[verb]
    else:
        raise UsageError(f"docker {verb}: not supported by xrunner")
    if verb == "inspect":
        cleaned = []
        skip = False
        for tok in rest:
            if skip:
                skip = False
                continue
            if tok in _INSPECT_DROP:
                skip = True
                continue
            if tok.startswith("--type="):
                continue
            cleaned.append(tok)
        rest = cleaned
    return head + rest


def _warn_ignored(opts: RunOptions) -> None:
    if opts.ignored:
        log.warning("ignoring unsupported docker options: %s", ", ".join(sorted(set(opts.ignored))))


# -- commands ------------------------------------------------------------------------


def cmd_pull(rt: Runtime, args) -> int:
    platform = parse_platform(args.platform) if args.platform else None
    img = rt.pull(args.image, platform)
    print(f"{args.image}: {img.short_id}")
    return 0


# docker's `--format '{{.Id}}'`, whitespace inside the braces allowed. Any other
# --format value is accepted (docker itself supports a whole template language we do
# not) but ignored: we print the full JSON and warn instead of failing outright.
_ID_FORMAT_RE = re.compile(r"^\{\{\s*\.Id\s*\}\}$")


def cmd_inspect(rt: Runtime, args) -> int:
    refs = args.image if isinstance(args.image, list) else [args.image]
    docs: list[dict] = []
    missing: list[str] = []
    for ref in refs:
        doc = rt.images.inspect(ref)
        if doc:
            docs.extend(doc)
        else:
            missing.append(ref)
    fmt = getattr(args, "format", None)
    if fmt and _ID_FORMAT_RE.match(fmt.strip()):
        for d in docs:
            print(d["Id"])
    else:
        if fmt:
            log.warning("ignoring unsupported --format %r; printing the full JSON instead", fmt)
        print(json.dumps(docs, indent=2))
    for ref in missing:
        print(f"Error: No such image: {ref}", file=sys.stderr)
    return 1 if missing else 0


def cmd_images(rt: Runtime, args) -> int:
    print(f"{'REPOSITORY:TAG':<60} {'IMAGE ID':<14} SOURCE")
    for img in rt.list_images():
        source = json.loads((img.dir / "manifest.json").read_text()).get("source", "")
        for ref in img.refs or ["<none>"]:
            print(f"{ref:<60} {img.short_id:<14} {source}")
    return 0


def cmd_rmi(rt: Runtime, args) -> int:
    rt.remove_image(args.image)
    return 0


def cmd_run(rt: Runtime, args) -> int:
    if args.rest[:1] in (["-h"], ["--help"]):
        print(RUN_USAGE.format(cmd="run"))
        return 0
    opts = parse_run_args(args.rest)
    _warn_ignored(opts)
    return rt.run(opts.image, command=opts.command or None, entrypoint=opts.entrypoint, binds=opts.binds,
                  workdir=opts.workdir, env=opts.env, user=opts.user, name=opts.name, rm=opts.rm,
                  pull=opts.pull, cidfile=opts.cidfile, env_dir=opts.env_dir)


def cmd_create(rt: Runtime, args) -> int:
    if args.rest[:1] in (["-h"], ["--help"]):
        print(RUN_USAGE.format(cmd="create"))
        return 0
    opts = parse_run_args(args.rest)
    _warn_ignored(opts)
    c = rt.create(opts.image, command=opts.command or None, entrypoint=opts.entrypoint, binds=opts.binds,
                  workdir=opts.workdir, env=opts.env, user=opts.user, name=opts.name, pull=opts.pull,
                  env_dir=opts.env_dir)
    if opts.cidfile:
        _write_cidfile(opts.cidfile, c.id)
    print(c.id)
    return 0


def cmd_start(rt: Runtime, args) -> int:
    rt.start(rt.get_container(args.container))
    return 0


def cmd_exec(rt: Runtime, args) -> int:
    env = {}
    for item in args.env or []:
        k, v = _parse_env_item(item)
        env[k] = v
    c = rt.get_container(args.container)
    p = rt.popen(c, args.command or None, workdir=args.workdir, env=env)
    return p.wait()


def cmd_stop(rt: Runtime, args) -> int:
    rt.stop(rt.get_container(args.container))
    return 0


def cmd_rm(rt: Runtime, args) -> int:
    rt.remove(rt.get_container(args.container), force=args.force)
    return 0


def cmd_ps(rt: Runtime, args) -> int:
    print(f"{'CONTAINER ID':<14} {'IMAGE':<40} {'ENGINE':<6} {'STATE':<8} NAME")
    for c in rt.containers(all=args.all):
        print(f"{c.short_id:<14} {c.image_ref:<40} {c.engine:<6} {c.state:<8} {c.name or ''}")
    return 0


def cmd_logs(rt: Runtime, args) -> int:
    c = rt.get_container(args.container)
    if c.engine == "proot":
        print("no keeper log: proot engine", file=sys.stderr)
        return 0
    path = c.dir / KEEPER_LOG
    if path.exists():
        sys.stdout.write(path.read_text(errors="replace"))
    return 0


def cmd_info(rt: Runtime, args) -> int:
    print(json.dumps(rt.info(), indent=2))
    return 0


def cmd_prune(rt: Runtime, args) -> int:
    for p in rt.prune(all=args.all):
        print(f"removed {p}")
    return 0


def cmd_build(rt: Runtime, args) -> int:
    build_args = {}
    for item in args.build_arg or []:
        k, has_eq, v = item.partition("=")
        build_args[k] = v if has_eq else os.environ.get(k, "")
    out = (lambda line: None) if args.quiet else (lambda line: print(line, flush=True))
    img = rt.build(args.context, dockerfile=args.file, tags=args.tag or [], build_args=build_args,
                   no_cache=args.no_cache, out=out)
    if args.quiet:
        print(f"sha256:{img.id}")
    return 0


def _parse_change(spec: str, scope: dict[str, str]) -> dict:
    from xcodon_runtime.build import DEFAULT_SHELL, parse_command, parse_env

    name, _, rest = spec.strip().partition(" ")
    name = name.upper()
    if name == "ENV":
        return {"Env": [f"{k}={v}" for k, v in parse_env(rest, scope).items()]}
    if name == "LABEL":
        return {"Labels": parse_env(rest, scope)}
    if name == "WORKDIR":
        return {"WorkingDir": rest.strip()}
    if name == "USER":
        return {"User": rest.strip()}
    if name in ("CMD", "ENTRYPOINT"):
        return {"Cmd" if name == "CMD" else "Entrypoint": parse_command(rest, DEFAULT_SHELL)}
    if name == "SHELL":
        return {"Shell": parse_command(rest, DEFAULT_SHELL)}
    raise UsageError(f"unsupported --change {spec!r}; use ENV, LABEL, WORKDIR, USER, CMD, ENTRYPOINT, or SHELL")


def cmd_commit(rt: Runtime, args) -> int:
    # `commit --env-dir D --image I TAG` has only one positional: argparse fills the
    # first declared positional (container) with it, leaving tag None. Shift it over.
    if args.env_dir and args.tag is None:
        args.tag, args.container = args.container, None
    container = None
    if args.env_dir:
        if not args.image:
            raise UsageError("commit --env-dir also needs --image IMAGE")
        base = rt.images.require(args.image)
    else:
        if not args.container:
            raise UsageError("commit needs a CONTAINER, or --env-dir DIR --image IMAGE")
        container = rt.get_container(args.container)
        base = rt.images.require(container.image_id)
    # A mutable copy: each `-c ENV ...` change updates it, so a later `-c` sees the
    # variables an earlier one just set (`-c 'ENV A=1' -c 'ENV B=$A'` gives B=1), not
    # just the base image's own Env.
    # The image's own Env, for expanding `$VAR` in a `commit -c` change.
    scope = env_to_dict(base.config.get("config", {}).get("Env"))
    changes: dict = {}
    for spec in args.change or []:
        for k, v in _parse_change(spec, scope).items():
            if k == "Env":
                changes.setdefault("Env", []).extend(v)
                scope.update(env_to_dict(v))
            elif k == "Labels":
                changes.setdefault("Labels", {}).update(v)
            else:
                changes[k] = v
    if args.env_dir:
        img = rt.commit(None, args.tag, env_dir=os.path.abspath(os.path.expanduser(args.env_dir)),
                         image=args.image, changes=changes, message=args.message or "")
    else:
        img = rt.commit(container, args.tag, changes=changes, message=args.message or "")
    print(f"sha256:{img.id}")
    return 0


def cmd_tag(rt: Runtime, args) -> int:
    rt.images.tag(args.source, args.target)
    return 0


def cmd_docker(rt: Runtime, args) -> int:
    translated = translate_docker_argv(list(args.rest))
    return main(translated, _runtime=rt)


def cmd_shim(rt: Runtime, args) -> int:
    target_dir = Path(args.dir or os.path.dirname(sys.executable)).expanduser().resolve()
    shim_path = target_dir / SHIM_NAME
    if not args.force:
        # Anywhere on PATH: reuses daemon.py's own shim-aware resolver, so a stale shim
        # earlier in PATH cannot hide a real docker installed further along it.
        real_on_path = resolve_docker(SHIM_NAME)
        if real_on_path:
            raise UsageError(f"a real docker is on PATH at {real_on_path}; pass --force to install the shim anyway")
        # DIR itself, even when DIR is not on PATH at all: `os.path.lexists` so a
        # symlink is detected without following it, and `is_shim` decides purely from
        # the 512-byte marker check, never from where a symlink points.
        if os.path.lexists(shim_path) and not is_shim(str(shim_path)):
            raise UsageError(f"{shim_path} already exists and is not an xrunner shim; "
                              f"pass --force to overwrite it")
    xrunner = os.path.join(os.path.dirname(sys.executable), "xrunner")
    if not os.access(xrunner, os.X_OK):
        xrunner = shutil.which("xrunner") or "xrunner"
    target_dir.mkdir(parents=True, exist_ok=True)
    script = f"#!/bin/sh\n{SHIM_MARKER}\nexec {shlex.quote(xrunner)} docker \"$@\"\n"
    # Write to a temp file in the same directory, then `os.replace` it onto the final
    # name: that swaps the directory entry atomically, so an existing symlink at
    # `shim_path` is replaced rather than opened and written through (which would
    # instead overwrite whatever real docker binary it points at).
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".docker-shim-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        os.chmod(tmp_name, 0o755)
        os.replace(tmp_name, shim_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    print(f"installed {shim_path}")
    if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f'add it to PATH: export PATH="{target_dir}:$PATH"')
    return 0


# -- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xrunner", description="Rootless container runtime for Docker images.")
    p.add_argument("--version", action="version", version=f"xcodon-runtime {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for info, -vv for debug")
    p.add_argument("--engine", choices=("ns", "proot"), help="force an engine (default: probe the host)")
    p.add_argument("--home", help="runtime home (default: $XCODON_RUNTIME_HOME or ~/.xcodon/runtime)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pull", help="fetch an image into the local store")
    s.add_argument("image")
    s.add_argument("--platform")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("inspect", help="print image metadata as JSON")
    s.add_argument("--format", "-f", help="only \"{{.Id}}\" is understood; anything else is ignored")
    s.add_argument("image", nargs="+")
    s.set_defaults(func=cmd_inspect)

    sub.add_parser("images", help="list stored images").set_defaults(func=cmd_images)

    s = sub.add_parser("rmi", help="remove an image reference")
    s.add_argument("image")
    s.set_defaults(func=cmd_rmi)

    for name, func, help_text in (("run", cmd_run, "create, start, and run a command"),
                                  ("create", cmd_create, "create a container and print its id")):
        # Docker-style options are parsed by parse_run_args, not argparse: main() splits
        # argv at the subcommand and hands everything after it over untouched.
        s = sub.add_parser(name, help=help_text, add_help=False, usage=RUN_USAGE.format(cmd=name))
        s.set_defaults(func=func, rest=[])

    s = sub.add_parser("start", help="start a container's keeper")
    s.add_argument("container")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("exec", help="run a command in a running container")
    s.add_argument("-w", "--workdir")
    s.add_argument("-e", "--env", action="append")
    s.add_argument("container")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("stop", help="stop a container")
    s.add_argument("container")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("rm", help="remove a container")
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument("container")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("ps", help="list containers")
    s.add_argument("-a", "--all", action="store_true")
    s.set_defaults(func=cmd_ps)

    s = sub.add_parser("logs", help="print the keeper log")
    s.add_argument("container")
    s.set_defaults(func=cmd_logs)

    sub.add_parser("info", help="engine choice, probe results, paths").set_defaults(func=cmd_info)
    s = sub.add_parser("prune", help="remove leftovers; -a also removes unused layers and old containers")
    s.add_argument("-a", "--all", action="store_true",
                   help="also remove unreferenced layers and exited containers older than a day")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("build", help="build an image from a Dockerfile subset")
    s.add_argument("-t", "--tag", action="append")
    s.add_argument("-f", "--file")
    s.add_argument("--build-arg", action="append")
    s.add_argument("--no-cache", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    # Flags docker build accepts that xrunner ignores. Booleans take no value; --rm/
    # --force-rm/--pull must NOT be nargs="?", or `build --rm CTX` would swallow CTX
    # as --rm's own optional argument instead of leaving it for the context positional.
    s.add_argument("--rm", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--force-rm", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--pull", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--progress", help=argparse.SUPPRESS)
    s.add_argument("--platform", help=argparse.SUPPRESS)
    s.add_argument("--network", help=argparse.SUPPRESS)
    s.add_argument("--label", action="append", help=argparse.SUPPRESS)
    s.add_argument("context")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("commit", help="snapshot a stopped container or an env folder layer as an image")
    s.add_argument("-m", "--message")
    s.add_argument("-c", "--change", action="append")
    s.add_argument("--env-dir")
    s.add_argument("--image")
    s.add_argument("container", nargs="?")
    s.add_argument("tag", nargs="?")
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("tag", help="add a tag to an image")
    s.add_argument("source")
    s.add_argument("target")
    s.set_defaults(func=cmd_tag)

    s = sub.add_parser("image", help="docker-style image commands")
    isub = s.add_subparsers(dest="image_cmd", required=True)
    i = isub.add_parser("inspect")
    i.add_argument("--format", "-f", help="only \"{{.Id}}\" is understood; anything else is ignored")
    i.add_argument("image", nargs="+")
    i.set_defaults(func=cmd_inspect)
    isub.add_parser("ls").set_defaults(func=cmd_images)
    i = isub.add_parser("rm")
    i.add_argument("image")
    i.set_defaults(func=cmd_rmi)

    s = sub.add_parser("docker", help="accept docker verbs (build, image inspect, run, ...)", add_help=False)
    s.set_defaults(func=cmd_docker, rest=[])

    s = sub.add_parser("shim", help="install a docker command that forwards to xrunner")
    ssub = s.add_subparsers(dest="shim_cmd", required=True)
    i = ssub.add_parser("install", help="write a docker script that forwards to `xrunner docker`")
    i.add_argument("--dir", help="where to write it (default: beside the xrunner executable)")
    i.add_argument("--force", action="store_true", help="overwrite even if a real docker is already on PATH")
    i.set_defaults(func=cmd_shim)
    return p


def _configure_logging(verbosity: int) -> None:
    env_level = os.environ.get("XCODON_LOG", "").lower()
    level = logging.WARNING
    if verbosity >= 2 or env_level == "debug":
        level = logging.DEBUG
    elif verbosity == 1 or env_level == "info":
        level = logging.INFO
    logging.basicConfig(level=level, format="xrunner: %(levelname)s %(name)s: %(message)s", stream=sys.stderr,
                        force=True)


def main(argv: list[str] | None = None, _runtime: Runtime | None = None) -> int:
    """`_runtime` lets `cmd_docker` re-enter `main` with the same Runtime (and so the
    same --engine/--home) after translating a docker invocation to an xrunner one."""
    parser = build_parser()
    head, rest = _split_argv(list(sys.argv[1:] if argv is None else argv))
    # `docker ...` (any verb but run/create) comes back from `_split_argv` as (argv,
    # None): pull the docker verb's own argv out here, before argparse ever sees it,
    # the same way run/create's are carved out above.
    docker_split = _split_at_docker(head)
    if docker_split is not None:
        prefix, after = docker_split
        docker_rest = after + (rest or [])
        head = prefix
        rest = None
    try:
        args = parser.parse_args(head)
    except SystemExit as e:
        return int(e.code or 0)
    if docker_split is not None:
        args.rest = docker_rest
    elif rest is not None:
        args.rest = rest
    if _runtime is None:
        # Only the outer call configures logging: the re-entrant call from cmd_docker
        # parses a translated argv with no `-v`/`-vv` of its own, and would otherwise
        # silently reset verbosity back to the default on every `xrunner docker ...`.
        _configure_logging(args.verbose)
    try:
        rt = _runtime if _runtime is not None else Runtime(args.home, engine=args.engine)
        return args.func(rt, args)
    except XcodonError as e:
        print(f"xrunner: {e}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
