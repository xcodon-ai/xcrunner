"""Run a Dockerfile subset in xrunner containers. See spec section 11.3."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import stat
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
KNOWN_INSTRUCTIONS = {"ARG", "FROM", "ENV", "LABEL", "WORKDIR", "USER", "CMD", "ENTRYPOINT", "RUN", "COPY", "ADD",
                      "SHELL"} | IGNORED
DEFAULT_SHELL = ["/bin/sh", "-c"]
MAX_SYMLINK_HOPS = 40
# ``$NAME`` / ``${NAME}`` / ``${NAME:-default}`` / ``${NAME:+word}``.
_VAR = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(:-|:\+)([^}]*)\}"
    r"|\{([A-Za-z_][A-Za-z0-9_]*)\}"
    r"|([A-Za-z_][A-Za-z0-9_]*))"
)


@dataclass
class Instruction:
    name: str
    args: str
    line: int


def parse_dockerfile(text: str) -> list[Instruction]:
    """Split a Dockerfile into instructions, joining backslash continuation lines.

    Blank lines and full-line comments (a ``#`` as the first non-space
    character) are skipped outside of a continuation; a ``#`` elsewhere on a
    line is kept as part of the instruction's arguments, matching docker. A
    comment line in the middle of a continuation is dropped without ending
    the instruction, also matching docker.
    """
    out: list[Instruction] = []
    buf: list[str] = []
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if buf and line.lstrip().startswith("#"):
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
    """Expand ``$NAME``, ``${NAME}``, ``${NAME:-default}``, and ``${NAME:+word}``.

    A backslash right before ``$`` escapes it to a literal ``$`` (the
    backslash is consumed). Text inside single quotes is left untouched,
    ``$`` included, the way a POSIX shell leaves it -- the quote characters
    themselves are not stripped here; a later ``shlex``-based dequote does
    that once expansion is done, so word-splitting still sees the original
    quoting.
    """

    def sub(m: re.Match) -> str:
        if m.group(1) is not None:
            name, op, word = m.group(1), m.group(2), m.group(3)
            value = args.get(name)
            if op == ":-":
                return value if value else word
            return word if value else ""
        name = m.group(4) or m.group(5)
        return args.get(name, "")

    out: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    while i < n:
        c = text[i]
        if c == "'":
            in_single = not in_single
            out.append(c)
            i += 1
            continue
        if in_single:
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n and text[i + 1] == "$":
            out.append("$")
            i += 2
            continue
        if c == "$":
            m = _VAR.match(text, i)
            if m:
                out.append(sub(m))
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


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


def parse_env(args: str, scope: Mapping[str, str] | None = None) -> dict[str, str]:
    """Parse ENV/LABEL arguments.

    Splits into Dockerfile-level words first, using the raw (unexpanded)
    text so the original quoting decides word boundaries, then expands
    ``$VAR`` within each already-bounded word. Doing it in the other order
    (expand the whole line, then split) would let a value that expands to
    include a space get mis-split into a second word.
    """
    s = args.strip()
    if not s:
        raise XcodonError("ENV/LABEL requires at least one KEY=value pair")
    scope = scope or {}
    if "=" not in s.split(None, 1)[0]:
        key, _, value = s.partition(" ")
        value = value.strip()
        if not value:
            raise XcodonError(f"ENV {key!r} requires a value")
        return {key: expand_args(value, scope)}
    try:
        tokens = shlex.split(s)
    except ValueError as e:
        raise XcodonError(f"cannot parse ENV/LABEL arguments {s!r}: {e}") from e
    out: dict[str, str] = {}
    for token in tokens:
        expanded = expand_args(token, scope)
        k, _, v = expanded.partition("=")
        out[k] = v
    return out


def _hash_tree(paths: Sequence[Path]) -> str:
    """Hash a set of COPY/ADD sources: every entry's relative path, type, mode, and content.

    Symlinks are never followed (a symlink to a directory is hashed as a
    link, not descended into); empty directories are included so that adding
    or removing one still changes the hash.
    """
    h = hashlib.sha256()

    def add(full: Path, rel: str) -> None:
        st = full.lstat()
        if stat.S_ISLNK(st.st_mode):
            h.update(f"L\0{rel}\0".encode())
            h.update(os.readlink(full).encode())
            h.update(b"\0")
            return
        mode = oct(stat.S_IMODE(st.st_mode)).encode()
        if stat.S_ISDIR(st.st_mode):
            h.update(b"D\0" + rel.encode() + b"\0" + mode + b"\0")
            for entry in sorted(os.scandir(full), key=lambda e: e.name):
                add(Path(entry.path), f"{rel}/{entry.name}" if rel else entry.name)
        else:
            h.update(b"F\0" + rel.encode() + b"\0" + mode + b"\0")
            h.update(full.read_bytes())
            h.update(b"\0")

    for p in sorted(paths):
        add(p, p.name)
    return h.hexdigest()


# -- symlink-safe rootfs path resolution --------------------------------------

def resolve_in_rootfs(rootfs: Path, guest: str) -> str:
    """Resolve a guest path inside an image rootfs without ever following a symlink onto the host.

    Walks the normalized guest path one component at a time. At each step
    the path built so far is already known to contain no symlinks (each
    earlier component was individually verified), so joining exactly one
    more raw component and taking a single ``lstat`` of that is safe: the
    host kernel only has to walk through already-verified real directories,
    and the trailing component itself is never dereferenced by ``lstat``.

    Every existing symlink found this way is read with ``os.readlink`` --
    never followed by the host kernel. An absolute target is re-rooted at
    ``rootfs`` (never the host's real root); a relative target is joined to
    the symlink's own containing directory. The result is renormalized and
    clamped at ``/`` after every hop, so ``..`` can never climb above it,
    matching docker. Raises ``XcodonError`` past ``MAX_SYMLINK_HOPS`` hops
    (a symlink loop).
    """
    normalized = posixpath.normpath("/" + guest.lstrip("/"))
    queue = [p for p in normalized.split("/") if p]
    resolved: list[str] = []
    hops = 0
    while queue:
        part = queue.pop(0)
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        if part == ".":
            continue
        candidate = resolved + [part]
        host_path = rootfs / "/".join(candidate)
        try:
            st = host_path.lstat()
        except OSError:
            resolved = candidate
            continue
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                raise XcodonError(f"too many symlink hops resolving {guest!r} in the image")
            target = os.readlink(host_path)
            target_parts = [p for p in target.split("/") if p]
            if target.startswith("/"):
                resolved = []
            queue = target_parts + queue
        else:
            resolved = candidate
    return "/" + "/".join(resolved) if resolved else "/"


def _rootfs_lstat(rootfs: Path, guest: str) -> os.stat_result | None:
    """``lstat`` of a guest path, resolved symlink-safely first. None if it does not exist."""
    resolved = resolve_in_rootfs(rootfs, guest)
    try:
        return (rootfs / resolved.lstrip("/")).lstat()
    except OSError:
        return None


def _rootfs_dir_mode(rootfs: Path, guest: str) -> int | None:
    """The mode of ``guest`` in ``rootfs`` if it exists there as a directory, else None."""
    st = _rootfs_lstat(rootfs, guest)
    if st is not None and stat.S_ISDIR(st.st_mode):
        return stat.S_IMODE(st.st_mode)
    return None


def _rootfs_exists(rootfs: Path, guest: str) -> bool:
    return _rootfs_lstat(rootfs, guest) is not None


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

    # -- variable scope ------------------------------------------------------

    def _scope(self, image: Image, args: Mapping[str, str]) -> dict[str, str]:
        """ARGs in scope plus the image's current Env, with Env winning on a name clash."""
        scope: dict[str, str] = dict(args)
        for item in image.config.get("config", {}).get("Env") or []:
            k, _, v = item.partition("=")
            scope[k] = v
        return scope

    def _dequote(self, text: str) -> str:
        try:
            parts = shlex.split(text)
        except ValueError as e:
            raise XcodonError(f"cannot parse value {text!r}: {e}") from e
        return " ".join(parts)

    def _arg_value(self, raw: str, scope: Mapping[str, str]) -> str:
        # Expand first (single quotes there suppress $ the way a shell would),
        # then strip the quote characters -- doing it the other way round
        # would lose which spans were single-quoted before expansion ever saw them.
        return self._dequote(expand_args(raw, scope))

    # -- build -------------------------------------------------------------------

    def build(self, dockerfile_text: str, tags: Sequence[str] = (), build_args: Mapping[str, str] | None = None,
              no_cache: bool = False) -> Image:
        instructions = parse_dockerfile(dockerfile_text)
        if not instructions:
            raise XcodonError("Dockerfile has no instructions")
        for ins in instructions:
            if ins.name not in KNOWN_INSTRUCTIONS:
                raise XcodonError(f"line {ins.line}: unsupported Dockerfile instruction {ins.name}")

        overrides = dict(build_args or {})
        # ARGs declared before the first FROM: usable only to expand the FROM
        # line itself. None means "declared, no default, not overridden".
        global_defaults: dict[str, str | None] = {}
        args: dict[str, str] = {}
        shell = list(DEFAULT_SHELL)
        image: Image | None = None

        for n, ins in enumerate(instructions, 1):
            self.out(f"Step {n}/{len(instructions)} : {ins.name} {ins.args}")

            if ins.name == "ARG":
                name, has_default, default_raw = ins.args.partition("=")
                name = name.strip()
                if not name:
                    raise XcodonError(f"line {ins.line}: ARG requires a name")
                if image is None:
                    if name in overrides:
                        global_defaults[name] = overrides[name]
                    elif has_default:
                        scope = {k: v for k, v in global_defaults.items() if v is not None}
                        global_defaults[name] = self._arg_value(default_raw, scope)
                    else:
                        global_defaults.setdefault(name, None)
                else:
                    if name in overrides:
                        args[name] = overrides[name]
                    elif has_default:
                        args[name] = self._arg_value(default_raw, self._scope(image, args))
                    elif global_defaults.get(name) is not None:
                        args[name] = global_defaults[name]
                    else:
                        args.pop(name, None)
                continue

            if ins.name == "FROM":
                if image is not None:
                    raise XcodonError(f"line {ins.line}: multi-stage builds (a second FROM) are not supported")
                from_scope = {k: v for k, v in global_defaults.items() if v is not None}
                tokens = expand_args(ins.args, from_scope).split()
                while tokens and tokens[0].startswith("--"):
                    flag = tokens.pop(0)
                    if flag.startswith("--platform"):
                        self.out(f" ---> {flag} is ignored by xrunner")
                        log.warning("Dockerfile line %d: %s is ignored by xrunner", ins.line, flag)
                    else:
                        raise XcodonError(f"line {ins.line}: unsupported FROM flag {flag!r}")
                if not tokens:
                    raise XcodonError(f"line {ins.line}: FROM requires an image reference")
                if len(tokens) > 1:
                    raise XcodonError(f"line {ins.line}: multi-stage builds (FROM ... AS name) are not supported")
                image = self.rt.resolve_image(tokens[0])
                args = {}
                shell = list(image.config.get("config", {}).get("Shell") or DEFAULT_SHELL)
                self.out(f" ---> {image.short_id}")
                continue

            if image is None:
                raise XcodonError(f"line {ins.line}: FROM must come before {ins.name}")

            if ins.name in IGNORED:
                self.out(f" ---> {ins.name} is ignored by xrunner")
                log.warning("Dockerfile line %d: %s is ignored by xrunner", ins.line, ins.name)
                continue

            content_hash = ""
            sources: list[Path] = []
            dest = ""
            parsed_env: dict[str, str] = {}
            if ins.name in ("COPY", "ADD"):
                scope = self._scope(image, args)
                raw_paths = self._parse_copy_args(ins.args, ins)
                if len(raw_paths) < 2:
                    raise XcodonError(f"line {ins.line}: {ins.name} requires at least one source and a destination")
                paths = [expand_args(p, scope) for p in raw_paths]
                sources = self._resolve_sources(paths[:-1], ins)
                dest = paths[-1]
                content_hash = _hash_tree(sources)
                expanded = shlex.join(paths)
            elif ins.name in ("ENV", "LABEL"):
                parsed_env = parse_env(ins.args, self._scope(image, args))
                expanded = json.dumps(parsed_env, sort_keys=True)
            elif ins.name in ("WORKDIR", "USER"):
                expanded = expand_args(ins.args, self._scope(image, args))
            else:  # RUN, CMD, ENTRYPOINT, SHELL: used verbatim, the shell resolves its own vars
                expanded = ins.args
                if ins.name == "RUN":
                    content_hash = ",".join(f"{k}={v}" for k, v in sorted(args.items()))

            key = hashlib.sha256(f"{image.id}|{ins.name}|{expanded}|{content_hash}".encode()).hexdigest()
            cached_image = None
            if not no_cache:
                cached_id = self._cache_get(key)
                if cached_id:
                    cached_image = self.rt.images.get(cached_id)
            if cached_image is not None:
                image = cached_image
                self.out(f" ---> CACHED {image.short_id}")
                continue

            if ins.name == "RUN":
                image = self._run_step(image, parse_command(expanded, shell), args, expanded)
            elif ins.name in ("COPY", "ADD"):
                image = self._copy_step(image, sources, dest, expanded, ins)
            elif ins.name in ("ENV", "LABEL"):
                changes = ({"Env": [f"{k}={v}" for k, v in parsed_env.items()]} if ins.name == "ENV"
                           else {"Labels": parsed_env})
                image = self.rt.images.commit(image, None, changes=changes, created_by=f"{ins.name} {ins.args}")
            else:
                image, shell = self._config_step(image, ins.name, expanded, ins, shell)
            self.out(f" ---> {image.short_id}")
            self._cache_put(key, image.id)

        assert image is not None
        for t in tags:
            image = self.rt.images.tag(image.id, t)
        self.out(f"Successfully built {image.short_id}")
        return image

    # -- COPY / ADD ----------------------------------------------------------

    def _parse_copy_args(self, text: str, ins: Instruction) -> list[str]:
        """Split a COPY/ADD instruction's raw text into path tokens, handling flags.

        Called on the *raw* (unexpanded) text: word-splitting must see the
        Dockerfile's own quoting before any ``$VAR`` is substituted, so a
        substituted value's internal spaces are never mistaken for a new
        word boundary by a second round of splitting.
        """
        s = text.strip()
        if s.startswith("["):
            try:
                parts = json.loads(s)
            except json.JSONDecodeError as e:
                raise XcodonError(f"line {ins.line}: bad exec-form path list {s!r}: {e}") from e
            if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
                raise XcodonError(f"line {ins.line}: {ins.name} path list must be a JSON array of strings: {s!r}")
            return parts
        try:
            tokens = shlex.split(s)
        except ValueError as e:
            raise XcodonError(f"line {ins.line}: cannot parse {ins.name} arguments {s!r}: {e}") from e
        paths: list[str] = []
        for t in tokens:
            if t.startswith("--from"):
                raise XcodonError(f"line {ins.line}: multi-stage builds ({ins.name} --from) are not supported")
            if t.startswith("--chown") or t.startswith("--chmod"):
                self.out(f" ---> {ins.name} {t} is ignored by xrunner")
                log.warning("Dockerfile line %d: %s %s is ignored by xrunner", ins.line, ins.name, t)
                continue
            if t.startswith("--"):
                raise XcodonError(f"line {ins.line}: unsupported {ins.name} flag {t!r}")
            paths.append(t)
        return paths

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

    def _src_mode(self, src: Path) -> int:
        return stat.S_IMODE(os.lstat(src).st_mode)

    def _remove_existing(self, target: Path) -> None:
        """Remove whatever is at ``target`` so a placement never writes through a stale link.

        Checked in this order because ``is_dir``/``is_file`` follow symlinks:
        a symlink (to a file or a directory) is always unlinked, never
        ``rmtree``'d (which refuses to operate on a symlink anyway).
        """
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    def _guard_within_layer(self, layer: Path, target: Path) -> None:
        """Second guard, in addition to guest-path resolution: refuse to write outside ``layer``."""
        layer_str = os.path.normpath(str(layer))
        target_str = os.path.normpath(str(target))
        if target_str != layer_str and not target_str.startswith(layer_str + os.sep):
            raise XcodonError(f"refusing to write outside the build layer: {target}")

    def _apply_mode_fixups(self, fixups: list[tuple[Path, int]]) -> None:
        """Chmod every directory this step created to its final mode, deepest first.

        Deferred until every file is in place: a directory whose final mode
        has no owner-write bit (mirroring a read-only source directory or an
        existing image directory) must stay writable while its own contents
        are still being created inside it.
        """
        for path, mode in sorted(fixups, key=lambda pm: len(pm[0].parts), reverse=True):
            os.chmod(path, mode)

    def _materialize_dir(self, layer: Path, resolved_rel: str, image: Image, leaf_fallback_mode: int,
                          mode_fixups: list[tuple[Path, int]]) -> Path:
        """mkdir -p an already symlink-resolved, normalized guest path under ``layer``.

        Every directory created along the way is queued to take the mode of
        the same path in ``image.rootfs`` when that path exists there as a
        directory; otherwise the final component gets ``leaf_fallback_mode``
        and any earlier scaffolding parent gets 0o755. The mode is only
        queued (in ``mode_fixups``), never applied immediately: applying it
        here would block writing further content into a directory whose
        resolved mode is not owner-writable.
        """
        cur = layer
        rootfs_cur = image.rootfs
        parts = [p for p in resolved_rel.split("/") if p not in ("", ".")]
        for i, part in enumerate(parts):
            cur = cur / part
            rootfs_cur = rootfs_cur / part
            if not cur.exists():
                mode = leaf_fallback_mode if i == len(parts) - 1 else 0o755
                try:
                    st = rootfs_cur.lstat()
                    if stat.S_ISDIR(st.st_mode):
                        mode = stat.S_IMODE(st.st_mode)
                except OSError:
                    pass
                cur.mkdir()
                mode_fixups.append((cur, mode))
        return cur

    def _copy_tree_into(self, src: Path, target: Path, image: Image, dest_rel: str,
                         mode_fixups: list[tuple[Path, int]]) -> None:
        """Merge the contents of a source directory into an already-materialized target directory.

        A later source (or a later entry in the same source) always wins: an
        existing non-directory in the way of a directory entry is replaced,
        and a file or symlink placement always removes whatever was there
        first. A nested subdirectory takes the mode of the same path in
        ``image.rootfs`` when it exists there, else its own mode in ``src``;
        that mode is queued in ``mode_fixups``, applied only after this
        subdirectory's own contents are placed (deepest first overall).
        """
        for entry in sorted(os.scandir(src), key=lambda e: e.name):
            child_rel_raw = f"{dest_rel}/{entry.name}" if dest_rel else entry.name
            resolved_child_rel = resolve_in_rootfs(image.rootfs, "/" + child_rel_raw).lstrip("/")
            child_target = target / entry.name
            if entry.is_symlink():
                self._remove_existing(child_target)
                os.symlink(os.readlink(entry.path), child_target)
            elif entry.is_dir(follow_symlinks=False):
                if child_target.is_symlink() or (child_target.exists() and not child_target.is_dir()):
                    self._remove_existing(child_target)
                is_new = not child_target.exists()
                mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
                existing_mode = _rootfs_dir_mode(image.rootfs, "/" + resolved_child_rel)
                if existing_mode is not None:
                    mode = existing_mode
                if is_new:
                    child_target.mkdir()
                self._copy_tree_into(Path(entry.path), child_target, image, resolved_child_rel, mode_fixups)
                mode_fixups.append((child_target, mode))
            else:
                self._remove_existing(child_target)
                shutil.copy2(entry.path, child_target, follow_symlinks=False)

    def _copy_step(self, image: Image, sources: list[Path], dest: str, text: str, ins: Instruction) -> Image:
        workdir = image.config.get("config", {}).get("WorkingDir") or "/"
        dest_abs = dest if dest.startswith("/") else os.path.join(workdir, dest)
        # Resolve the destination as an absolute guest path: normalize (so ".."
        # can never climb above "/", matching docker) and follow any existing
        # symlink component to where it actually points inside the rootfs,
        # never onto the host (see resolve_in_rootfs).
        resolved_dest = resolve_in_rootfs(image.rootfs, dest_abs)
        dest_rel = resolved_dest.lstrip("/")
        dest_dir_form = dest == "." or dest.endswith("/") or dest.endswith("/.")
        dest_exists_as_dir = _rootfs_dir_mode(image.rootfs, resolved_dest) is not None
        dest_looks_like_dir = dest_dir_form or dest_exists_as_dir
        if len(sources) > 1 and not dest_looks_like_dir:
            raise XcodonError(
                f"line {ins.line}: when using {ins.name} with more than one source file, "
                "the destination must be a directory and end with a /"
            )
        dest_is_dir = dest_looks_like_dir or len(sources) > 1
        work = Path(tempfile.mkdtemp(prefix="copy-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            layer.mkdir()
            mode_fixups: list[tuple[Path, int]] = []
            for src in sources:
                if src.is_dir():
                    # A directory source always copies its contents, never nested
                    # under its own name, regardless of the destination's form.
                    target_dir = self._materialize_dir(layer, dest_rel, image, self._src_mode(src), mode_fixups)
                    self._guard_within_layer(layer, target_dir)
                    self._copy_tree_into(src, target_dir, image, dest_rel, mode_fixups)
                elif dest_is_dir:
                    target_dir = self._materialize_dir(layer, dest_rel, image, 0o755, mode_fixups)
                    target = target_dir / src.name
                    self._guard_within_layer(layer, target)
                    self._remove_existing(target)
                    shutil.copy2(src, target, follow_symlinks=False)
                else:
                    parent_rel = os.path.dirname(dest_rel)
                    if parent_rel:
                        self._materialize_dir(layer, parent_rel, image, 0o755, mode_fixups)
                    target = layer / dest_rel
                    self._guard_within_layer(layer, target)
                    self._remove_existing(target)
                    shutil.copy2(src, target, follow_symlinks=False)
            self._apply_mode_fixups(mode_fixups)
            return self.rt.images.commit(image, layer, created_by=f"{ins.name} {text}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- RUN -------------------------------------------------------------------

    def _run_step(self, image: Image, argv: list[str], args: Mapping[str, str], text: str) -> Image:
        # An ARG is only exposed as a RUN environment variable when no ENV of
        # the same name already won that name for the image: ENV always wins.
        env_keys = {item.partition("=")[0] for item in (image.config.get("config", {}).get("Env") or [])}
        run_env = {k: v for k, v in args.items() if k not in env_keys}
        c = self.rt.create(image.id, command=argv)
        try:
            self.rt.start(c)
            p = self.rt.popen(c, argv, env=run_env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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

    # -- WORKDIR / USER / CMD / ENTRYPOINT / SHELL ------------------------------

    def _config_step(self, image: Image, name: str, text: str, ins: Instruction,
                      shell: list[str]) -> tuple[Image, list[str]]:
        if name == "WORKDIR":
            base = image.config.get("config", {}).get("WorkingDir") or "/"
            raw_path = text if text.startswith("/") else os.path.join(base, text)
            path = posixpath.normpath(raw_path)
            return self._workdir_step(image, path, text), shell
        elif name == "USER":
            changes = {"User": text}
        elif name in ("CMD", "ENTRYPOINT"):
            changes = {"Cmd" if name == "CMD" else "Entrypoint": parse_command(text, shell)}
        elif name == "SHELL":
            new_shell = parse_command(text, shell)
            image = self.rt.images.commit(image, None, changes={"Shell": list(new_shell)},
                                           created_by=f"SHELL {text}")
            return image, new_shell
        else:
            raise XcodonError(f"line {ins.line}: unsupported Dockerfile instruction {name}")
        return self.rt.images.commit(image, None, changes=changes, created_by=f"{name} {text}"), shell

    def _workdir_step(self, image: Image, path: str, text: str) -> Image:
        """WORKDIR creates the directory in the image when the base rootfs lacks it.

        Matches docker: if the path does not exist in the base image, the
        commit carries a layer with just that empty directory (and its
        parents, each preserving any pre-existing mode); otherwise this is a
        config-only commit like the other metadata instructions. The path is
        resolved symlink-safely before either check, so WORKDIR through an
        existing symlink lands on its target, never wipes it out.
        """
        changes = {"WorkingDir": path}
        resolved = resolve_in_rootfs(image.rootfs, path)
        if _rootfs_exists(image.rootfs, resolved):
            return self.rt.images.commit(image, None, changes=changes, created_by=f"WORKDIR {text}")
        work = Path(tempfile.mkdtemp(prefix="workdir-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            layer.mkdir()
            mode_fixups: list[tuple[Path, int]] = []
            target_dir = self._materialize_dir(layer, resolved.lstrip("/"), image, 0o755, mode_fixups)
            self._guard_within_layer(layer, target_dir)
            self._apply_mode_fixups(mode_fixups)
            return self.rt.images.commit(image, layer, changes=changes, created_by=f"WORKDIR {text}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
