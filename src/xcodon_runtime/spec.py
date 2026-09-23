"""Merge image config and run options into the process to start."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from xcodon_runtime.errors import XcodonError

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class ProcessSpec:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/"
    uid: int = 0
    gid: int = 0


def _read_db(path: Path) -> list[list[str]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [line.split(":") for line in lines if line and not line.startswith("#")]


def _passwd_lookup(rootfs: Path, key: str) -> list[str] | None:
    for row in _read_db(rootfs / "etc" / "passwd"):
        if len(row) >= 7 and (row[0] == key or row[2] == key):
            return row
    return None


def _group_lookup(rootfs: Path, key: str) -> list[str] | None:
    for row in _read_db(rootfs / "etc" / "group"):
        if len(row) >= 3 and (row[0] == key or row[2] == key):
            return row
    return None


def resolve_user(user: str | None, rootfs: Path) -> tuple[int, int]:
    """Turn ``name``, ``uid``, ``name:group``, or ``uid:gid`` into numbers using the rootfs databases."""
    if not user:
        return 0, 0
    user_part, _, group_part = user.partition(":")
    pw = _passwd_lookup(rootfs, user_part)
    if pw is not None:
        uid, default_gid = int(pw[2]), int(pw[3])
    elif user_part.isdigit():
        uid, default_gid = int(user_part), int(user_part)
    else:
        raise XcodonError(f"unknown user {user_part!r} in image /etc/passwd")
    if not group_part:
        return uid, default_gid
    gr = _group_lookup(rootfs, group_part)
    if gr is not None:
        return uid, int(gr[2])
    if group_part.isdigit():
        return uid, int(group_part)
    raise XcodonError(f"unknown group {group_part!r} in image /etc/group")


def _home_for(uid: int, rootfs: Path) -> str:
    pw = _passwd_lookup(rootfs, str(uid))
    if pw is not None and pw[5]:
        return pw[5]
    return "/root" if uid == 0 else "/"


def build_spec(
    image_config: dict,
    rootfs: Path,
    container_id: str,
    command: Sequence[str] | None = None,
    entrypoint: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    user: str | None = None,
) -> ProcessSpec:
    cfg = image_config.get("config") or {}

    ep = list(entrypoint) if entrypoint is not None else list(cfg.get("Entrypoint") or [])
    cmd = list(command) if command is not None else list(cfg.get("Cmd") or [])
    if entrypoint is not None and entrypoint and command is None:
        cmd = []  # a new entrypoint discards the image CMD, as docker does
    argv = ep + cmd
    if not argv:
        raise XcodonError("no command: the image has no ENTRYPOINT or CMD and none was given")

    uid, gid = resolve_user(user if user is not None else cfg.get("User"), rootfs)

    merged: dict[str, str] = {}
    for item in cfg.get("Env") or []:
        k, _, v = item.partition("=")
        merged[k] = v
    merged["HOSTNAME"] = container_id[:12]
    merged.setdefault("HOME", _home_for(uid, rootfs))
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    merged.setdefault("PATH", DEFAULT_PATH)

    wd = workdir or cfg.get("WorkingDir") or "/"
    if not wd.startswith("/"):
        wd = "/" + wd
    return ProcessSpec(argv=argv, env=merged, workdir=wd, uid=uid, gid=gid)
