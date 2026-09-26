"""The project's environment record, <env folder>/environment.json. See spec section 13."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from xcodon_runtime.envdir import ENV_INFO_NAME
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.pkgscan import changes, ns_layer_packages, scan_tree

log = logging.getLogger(__name__)

RECORD_NAME = "environment.json"
LOCK_NAME = ".environment.lock"
VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCES = {"registry": "registry", "daemon": "daemon", "commit": "build"}


def empty() -> dict:
    return {"version": VERSION, "images": {}, "layers": {}, "conda": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_doc(path: Path) -> dict | None:
    """The raw stored document, unknown keys included, or None if absent/unreadable."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable environment record %s: %s", path, e)
        return None
    return doc if isinstance(doc, dict) else None


def load(env_dir: Path) -> dict:
    """The known sections, for display. Tolerates a missing, corrupt or newer-version file."""
    doc = _read_doc(Path(env_dir) / RECORD_NAME)
    base = empty()
    if doc:
        for key in ("images", "layers", "conda"):
            if isinstance(doc.get(key), dict):
                base[key] = doc[key]
    return base


@contextmanager
def _locked(env_dir: Path) -> Iterator[None]:
    fd = os.open(Path(env_dir) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def update(env_dir: Path, fn: Callable[[dict], None]) -> Path:
    """Load, mutate and atomically rewrite the record, under the env dir's lock.

    Unknown top-level keys in the stored file survive the round trip. A
    stored version newer than what this xrunner writes is refused outright,
    rather than silently downgraded.
    """
    env_dir = Path(env_dir)
    path = env_dir / RECORD_NAME
    with _locked(env_dir):
        doc = _read_doc(path) or {}
        version = doc.get("version")
        if isinstance(version, int) and version > VERSION:
            raise XcodonError(f"environment record {path} has version {version}; "
                               f"this xrunner writes version {VERSION}")
        for key in ("images", "layers", "conda"):
            if not isinstance(doc.get(key), dict):
                doc[key] = {}
        fn(doc)
        doc["version"] = VERSION
        fd, tmp = tempfile.mkstemp(dir=env_dir, prefix=".environment-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return path


def image_entry(store, image) -> dict:
    m = store.manifest(image)
    source = m.get("source") or "unknown"
    e = {"id": "sha256:" + image.id, "source": _SOURCES.get(source, source),
         "repo_digests": sorted(set(m.get("repo_digests", [])))}
    if m.get("dockerfile"):
        e["dockerfile"] = m["dockerfile"]
    if m.get("parent"):
        parent = m["parent"]
        e["parent"] = parent if parent.startswith("sha256:") else "sha256:" + parent
    return e


def note_image(env_dir: Path, ref: str, image, store, err: TextIO | None = None) -> None:
    env_dir = Path(env_dir)
    new = image_entry(store, image)
    now = _now()

    def fn(doc: dict) -> None:
        cur = doc["images"].get(ref)
        cur = cur if isinstance(cur, dict) else None
        if cur and cur.get("id") and cur["id"] != new["id"]:
            prev = list(cur.get("previous_ids", []))
            if cur["id"] not in prev:
                prev.append(cur["id"])
            old_hex = cur["id"].split(":", 1)[-1]
            print(f"xrunner: {ref} now points to {image.id[:12]}; installs made on {old_hex[:12]} "
                  f"stay in {env_dir.name}/{old_hex}", file=err or sys.stderr)
            new["previous_ids"] = prev
            new["first_used"] = now
        elif cur:
            new["previous_ids"] = list(cur.get("previous_ids", []))
            new["first_used"] = cur.get("first_used", now)
        else:
            new["previous_ids"] = []
            new["first_used"] = now
        new["previous_ids"] = [p for p in new["previous_ids"] if p != new["id"]]
        new["last_used"] = now
        doc["images"][ref] = new

    update(env_dir, fn)


def _layer_info(layer: Path) -> dict:
    try:
        return json.loads((layer / ENV_INFO_NAME).read_text())
    except (OSError, ValueError):
        return {}


def record_layer(env_dir: Path, image_id: str, store) -> None:
    env_dir = Path(env_dir)
    layer = env_dir / image_id
    has_upper = (layer / "upper").is_dir()
    has_rootfs = (layer / "rootfs").is_dir()
    if not has_upper and not has_rootfs:
        # No writable layer left for this folder (or it never had one): drop
        # any stale entry rather than keep describing packages that no
        # longer have a layer behind them.
        update(env_dir, lambda doc: doc["layers"].pop(image_id, None))
        return
    info = _layer_info(layer)
    image = store.get(image_id)
    if image is None:
        log.warning("image %s of env layer %s is not in the image store; skipping its packages",
                    image_id[:12], layer)
        return
    base = store.package_inventory(image)
    if has_upper:
        merged, engine = ns_layer_packages(layer / "upper", base), "ns"
    else:
        merged, engine = scan_tree(layer / "rootfs"), "proot"
    entry = {"ref": info.get("image_ref"), "engine": info.get("engine") or engine,
             "packages": changes(base, merged)}
    update(env_dir, lambda doc: doc["layers"].__setitem__(image_id, entry))


def _explicit_entry(prefix: Path, project: Path) -> dict | None:
    f = prefix / "conda-explicit.txt"
    try:
        data = f.read_bytes()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"explicit": os.path.relpath(f, project), "sha256": hashlib.sha256(data).hexdigest(),
            "packages": sum(1 for line in lines if line.startswith(("http://", "https://", "file://")))}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def record_conda(env_dir: Path) -> None:
    # Resolved up front: the project may be reached through a symlink (e.g. a
    # symlinked project directory), and an unresolved mix of real and
    # symlinked paths would otherwise produce a "path:" key full of "..".
    env_dir = Path(env_dir).resolve()
    project = env_dir.parent
    root = env_dir / "conda"
    entries: dict[str, dict] = {}
    base = _explicit_entry(root, project)
    if base:
        entries["name:base"] = base
    envs = root / "envs"
    if envs.is_dir():
        for d in sorted(envs.iterdir()):
            e = _explicit_entry(d, project) if d.is_dir() else None
            if e:
                entries[f"name:{d.name}"] = e
    registry = root / ".home" / ".conda" / "environments.txt"
    try:
        listed = [Path(line.strip()) for line in registry.read_text().splitlines()
                  if line.strip() and Path(line.strip()).is_absolute()]
    except OSError:
        listed = []
    for prefix in listed:
        if not _inside(prefix, project) or _inside(prefix, root):
            continue
        prefix = prefix.resolve()
        e = _explicit_entry(prefix, project)
        if e:
            entries[f"path:{os.path.relpath(prefix, project)}"] = e
    update(env_dir, lambda doc: doc.__setitem__("conda", entries))


def record_all(env_dir: Path, store) -> Path:
    """Rebuild every section from what is on disk right now.

    A layer folder that no longer exists loses its ``layers`` entry, and any
    ``images`` ref left with no surviving folder for its current id or any
    previous id is dropped entirely.
    """
    env_dir = Path(env_dir)
    layers = sorted(d for d in env_dir.iterdir() if d.is_dir() and _HEX64.match(d.name)) if env_dir.is_dir() else []
    present_ids = {d.name for d in layers}

    # Drop layer entries whose folder is gone before re-deriving the rest,
    # so a stale entry cannot survive a rebuild by sheer absence of change.
    update(env_dir, lambda doc: [doc["layers"].pop(k, None) for k in list(doc["layers"]) if k not in present_ids])

    by_ref: dict[str, list[tuple[str, str]]] = {}
    for d in layers:
        info = _layer_info(d)
        if info.get("image_ref"):
            by_ref.setdefault(info["image_ref"], []).append((info.get("first_used", ""), d.name))

    for ref, items in sorted(by_ref.items()):
        items.sort()
        candidates = [hexid for _, hexid in items]

        def fn(doc: dict, ref=ref, items=items, candidates=candidates) -> None:
            cur = doc["images"].get(ref)
            cur = cur if isinstance(cur, dict) else {}
            cur_hex = cur["id"].split(":", 1)[-1] if cur.get("id") else None
            keep_current = bool(cur_hex) and cur_hex in present_ids and cur_hex in candidates
            image = store.get(cur_hex) if keep_current else None
            keep_current = keep_current and image is not None
            current_hex = cur_hex if keep_current else None
            if not keep_current:
                # The recorded id has no folder left (or none was recorded
                # yet): fall back to the newest folder whose image is still
                # in the store, skipping any newer one that is not.
                for _, hexid in reversed(items):
                    candidate_image = store.get(hexid)
                    if candidate_image is not None:
                        image, current_hex = candidate_image, hexid
                        break
            if image is None:
                return  # nothing usable in the store for this ref; leave it alone
            entry = image_entry(store, image)
            prev = list(cur.get("previous_ids", [])) if isinstance(cur.get("previous_ids"), list) else []
            for hexid in candidates:
                sid = "sha256:" + hexid
                if hexid != current_hex and sid not in prev:
                    prev.append(sid)
            if not keep_current and cur.get("id") and cur["id"] not in prev:
                prev.append(cur["id"])
            entry["previous_ids"] = [p for p in prev if p != entry["id"]]
            if keep_current:
                entry["first_used"] = cur.get("first_used") or _now()
                entry["last_used"] = cur.get("last_used") or entry["first_used"]
            else:
                first = next((fu for fu, hx in items if hx == current_hex), "")
                entry["first_used"] = first or _now()
                # A ref that switches ids starts a fresh last_used from the
                # new entry's first_used, never the old id's last_used.
                entry["last_used"] = entry["first_used"]
            doc["images"][ref] = entry

        update(env_dir, fn)

    def _drop_orphaned_refs(doc: dict) -> None:
        for ref, e in list(doc["images"].items()):
            if not isinstance(e, dict):
                continue
            ids = [e.get("id")] + [p for p in e.get("previous_ids", []) if isinstance(p, str)]
            hexes = [i.split(":", 1)[-1] for i in ids if isinstance(i, str)]
            if not any(h in present_ids for h in hexes):
                del doc["images"][ref]

    update(env_dir, _drop_orphaned_refs)

    for d in layers:
        record_layer(env_dir, d.name, store)
    record_conda(env_dir)
    return env_dir / RECORD_NAME


def show(env_dir: Path) -> str:
    doc = load(env_dir)
    out = [f"Environment record: {Path(env_dir) / RECORD_NAME}", "", "Images:"]
    for ref, e in sorted(doc["images"].items()):
        if not isinstance(e, dict):
            continue
        digest = e.get("repo_digests", [None])[0] if e.get("repo_digests") else "no registry digest"
        out.append(f"  {ref}  {e.get('id', '?')[7:19]}  {e.get('source', '?')}  {digest}")
        if e.get("previous_ids"):
            out.append(f"    earlier: {', '.join(i[7:19] for i in e['previous_ids'])}")
    out += ["", "Container layers:"]
    for hexid, e in sorted(doc["layers"].items()):
        out.append(f"  {hexid[:12]}  {e.get('ref')}  ({e.get('engine')})")
        pkgs = e.get("packages", [])
        if not pkgs:
            out.append("    no package changes")
        for p in pkgs:
            out.append(f"    {p['manager']:<6} {p['name']}  {p['change']} {p['version']}")
    out += ["", "Conda environments:"]
    for key, e in sorted(doc["conda"].items()):
        out.append(f"  {key}  {e.get('packages')} packages  {e.get('explicit')}")
    return "\n".join(out) + "\n"
