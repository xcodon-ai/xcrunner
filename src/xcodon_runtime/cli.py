"""Docker-compatible command line over the Runtime API."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from io import StringIO

from xcodon_runtime import __version__
from xcodon_runtime.api import Runtime
from xcodon_runtime.engine import Bind
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.keeper import KEEPER_LOG
from xcodon_runtime.reference import parse_platform

log = logging.getLogger("xcodon")

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
    "--interactive": False, "--cidfile": True, "--pull": True,
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
            raise UsageError(f"unknown option {flag}; xcodon supports a docker subset (see xcodon run --help)")
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
        # -i / --interactive: stdin always passes through
    raise UsageError("no image given: usage: xcodon run [OPTIONS] IMAGE [COMMAND...]")


RUN_USAGE = ("xcodon {cmd} [--mount=... | -v HOST:CONTAINER[:ro]] [-w DIR] [-e K=V] [--entrypoint E] "
             "[-u USER] [--name N] [--rm] [-i] [--cidfile F] [--pull missing|always|never] IMAGE [COMMAND...]")

_GLOBAL_OPTIONS_WITH_VALUE = {"--engine", "--home"}


def _split_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split at a `run` or `create` subcommand. argparse's REMAINDER rejects leading options,
    and a global `-v` must not swallow a `-v` inside the container command."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok in ("run", "create"):
            return argv[: i + 1], argv[i + 1 :]
        return argv, None
    return argv, None


def _warn_ignored(opts: RunOptions) -> None:
    if opts.ignored:
        log.warning("ignoring unsupported docker options: %s", ", ".join(sorted(set(opts.ignored))))


# -- commands ------------------------------------------------------------------------


def cmd_pull(rt: Runtime, args) -> int:
    platform = parse_platform(args.platform) if args.platform else None
    img = rt.pull(args.image, platform)
    print(f"{args.image}: {img.short_id}")
    return 0


def cmd_inspect(rt: Runtime, args) -> int:
    doc = rt.images.inspect(args.image)
    print(json.dumps(doc, indent=2))
    return 0 if doc else 1


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
                  pull=opts.pull, cidfile=opts.cidfile)


def cmd_create(rt: Runtime, args) -> int:
    if args.rest[:1] in (["-h"], ["--help"]):
        print(RUN_USAGE.format(cmd="create"))
        return 0
    opts = parse_run_args(args.rest)
    _warn_ignored(opts)
    c = rt.create(opts.image, command=opts.command or None, entrypoint=opts.entrypoint, binds=opts.binds,
                  workdir=opts.workdir, env=opts.env, user=opts.user, name=opts.name, pull=opts.pull)
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
    for p in rt.prune():
        print(f"removed {p}")
    return 0


# -- parser --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xcodon", description="Rootless container runtime for Docker images.")
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
    s.add_argument("image")
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
    sub.add_parser("prune", help="remove leftovers and unreferenced layers").set_defaults(func=cmd_prune)
    return p


def _configure_logging(verbosity: int) -> None:
    env_level = os.environ.get("XCODON_LOG", "").lower()
    level = logging.WARNING
    if verbosity >= 2 or env_level == "debug":
        level = logging.DEBUG
    elif verbosity == 1 or env_level == "info":
        level = logging.INFO
    logging.basicConfig(level=level, format="xcodon: %(levelname)s %(name)s: %(message)s", stream=sys.stderr,
                        force=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    head, rest = _split_argv(list(sys.argv[1:] if argv is None else argv))
    try:
        args = parser.parse_args(head)
    except SystemExit as e:
        return int(e.code or 0)
    if rest is not None:
        args.rest = rest
    _configure_logging(args.verbose)
    try:
        rt = Runtime(args.home, engine=args.engine)
        return args.func(rt, args)
    except XcodonError as e:
        print(f"xcodon: {e}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
