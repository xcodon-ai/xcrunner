"""Node-local container and env-layer folders, chosen when the default ones cannot hold overlays.

On a cluster the runtime home (``~/.xcodon/runtime``) usually sits on NFS. An overlay upper layer
cannot live there, so the overlay probe fails and xcrunner falls back to PRoot, which is slower
and copies a whole root filesystem per container. The same node's ``/tmp`` usually can: on
cpn-c08-22-01, ``XCRUNNER_CONTAINER_DIR=/tmp/xcrunner-$USER/containers`` turned proot into ns.
Setting that by hand at ``xcodon init`` would not work either: init runs on a login node, the
agent on a compute node, and ``/tmp`` is a different disk on each.

So the choice is made where containers run, each time a ``Runtime`` starts: when neither folder
is set and the default container folder fails the overlay probe, the first node-local candidate
that passes it — a private ``xcrunner-<user>`` folder under ``/tmp``, then ``$TMPDIR`` — becomes
``XCRUNNER_CONTAINER_DIR`` and ``XCRUNNER_ENV_LAYER_DIR`` for this process and its children. The
image store stays where it is. The decision is cached per boot in that private folder, so only
the first process after a reboot pays for the probes. ``XCRUNNER_AUTO_LOCAL_DIRS=0`` turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import shutil
import stat
import sys
from pathlib import Path

from xcodon_runtime.envdir import ENV_LAYER_DIR_ENV
from xcodon_runtime.home import CONTAINER_DIR_ENV, RuntimeHome

log = logging.getLogger(__name__)

AUTO_ENV = "XCRUNNER_AUTO_LOCAL_DIRS"
_CACHE_NAME = "auto-dirs.json"
_MIN_FREE_BYTES = 10 << 30  # a container's upper layer plus an env layer of installed packages


def _user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _candidates() -> list[Path]:
    """Parents of the private folder, nearest-to-the-node first, each once."""
    out: list[Path] = []
    for p in (Path("/tmp"), Path(os.environ.get("TMPDIR") or "/tmp")):
        if p.is_dir() and p.resolve() not in [o.resolve() for o in out]:
            out.append(p)
    return out


def _private_dir(parent: Path) -> Path | None:
    """``<parent>/xcrunner-<user>``, created 0700; ``None`` unless it is ours alone.

    ``/tmp`` is shared: another user's folder (or a symlink) under our name would otherwise
    receive our containers.
    """
    d = parent / f"xcrunner-{_user()}"
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return d


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _overlay_ok(home_path: Path, containers: Path) -> bool:
    from xcodon_runtime.probe import run_probe

    try:
        containers.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return bool(run_probe("overlay", home_path, containers)["ok"])


def _free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _decide(home: RuntimeHome, private: Path) -> tuple[bool, str]:
    """``(default works, local base to use or "")``."""
    if _overlay_ok(home.path, home.containers):
        return True, ""
    if _free(private) < _MIN_FREE_BYTES:
        log.info("%s has under %d GB free; not using it", private, _MIN_FREE_BYTES >> 30)
        return False, ""
    return False, str(private) if _overlay_ok(home.path, private / "containers") else ""


def choose_local_dirs(home: RuntimeHome) -> str:
    """Point the container and env-layer folders at a node-local disk when that turns PRoot
    into ns. Returns a one-line note when it did, else ``""``."""
    if os.environ.get(AUTO_ENV, "1").strip() == "0" or not sys.platform.startswith("linux"):
        return ""
    if os.environ.get(CONTAINER_DIR_ENV) or os.environ.get("XCODON_ENGINE") in ("proot", "udocker"):
        return ""
    for parent in _candidates():
        private = _private_dir(parent)
        if private is None:
            continue
        key = f"{_boot_id()}|{home.path}|{home.containers}"
        cache = private / _CACHE_NAME
        try:
            cached = json.loads(cache.read_text())
        except (OSError, ValueError):
            cached = {}
        if isinstance(cached, dict) and cached.get("key") == key:
            default_ok, base = bool(cached.get("default_ok")), str(cached.get("base") or "")
        else:
            default_ok, base = _decide(home, private)
            try:
                tmp = cache.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"key": key, "default_ok": default_ok, "base": base}))
                os.replace(tmp, cache)
            except OSError:
                pass
        if default_ok:
            return ""
        if not base:
            continue
        os.environ[CONTAINER_DIR_ENV] = str(Path(base) / "containers")
        os.environ.setdefault(ENV_LAYER_DIR_ENV, str(Path(base) / "env-layers"))
        note = (f"container dir {os.environ[CONTAINER_DIR_ENV]} (auto: {home.containers} cannot hold "
                f"overlay layers; set {CONTAINER_DIR_ENV} to choose, or {AUTO_ENV}=0 to keep it)")
        log.info("%s", note)
        return note
    return ""
