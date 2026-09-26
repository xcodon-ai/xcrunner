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


def load(env_dir: Path) -> dict:
    path = Path(env_dir) / RECORD_NAME
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable environment record %s: %s", path, e)
        return empty()
    base = empty()
    if isinstance(doc, dict):
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
    env_dir = Path(env_dir)
    path = env_dir / RECORD_NAME
    with _locked(env_dir):
        doc = load(env_dir)
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
    info = _layer_info(layer)
    image = store.get(image_id)
    if image is None:
        log.warning("image %s of env layer %s is not in the image store; skipping its packages",
                    image_id[:12], layer)
        return
    base = store.package_inventory(image)
    if (layer / "upper").is_dir():
        merged, engine = ns_layer_packages(layer / "upper", base), "ns"
    elif (layer / "rootfs").is_dir():
        merged, engine = scan_tree(layer / "rootfs"), "proot"
    else:
        return
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
    env_dir = Path(env_dir)
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
        listed = [Path(line.strip()) for line in registry.read_text().splitlines() if line.strip()]
    except OSError:
        listed = []
    for prefix in listed:
        if not _inside(prefix, project) or _inside(prefix, root):
            continue
        e = _explicit_entry(prefix, project)
        if e:
            entries[f"path:{os.path.relpath(prefix, project)}"] = e
    update(env_dir, lambda doc: doc.__setitem__("conda", entries))


def record_all(env_dir: Path, store) -> Path:
    env_dir = Path(env_dir)
    layers = sorted(d for d in env_dir.iterdir() if d.is_dir() and _HEX64.match(d.name)) if env_dir.is_dir() else []
    by_ref: dict[str, list[tuple[str, str]]] = {}
    for d in layers:
        info = _layer_info(d)
        if info.get("image_ref"):
            by_ref.setdefault(info["image_ref"], []).append((info.get("first_used", ""), d.name))
    for ref, items in sorted(by_ref.items()):
        items.sort()
        current = store.get(items[-1][1])
        if current is None:
            continue
        older = ["sha256:" + hexid for _, hexid in items[:-1]]
        entry = image_entry(store, current)

        def fn(doc: dict, ref=ref, entry=entry, older=older, first=items[-1][0]) -> None:
            cur = doc["images"].get(ref, {})
            prev = list(cur.get("previous_ids", []))
            for old in older + ([cur["id"]] if cur.get("id") and cur["id"] != entry["id"] else []):
                if old not in prev and old != entry["id"]:
                    prev.append(old)
            entry["previous_ids"] = prev
            entry["first_used"] = cur.get("first_used") if cur.get("id") == entry["id"] else (first or _now())
            entry["last_used"] = cur.get("last_used") or entry["first_used"]
            doc["images"][ref] = entry

        update(env_dir, fn)
    for d in layers:
        record_layer(env_dir, d.name, store)
    record_conda(env_dir)
    return env_dir / RECORD_NAME


def show(env_dir: Path) -> str:
    doc = load(env_dir)
    out = [f"Environment record: {Path(env_dir) / RECORD_NAME}", "", "Images:"]
    for ref, e in sorted(doc["images"].items()):
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
