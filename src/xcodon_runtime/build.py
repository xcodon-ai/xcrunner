"""Run a Dockerfile subset in xrunner containers. See spec section 11.3."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.imagestore import Image

if TYPE_CHECKING:
    from xcodon_runtime.api import Runtime

log = logging.getLogger(__name__)
CACHE_FILE = "build-cache.json"
IGNORED = {"EXPOSE", "VOLUME", "HEALTHCHECK", "STOPSIGNAL", "MAINTAINER", "ONBUILD"}
DEFAULT_SHELL = ["/bin/sh", "-c"]
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass
class Instruction:
    name: str
    args: str
    line: int


def parse_dockerfile(text: str) -> list[Instruction]:
    """Split a Dockerfile into instructions, joining backslash continuation lines.

    Blank lines and full-line comments (a ``#`` as the first non-space
    character) are skipped outside of a continuation; a ``#`` elsewhere on a
    line is kept as part of the instruction's arguments, matching docker.
    """
    out: list[Instruction] = []
    buf: list[str] = []
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not buf:
            start = n
        if line.endswith("\\"):
            buf.append(line[:-1].rstrip() + " ")
            continue
        buf.append(line)
        full = "".join(buf).strip()
        buf = []
        name, _, args = full.partition(" ")
        out.append(Instruction(name.upper(), args.strip(), start))
    if buf:
        full = "".join(buf).strip()
        name, _, args = full.partition(" ")
        out.append(Instruction(name.upper(), args.strip(), start))
    return out


def expand_args(text: str, args: Mapping[str, str]) -> str:
    """Expand ``$NAME``, ``${NAME}``, and ``${NAME:-default}`` against ``args``."""

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2)
        if name in args:
            return args[name]
        return default if default is not None else ""

    return _VAR.sub(sub, text)


def parse_command(args: str, shell: list[str]) -> list[str]:
    """Parse a CMD/ENTRYPOINT/RUN/SHELL argument: JSON exec form, or a plain shell string."""
    s = args.strip()
    if s.startswith("["):
        try:
            parts = json.loads(s)
        except json.JSONDecodeError as e:
            raise XcodonError(f"bad exec form {s!r}: {e}") from e
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
            raise XcodonError(f"exec form must be a JSON array of strings: {s!r}")
        return parts
    return [*shell, s]


def parse_env(args: str) -> dict[str, str]:
    """Parse ENV/LABEL arguments: one or more ``KEY=value`` pairs, or the legacy ``KEY value`` form."""
    s = args.strip()
    if "=" not in s.split(None, 1)[0]:
        key, _, value = s.partition(" ")
        return {key: value.strip()}
    out: dict[str, str] = {}
    for token in shlex.split(s):
        k, _, v = token.partition("=")
        out[k] = v
    return out


def _parse_paths(args: str) -> list[str]:
    s = args.strip()
    if s.startswith("["):
        parts = json.loads(s)
        return [str(p) for p in parts]
    tokens = shlex.split(s)
    return [t for t in tokens if not t.startswith("--")]


def _hash_tree(paths: Sequence[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        for root, dirs, files in os.walk(p) if p.is_dir() else [(str(p.parent), [], [p.name])]:
            dirs.sort()
            for name in sorted(files):
                full = Path(root) / name
                h.update(str(full.relative_to(p.parent)).encode())
                h.update(full.read_bytes() if full.is_file() else os.readlink(full).encode())
    return h.hexdigest()


class Builder:
    """Executes a parsed Dockerfile subset against a Runtime, one image commit per step."""

    def __init__(self, runtime: "Runtime", context: Path, out: Callable[[str], None] | None = None) -> None:
        self.rt = runtime
        self.context = Path(context).resolve()
        self.out = out or (lambda line: None)
        self.cache_path = runtime.home.path / CACHE_FILE

    # -- cache -------------------------------------------------------------------

    def _cache_get(self, key: str) -> str | None:
        try:
            return json.loads(self.cache_path.read_text()).get(key)
        except (OSError, ValueError):
            return None

    def _cache_put(self, key: str, image_id: str) -> None:
        with self.rt.home.lock("build-cache"):
            try:
                data = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                data = {}
            data[key] = image_id
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.cache_path)

    # -- build -------------------------------------------------------------------

    def build(self, dockerfile_text: str, tags: Sequence[str] = (), build_args: Mapping[str, str] | None = None,
              no_cache: bool = False) -> Image:
        instructions = parse_dockerfile(dockerfile_text)
        if not instructions:
            raise XcodonError("Dockerfile has no instructions")
        overrides = dict(build_args or {})
        args: dict[str, str] = {}
        shell = list(DEFAULT_SHELL)
        image: Image | None = None
        for n, ins in enumerate(instructions, 1):
            self.out(f"Step {n}/{len(instructions)} : {ins.name} {ins.args}")
            if ins.name == "ARG":
                name, has_default, default = ins.args.partition("=")
                name = name.strip()
                args[name] = overrides.get(name, args.get(name, expand_args(default, args) if has_default else ""))
                continue
            if ins.name == "FROM":
                ref = expand_args(ins.args, args).split()
                if len(ref) > 1:
                    raise XcodonError(f"line {ins.line}: multi-stage builds (FROM ... AS name) are not supported")
                image = self.rt.resolve_image(ref[0])
                self.out(f" ---> {image.short_id}")
                continue
            if image is None:
                raise XcodonError(f"line {ins.line}: FROM must come before {ins.name}")
            if ins.name in IGNORED:
                self.out(f" ---> {ins.name} is ignored by xrunner")
                log.warning("Dockerfile line %d: %s is ignored by xrunner", ins.line, ins.name)
                continue
            if ins.name == "SHELL":
                shell = parse_command(ins.args, shell)
                continue
            expanded = expand_args(ins.args, args)
            content_hash = ""
            sources: list[Path] = []
            if ins.name in ("COPY", "ADD"):
                sources = self._resolve_sources(_parse_paths(expanded)[:-1], ins)
                content_hash = _hash_tree(sources)
            key = hashlib.sha256(f"{image.id}|{ins.name}|{expanded}|{content_hash}".encode()).hexdigest()
            cached = None if no_cache else self._cache_get(key)
            if cached and self.rt.images.get(cached) is not None:
                image = self.rt.images.get(cached)
                self.out(f" ---> CACHED {image.short_id}")
                continue
            if ins.name == "RUN":
                image = self._run_step(image, parse_command(expanded, shell), args, expanded)
            elif ins.name in ("COPY", "ADD"):
                image = self._copy_step(image, sources, _parse_paths(expanded)[-1], expanded)
            else:
                image = self._config_step(image, ins.name, expanded, ins)
            self.out(f" ---> {image.short_id}")
            self._cache_put(key, image.id)
        assert image is not None
        for t in tags:
            image = self.rt.images.tag(image.id, t)
        self.out(f"Successfully built {image.short_id}")
        return image

    def _resolve_sources(self, patterns: list[str], ins: Instruction) -> list[Path]:
        out: list[Path] = []
        for pat in patterns:
            if pat.startswith(("http://", "https://")):
                raise XcodonError(f"line {ins.line}: {ins.name} from a URL is not supported")
            matches = sorted(glob.glob(str(self.context / pat)))
            if not matches:
                raise XcodonError(f"line {ins.line}: {ins.name} source {pat!r} not found in the build context")
            for m in matches:
                p = Path(m).resolve()
                if self.context not in p.parents and p != self.context:
                    raise XcodonError(f"line {ins.line}: {ins.name} source {pat!r} is outside the build context")
                out.append(p)
        return out

    def _run_step(self, image: Image, argv: list[str], args: Mapping[str, str], text: str) -> Image:
        c = self.rt.create(image.id, command=argv)
        try:
            self.rt.start(c)
            p = self.rt.popen(c, argv, env=dict(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert p.stdout is not None
            for raw in p.stdout:
                self.out(raw.decode(errors="replace").rstrip("\n"))
            code = p.wait()
            self.rt.stop(c)
            if code != 0:
                raise XcodonError(f"RUN failed with exit {code}: {text}")
            return self.rt.commit(c, None, message=f"RUN {text}")
        finally:
            try:
                self.rt.remove(c, force=True)
            except XcodonError:
                pass

    def _copy_step(self, image: Image, sources: list[Path], dest: str, text: str) -> Image:
        workdir = image.config.get("config", {}).get("WorkingDir") or "/"
        dest_abs = dest if dest.startswith("/") else os.path.join(workdir, dest)
        into_dir = dest.endswith("/") or len(sources) > 1 or dest_abs.endswith(".")
        work = Path(tempfile.mkdtemp(prefix="copy-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            for src in sources:
                target = layer / dest_abs.lstrip("/")
                if into_dir or src.is_dir() and target.exists():
                    target = target / src.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
            return self.rt.images.commit(image, layer, created_by=f"COPY {text}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _config_step(self, image: Image, name: str, args: str, ins: Instruction) -> Image:
        if name == "ENV":
            changes = {"Env": [f"{k}={v}" for k, v in parse_env(args).items()]}
        elif name == "LABEL":
            changes = {"Labels": parse_env(args)}
        elif name == "WORKDIR":
            base = image.config.get("config", {}).get("WorkingDir") or "/"
            path = args if args.startswith("/") else os.path.join(base, args)
            changes = {"WorkingDir": path}
            return self._workdir_step(image, path, changes, args)
        elif name == "USER":
            changes = {"User": args}
        elif name in ("CMD", "ENTRYPOINT"):
            changes = {"Cmd" if name == "CMD" else "Entrypoint": parse_command(args, DEFAULT_SHELL)}
        else:
            raise XcodonError(f"unsupported Dockerfile instruction {name} at line {ins.line}")
        return self.rt.images.commit(image, None, changes=changes, created_by=f"{name} {args}")

    def _workdir_step(self, image: Image, path: str, changes: dict, args: str) -> Image:
        """WORKDIR creates the directory in the image when the base rootfs lacks it.

        Matches docker: if the path does not exist in the base image, the
        commit carries a layer with just that empty directory (and its
        parents); otherwise this is a config-only commit like the other
        metadata instructions.
        """
        target = image.rootfs / path.lstrip("/")
        if target.exists():
            return self.rt.images.commit(image, None, changes=changes, created_by=f"WORKDIR {args}")
        work = Path(tempfile.mkdtemp(prefix="workdir-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            layer.mkdir()
            layer_target = layer / path.lstrip("/")
            layer_target.mkdir(parents=True, exist_ok=True)
            for d in [layer_target, *layer_target.parents]:
                if d == layer:
                    break
                os.chmod(d, 0o755)
            return self.rt.images.commit(image, layer, changes=changes, created_by=f"WORKDIR {args}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
