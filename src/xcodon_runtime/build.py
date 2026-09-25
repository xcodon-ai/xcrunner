"""Run a Dockerfile subset in xrunner containers. See spec section 11.3."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import posixpath
import shlex
import shutil
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from xcodon_runtime.buildpaths import (
    expand_args,
    rootfs_dir_mode,
    rootfs_exists,
    resolve_in_rootfs,
    split_words,
)
from xcodon_runtime.containers import _rmtree_tolerant
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
    text so the original quoting decides word boundaries and which spans are
    single-quoted (see ``split_words``), then expands ``$VAR`` within each
    already-bounded word. Doing it in the other order (expand the whole
    line, then split) would both lose single-quote-suppresses-`$`
    information and let a value that expands to include a space get
    mis-split into a second word.
    """
    s = args.strip()
    if not s:
        raise XcodonError("ENV/LABEL requires at least one KEY=value pair")
    scope = scope or {}
    if "=" not in s.split(None, 1)[0]:
        key, _, rest = s.partition(" ")
        rest = rest.strip()
        if not rest:
            raise XcodonError(f"ENV {key!r} requires a value")
        return {key: " ".join(split_words(rest, scope))}
    words = split_words(s, scope)
    out: dict[str, str] = {}
    for word in words:
        k, has_eq, v = word.partition("=")
        if not has_eq:
            # Docker rejects a bare key once the KEY=value form is in use.
            raise XcodonError(f"ENV/LABEL word {word!r} has no '=': every word must be KEY=value")
        out[k] = v
    return out


def _hash_tree(sources: Sequence[tuple[str, Path]]) -> str:
    """Hash a set of COPY/ADD sources: every entry's relative path, type, mode, and content.

    Each source is a (name, path) pair from ``Builder._resolve_sources``: the
    name the source is copied under and the resolved path its content comes
    from. Symlinks below a source are never followed (a symlink to a
    directory is hashed as a link, not descended into); empty directories
    are included so that adding or removing one still changes the hash.
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

    for name, p in sorted(sources):
        add(p, name)
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

    # -- variable scope ------------------------------------------------------

    def _scope(self, image: Image, args: Mapping[str, str]) -> dict[str, str]:
        """ARGs in scope plus the image's current Env, with Env winning on a name clash."""
        scope: dict[str, str] = dict(args)
        for item in image.config.get("config", {}).get("Env") or []:
            k, _, v = item.partition("=")
            scope[k] = v
        return scope

    def _arg_value(self, raw: str, scope: Mapping[str, str]) -> str:
        return " ".join(split_words(raw, scope))

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

        # ``store`` shared from the FROM step to the end, so a concurrent
        # ``prune --all`` cannot delete an intermediate (untagged) step image
        # while later steps still build on it. It is taken only after FROM
        # has resolved, which may pull: lock order is pull -> store, and a
        # pull takes both itself.
        with ExitStack() as held:
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
                    tokens = split_words(ins.args, from_scope)
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
                    held.enter_context(self.rt.home.lock("store", shared=True))
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
                sources: list[tuple[str, Path]] = []
                dest = ""
                parsed_env: dict[str, str] = {}
                if ins.name in ("COPY", "ADD"):
                    scope = self._scope(image, args)
                    paths = self._parse_copy_args(ins.args, ins, scope)
                    if len(paths) < 2:
                        raise XcodonError(f"line {ins.line}: {ins.name} requires at least one source and a destination")
                    sources = self._resolve_sources(paths[:-1], ins)
                    dest = paths[-1]
                    content_hash = _hash_tree(sources)
                    expanded = shlex.join(paths)
                elif ins.name in ("ENV", "LABEL"):
                    parsed_env = parse_env(ins.args, self._scope(image, args))
                    expanded = json.dumps(parsed_env, sort_keys=True)
                elif ins.name in ("WORKDIR", "USER"):
                    expanded = " ".join(split_words(ins.args, self._scope(image, args)))
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

    def _parse_copy_args(self, text: str, ins: Instruction, scope: Mapping[str, str]) -> list[str]:
        """Split and expand a COPY/ADD instruction's raw text into path tokens, handling flags.

        Word-splitting and ``$VAR`` expansion happen together, in
        ``split_words``, on the *raw* text: the Dockerfile's own quoting
        decides word boundaries (and which spans are single-quoted, so a
        literal ``$`` there is never substituted) before any value is
        substituted in.
        """
        s = text.strip()
        if s.startswith("["):
            try:
                parts = json.loads(s)
            except json.JSONDecodeError as e:
                raise XcodonError(f"line {ins.line}: bad exec-form path list {s!r}: {e}") from e
            if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
                raise XcodonError(f"line {ins.line}: {ins.name} path list must be a JSON array of strings: {s!r}")
            words = [expand_args(p, scope) for p in parts]
        else:
            words = split_words(s, scope)
        paths: list[str] = []
        for t in words:
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

    def _resolve_sources(self, patterns: list[str], ins: Instruction) -> list[tuple[str, Path]]:
        """Match COPY/ADD source patterns in the context. Returns (name, content path) pairs.

        The name comes from the lexical path, so ``COPY link.txt /d/`` lands
        at ``/d/link.txt``. The content path is the resolved path: a source
        that is itself a symlink is followed, as docker does, and the
        resolved path must stay inside the context. Symlinks below a source
        directory are copied as links.
        """
        out: list[tuple[str, Path]] = []
        for pat in patterns:
            if pat.startswith(("http://", "https://")):
                raise XcodonError(f"line {ins.line}: {ins.name} from a URL is not supported")
            matches = sorted(glob.glob(str(self.context / pat)))
            if not matches:
                raise XcodonError(f"line {ins.line}: {ins.name} source {pat!r} not found in the build context")
            for m in matches:
                lexical = Path(os.path.normpath(m))
                p = lexical.resolve()
                if self.context not in p.parents and p != self.context:
                    raise XcodonError(f"line {ins.line}: {ins.name} source {pat!r} is outside the build context")
                if not p.exists():
                    raise XcodonError(f"line {ins.line}: {ins.name} source {pat!r} is a link to a missing file")
                out.append((lexical.name, p))
        return out

    def _src_mode(self, src: Path) -> int:
        return stat.S_IMODE(os.lstat(src).st_mode)

    def _assert_within_layer(self, layer: Path, target: Path) -> None:
        """Refuse to write outside ``layer``, following symlinks on both sides (realpath).

        A purely lexical (``os.path.normpath``) comparison would miss a
        target that only escapes because some component *inside* the layer
        was itself replaced by a symlink earlier in this same step; ``..``
        in a guest path is already excluded upstream by ``resolve_in_rootfs``,
        so this is defense in depth, not the primary guard.
        """
        layer_real = os.path.realpath(str(layer))
        target_real = os.path.realpath(str(target))
        if target_real != layer_real and not target_real.startswith(layer_real + os.sep):
            raise XcodonError(f"refusing to write outside the build layer: {target}")

    def _remove_existing(self, target: Path, mode_fixups: list[tuple[Path, int]]) -> None:
        """Remove whatever is at ``target`` so a placement never writes through a stale link.

        Checked in this order because ``is_dir``/``is_file`` follow symlinks:
        a symlink (to a file or a directory) is always unlinked, never
        ``rmtree``'d (which refuses to operate on a symlink anyway). Also
        drops every queued mode fixup at or under ``target``: one queued for
        a directory that used to be here (e.g. from an earlier source in the
        same COPY) must never be applied to whatever now occupies -- or used
        to be reachable through -- that path, which is how a stale fixup
        could otherwise chmod a symlink's host target, or hit a path that no
        longer exists because a parent is now a file.
        """
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            return
        target_str = str(target)
        mode_fixups[:] = [
            (p, m) for p, m in mode_fixups
            if str(p) != target_str and not str(p).startswith(target_str + os.sep)
        ]

    def _apply_mode_fixups(self, layer: Path, fixups: list[tuple[Path, int]]) -> None:
        """Chmod every directory this step created to its final mode, deepest first.

        Deferred until every file is in place: a directory whose final mode
        has no owner-write bit (mirroring a read-only source directory or an
        existing image directory) must stay writable while its own contents
        are still being created inside it.

        Each entry is re-checked here, right before the chmod, rather than
        trusted from when it was queued: ``os.lstat`` must show a real
        directory (``os.chmod`` follows symlinks -- Linux has no lchmod, so
        ``follow_symlinks=False`` raises ``NotImplementedError`` -- and
        chmod'ing a symlink would silently reach through it and change a
        *host* path's mode instead), and its ``os.path.realpath`` must stay
        inside the layer's own realpath. A path that no longer exists, or
        that is no longer a real directory there, or that somehow resolves
        outside the layer, is skipped instead of chmod'ed.
        """
        layer_real = os.path.realpath(str(layer))
        for path, mode in sorted(fixups, key=lambda pm: len(pm[0].parts), reverse=True):
            try:
                st = path.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            real = os.path.realpath(str(path))
            if real != layer_real and not real.startswith(layer_real + os.sep):
                continue
            os.chmod(path, mode)

    def _materialize_dir(self, layer: Path, resolved_rel: str, image: Image, leaf_fallback_mode: int,
                          mode_fixups: list[tuple[Path, int]]) -> Path:
        """mkdir -p an already symlink-resolved, normalized guest path under ``layer``.

        Every directory created along the way is queued to take the mode of
        the same path in ``image.rootfs`` when that path exists there as a
        directory; otherwise the final component gets ``leaf_fallback_mode``
        and any earlier scaffolding parent gets 0o755. The mode is only
        queued (in ``mode_fixups``), never applied immediately (see
        ``_apply_mode_fixups``). Whatever already occupies a path component
        that is not itself a directory (a symlink left by an earlier source
        in the same COPY, or a plain file) is removed first and replaced
        with a fresh directory, matching docker's own "a later directory
        replaces an earlier non-directory" merge behavior.
        """
        cur = layer
        rootfs_cur = image.rootfs
        parts = [p for p in resolved_rel.split("/") if p not in ("", ".")]
        for i, part in enumerate(parts):
            cur = cur / part
            rootfs_cur = rootfs_cur / part
            if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
                self._remove_existing(cur, mode_fixups)
            if not cur.exists():
                mode = leaf_fallback_mode if i == len(parts) - 1 else 0o755
                try:
                    st = rootfs_cur.lstat()
                    if stat.S_ISDIR(st.st_mode):
                        mode = stat.S_IMODE(st.st_mode)
                except OSError:
                    pass
                self._assert_within_layer(layer, cur)
                cur.mkdir()
                mode_fixups.append((cur, mode))
        return cur

    def _place_non_dir(self, layer: Path, src: Path, target: Path, mode_fixups: list[tuple[Path, int]]) -> None:
        """Put a file or symlink from the context at exactly ``target`` in the layer.

        ``target`` keeps the entry's own name: it is never resolved through
        a symlink, in the image or in the layer. Whatever the layer already
        holds at that name is removed first. When the layer is applied, the
        entry then replaces whatever the image has at that name (a link, a
        file, or a directory), the way docker and overlay layering do it.
        The parent is checked to be inside the layer before anything is
        removed, and the target is checked again before it is written.
        """
        self._assert_within_layer(layer, target.parent)
        self._remove_existing(target, mode_fixups)
        self._assert_within_layer(layer, target)
        if src.is_symlink():
            os.symlink(os.readlink(src), target)
        else:
            shutil.copy2(src, target, follow_symlinks=False)

    def _copy_tree_into(self, src: Path, image: Image, dest_rel: str, layer: Path,
                         mode_fixups: list[tuple[Path, int]]) -> None:
        """Merge the contents of a source directory into the guest directory at ``dest_rel``.

        ``dest_rel`` is already symlink-resolved and already exists in the
        layer. A directory entry resolves its own path through the image's
        symlinks again (``resolve_in_rootfs``), so a link at any nesting
        level, such as ``/bin -> usr/bin``, still redirects the merge to its
        target. A file or symlink entry resolves only its parent, which is
        ``dest_rel``, and keeps its own name (see ``_place_non_dir``). A
        later source, or a later entry in the same source, always wins: a
        directory entry replaces a non-directory in its way (via
        ``_materialize_dir``), and a file or symlink replaces whatever is at
        its name.
        """
        for entry in sorted(os.scandir(src), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False):
                child_rel_raw = f"{dest_rel}/{entry.name}" if dest_rel else entry.name
                resolved_child_rel = resolve_in_rootfs(image.rootfs, "/" + child_rel_raw).lstrip("/")
                # _materialize_dir queues this directory's own mode fixup (using
                # the rootfs's existing mode when there is one, else this
                # entry's own mode) only the first time it creates it; a
                # second source merging into the same already-materialized
                # directory leaves its mode alone.
                mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
                self._materialize_dir(layer, resolved_child_rel, image, mode, mode_fixups)
                self._copy_tree_into(Path(entry.path), image, resolved_child_rel, layer, mode_fixups)
                continue
            parent_dir = self._materialize_dir(layer, dest_rel, image, 0o755, mode_fixups) if dest_rel else layer
            self._place_non_dir(layer, Path(entry.path), parent_dir / entry.name, mode_fixups)

    def _copy_step(self, image: Image, sources: list[tuple[str, Path]], dest: str, text: str,
                   ins: Instruction) -> Image:
        workdir = image.config.get("config", {}).get("WorkingDir") or "/"
        dest_abs = dest if dest.startswith("/") else os.path.join(workdir, dest)
        # Resolve the destination as an absolute guest path: normalize (so ".."
        # can never climb above "/", matching docker) and follow any existing
        # symlink component to where it actually points inside the rootfs,
        # never onto the host (see resolve_in_rootfs).
        resolved_dest = resolve_in_rootfs(image.rootfs, dest_abs)
        dest_rel = resolved_dest.lstrip("/")
        dest_dir_form = dest == "." or dest.endswith("/") or dest.endswith("/.")
        dest_exists_as_dir = rootfs_dir_mode(image.rootfs, resolved_dest) is not None
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
            for name, src in sources:
                if src.is_dir():
                    # A directory source always copies its contents, never nested
                    # under its own name, regardless of the destination's form.
                    target_dir = self._materialize_dir(layer, dest_rel, image, self._src_mode(src), mode_fixups)
                    self._assert_within_layer(layer, target_dir)
                    self._copy_tree_into(src, image, dest_rel, layer, mode_fixups)
                elif dest_is_dir:
                    target_dir = self._materialize_dir(layer, dest_rel, image, 0o755, mode_fixups)
                    self._place_non_dir(layer, src, target_dir / name, mode_fixups)
                else:
                    # A file destination resolves only its parent through the
                    # image's symlinks. Its own name is kept, so a link already
                    # at that name is replaced, never written through.
                    parent_guest, dest_name = posixpath.split(posixpath.normpath("/" + dest_abs.lstrip("/")))
                    parent_rel = resolve_in_rootfs(image.rootfs, parent_guest).lstrip("/")
                    parent_dir = (self._materialize_dir(layer, parent_rel, image, 0o755, mode_fixups)
                                  if parent_rel else layer)
                    self._place_non_dir(layer, src, parent_dir / dest_name, mode_fixups)
            self._apply_mode_fixups(layer, mode_fixups)
            return self.rt.images.commit(image, layer, created_by=f"{ins.name} {text}")
        finally:
            # Tolerant removal: a context directory with mode 0555 is copied
            # with that mode, and a plain rmtree cannot empty it.
            _rmtree_tolerant(work)

    # -- RUN -------------------------------------------------------------------

    def _run_step(self, image: Image, argv: list[str], args: Mapping[str, str], text: str) -> Image:
        # An ARG is only exposed as a RUN environment variable when no ENV of
        # the same name already won that name for the image: ENV always wins.
        env_keys = {item.partition("=")[0] for item in (image.config.get("config", {}).get("Env") or [])}
        run_env = {k: v for k, v in args.items() if k not in env_keys}
        # pull="never": the step image is in the store, and the build holds
        # ``store`` shared, so prune cannot remove it. A missing image is a
        # bug to report, never a reason to ask a registry for "<hex>".
        c = self.rt.create(image.id, command=argv, pull="never")
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
            return self._workdir_step(image, raw_path, text), shell
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
        config-only commit like the other metadata instructions. The stored
        ``WorkingDir`` is the normalized (".." clamped at "/") but not
        symlink-resolved path, matching docker's own config value; the
        filesystem check and any materialization use the symlink-resolved
        path, so WORKDIR through an existing symlink lands on its target,
        never wipes it out.
        """
        normalized = posixpath.normpath(path)
        changes = {"WorkingDir": normalized}
        resolved = resolve_in_rootfs(image.rootfs, path)
        if rootfs_exists(image.rootfs, resolved):
            return self.rt.images.commit(image, None, changes=changes, created_by=f"WORKDIR {text}")
        work = Path(tempfile.mkdtemp(prefix="workdir-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            layer.mkdir()
            mode_fixups: list[tuple[Path, int]] = []
            target_dir = self._materialize_dir(layer, resolved.lstrip("/"), image, 0o755, mode_fixups)
            self._assert_within_layer(layer, target_dir)
            self._apply_mode_fixups(layer, mode_fixups)
            return self.rt.images.commit(image, layer, changes=changes, created_by=f"WORKDIR {text}")
        finally:
            # Tolerant removal: a context directory with mode 0555 is copied
            # with that mode, and a plain rmtree cannot empty it.
            _rmtree_tolerant(work)
