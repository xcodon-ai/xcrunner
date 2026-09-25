"""Docker-style word expansion and symlink-safe guest-path resolution for ``build.py``.

Split out of ``build.py`` to keep that module focused on executing Dockerfile
instructions: this one only knows about splitting/expanding text and about
resolving a path inside an image rootfs without ever following a symlink
onto the host.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
from pathlib import Path
from typing import Mapping

from xcodon_runtime.errors import XcodonError

MAX_SYMLINK_HOPS = 40
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# -- word splitting / expansion -----------------------------------------------

def _expand_dollar(text: str, i: int, scope: Mapping[str, str]) -> tuple[str, int]:
    """``text[i] == '$'``. Returns (expanded value, index just past the reference).

    A ``$`` not followed by a name or ``{`` is left as a literal ``$`` (index
    advances by 1 only). ``${NAME:-word}`` and ``${NAME:+word}``'s ``word`` is
    itself run through the same expansion (see ``_process_run``), joined back
    into one string -- "keep it simple: expand variables in it and strip
    quotes," not a fully general nested grammar. Any other ``${...}`` form,
    such as ``${}`` or ``${NAME junk}``, raises ``XcodonError``, like
    docker's "bad substitution" error.
    """
    j = i + 1
    if j < len(text) and text[j] == "{":
        end = text.find("}", j)
        if end == -1:
            raise XcodonError(f"unclosed '{{' in {text!r}")
        inner = text[j + 1 : end]
        m = _NAME_RE.match(inner)
        if not m:
            raise XcodonError(f"bad substitution '${{{inner}}}' in {text!r}")
        name = m.group(0)
        rest = inner[len(name) :]
        if rest.startswith(":-") or rest.startswith(":+"):
            op = rest[:2]
            word, _ = _process_run(rest[2:], 0, scope, stop_at_space=False)
            value = scope.get(name)
            if op == ":-":
                return (value if value else word), end + 1
            return (word if value else ""), end + 1
        if rest:
            raise XcodonError(f"bad substitution '${{{inner}}}' in {text!r}")
        return scope.get(name, ""), end + 1
    m = _NAME_RE.match(text, j)
    if not m:
        return "$", i + 1
    return scope.get(m.group(0), ""), m.end()


def _process_run(text: str, start: int, scope: Mapping[str, str], stop_at_space: bool) -> tuple[str, int]:
    """Process ``text`` from ``start``, handling quotes/escapes/``$`` expansion.

    Docker's shell-word rules: ``'...'`` is literal (no expansion, backslash
    literal) until the next ``'``; ``"..."`` expands ``$`` inside it, where a
    backslash escapes only ``"``, ``\\``, ``$``, and newline, and stays
    literal before any other character; outside quotes, a backslash makes the
    next character literal (so ``\\$x`` gives ``$x`` and ``\\ `` joins words).
    If ``stop_at_space``, stops (without consuming) at the first unquoted
    whitespace; otherwise consumes to the end of ``text``. An unclosed quote
    raises ``XcodonError``.
    """
    buf: list[str] = []
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if stop_at_space and c in " \t\n":
            break
        if c == "'":
            i += 1
            j = text.find("'", i)
            if j == -1:
                raise XcodonError(f"unclosed \"'\" in {text!r}")
            buf.append(text[i:j])
            i = j + 1
            continue
        if c == '"':
            i += 1
            while True:
                if i >= n:
                    raise XcodonError(f'unclosed \'"\' in {text!r}')
                c2 = text[i]
                if c2 == '"':
                    i += 1
                    break
                if c2 == "\\" and i + 1 < n and text[i + 1] in ('"', "\\", "$", "\n"):
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if c2 == "$":
                    val, i = _expand_dollar(text, i, scope)
                    buf.append(val)
                    continue
                buf.append(c2)
                i += 1
            continue
        if c == "\\":
            if i + 1 < n:
                buf.append(text[i + 1])
                i += 2
            else:
                buf.append(c)
                i += 1
            continue
        if c == "$":
            val, i = _expand_dollar(text, i, scope)
            buf.append(val)
            continue
        buf.append(c)
        i += 1
    return "".join(buf), i


def split_words(text: str, scope: Mapping[str, str]) -> list[str]:
    """Split ``text`` into docker-style shell words, expanding ``$VAR`` as it goes."""
    words: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in " \t\n":
            i += 1
            continue
        word, i = _process_run(text, i, scope, stop_at_space=True)
        words.append(word)
    return words


def expand_args(text: str, args: Mapping[str, str]) -> str:
    """Expand ``$NAME``, ``${NAME}``, ``${NAME:-default}``, and ``${NAME:+word}``.

    Plain substitution with no shell-quote handling (a backslash right
    before ``$`` still escapes it to a literal ``$``) -- for text that is
    already a single token with no Dockerfile-level quoting to worry about,
    such as one element of a COPY/ADD JSON array. Use ``split_words`` for
    anything that still needs word-splitting.
    """
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] == "$":
            buf.append("$")
            i += 2
            continue
        if c == "$":
            val, i = _expand_dollar(text, i, args)
            buf.append(val)
            continue
        buf.append(c)
        i += 1
    return "".join(buf)


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
    (a symlink loop), or when a path component turns out not to be a
    directory at all (e.g. resolving through an existing regular file),
    matching docker's own error for that case.
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
        except NotADirectoryError as e:
            raise XcodonError(
                f"cannot resolve {guest!r} in the image: a path component is not a directory"
            ) from e
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


def rootfs_lstat(rootfs: Path, guest: str) -> os.stat_result | None:
    """``lstat`` of a guest path, resolved symlink-safely first. None if it does not exist."""
    resolved = resolve_in_rootfs(rootfs, guest)
    try:
        return (rootfs / resolved.lstrip("/")).lstat()
    except OSError:
        return None


def rootfs_dir_mode(rootfs: Path, guest: str) -> int | None:
    """The mode of ``guest`` in ``rootfs`` if it exists there as a directory, else None."""
    st = rootfs_lstat(rootfs, guest)
    if st is not None and stat.S_ISDIR(st.st_mode):
        return stat.S_IMODE(st.st_mode)
    return None


def rootfs_exists(rootfs: Path, guest: str) -> bool:
    return rootfs_lstat(rootfs, guest) is not None
