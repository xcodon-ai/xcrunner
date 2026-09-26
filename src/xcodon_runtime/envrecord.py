"""The project's environment record, <env folder>/environment.json. See spec section 13."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from xcodon_runtime import __version__
from xcodon_runtime.envdir import ENV_INFO_NAME
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.pkgscan import Pkg, changes, ns_layer_packages, read_bytes, scan_tree

log = logging.getLogger(__name__)

RECORD_NAME = "environment.json"
LOCK_NAME = ".environment.lock"
VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCES = {"registry": "registry", "daemon": "daemon", "commit": "build"}
PACKAGES_DIR = "packages"
# Read caps. Files in the env folder can be changed from inside a container
# that has the project mounted, so every read here is capped and refuses
# symlinks, FIFOs and devices (see pkgscan.read_bytes).
_MAX_SMALL = 1 << 20
_MAX_EXPLICIT = 64 << 20
MAX_RECORD = 64 << 20


def _json_dict(data: bytes | None) -> dict | None:
    if data is None:
        return None
    try:
        d = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _atomic_write(path: Path, data: bytes, prefix: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def empty() -> dict:
    return {"version": VERSION, "images": {}, "layers": {}, "conda": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_doc(path: Path) -> dict | None:
    """The raw stored document, unknown keys included, or None if absent/unreadable."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("ignoring unreadable environment record %s: %s", path, e)
        return None
    data = read_bytes(path, MAX_RECORD) if stat.S_ISREG(st.st_mode) else None
    if data is None:
        log.warning("ignoring environment record %s: not a readable regular file", path)
        return None
    try:
        doc = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError) as e:
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
        doc["xrunner"] = __version__
        _atomic_write(path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(), ".environment-")
    return path


def _hex(image_id) -> str | None:
    return image_id.split(":", 1)[-1] if isinstance(image_id, str) and image_id else None


def _platform(config: dict) -> str | None:
    if not isinstance(config, dict) or not config.get("os") or not config.get("architecture"):
        return None
    out = f"{config['os']}/{config['architecture']}"
    return out + f"/{config['variant']}" if config.get("variant") else out


def packages_doc(image, pkgs: list[Pkg]) -> bytes:
    """The content of ``packages/<hex>.json``: the image's full package list, sorted, one image one content."""
    items = []
    for p in sorted(pkgs, key=lambda p: p.key):
        e = {"manager": p.manager, "name": p.name, "version": p.version, "location": p.location}
        if p.url:
            e["url"] = p.url
        items.append(e)
    doc = {"version": 1, "image": "sha256:" + image.id, "packages": items}
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()


def write_packages_file(env_dir: Path, image, pkgs: list[Pkg]) -> str:
    """Write ``<env folder>/packages/<hex>.json`` when it is missing or differs; return its project-relative path."""
    env_dir = Path(env_dir)
    path = env_dir / PACKAGES_DIR / f"{image.id}.json"
    data = packages_doc(image, pkgs)
    if read_bytes(path, len(data) + 1) != data:
        path.parent.mkdir(exist_ok=True)
        _atomic_write(path, data, ".packages-")
    return f"{env_dir.name}/{PACKAGES_DIR}/{image.id}.json"


def build_base(store, image, manifest: dict | None = None):
    """The image a ``build``/``commit`` image was made from, or None when it is not in the store.

    A build writes one image per step, each with the previous step as its
    ``parent``, and records the FROM image as ``base`` on the final one. For
    images without ``base`` (built before it was recorded), follow ``parent``
    through the intermediate step images (``commit`` images with no
    Dockerfile) to the first image that is not one.
    """
    m = manifest if manifest is not None else store.manifest(image)
    if m.get("source") != "commit" or not _hex(m.get("parent")):
        return None
    if _hex(m.get("base")):
        base = store.get(_hex(m["base"]))
        if base is not None:
            return base
    seen = {image.id}
    cur = store.get(_hex(m["parent"]))
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        cm = store.manifest(cur)
        up = _hex(cm.get("parent"))
        if cm.get("source") != "commit" or cm.get("dockerfile") or not up:
            return cur
        nxt = store.get(up)
        if nxt is None:
            return cur
        cur = nxt
    return cur


def image_entry(store, image, env_dir: Path | None = None) -> dict:
    """The ``images`` value for one image. With ``env_dir``, also writes its ``packages/<hex>.json``."""
    m = store.manifest(image)
    source = m.get("source") or "unknown"
    e = {"id": "sha256:" + image.id, "source": _SOURCES.get(source, source),
         "repo_digests": sorted({d for d in m.get("repo_digests", []) if isinstance(d, str)})}
    platform = _platform(image.config)
    if platform:
        e["platform"] = platform
    if isinstance(m.get("daemon_id"), str) and m["daemon_id"]:
        e["daemon_id"] = m["daemon_id"]
    if m.get("dockerfile"):
        e["dockerfile"] = m["dockerfile"]
    if _hex(m.get("parent")):
        e["parent"] = "sha256:" + _hex(m["parent"])
    pkgs = store.package_inventory(image)
    counts: dict[str, int] = {}
    for p in pkgs:
        counts[p.manager] = counts.get(p.manager, 0) + 1
    e["package_counts"] = dict(sorted(counts.items()))
    if env_dir is not None:
        e["packages_file"] = write_packages_file(env_dir, image, pkgs)
    base = build_base(store, image, m)
    if base is not None:
        e["packages"] = changes(store.package_inventory(base), pkgs)
        if e.get("parent") != "sha256:" + base.id:
            e["base"] = "sha256:" + base.id
    return e


def note_image(env_dir: Path, ref: str, image, store, err: TextIO | None = None) -> None:
    env_dir = Path(env_dir)
    new = image_entry(store, image, env_dir)
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
    return _json_dict(read_bytes(layer / ENV_INFO_NAME, _MAX_SMALL)) or {}


def _layer_entry(env_dir: Path, image_id: str, store) -> tuple[str, dict | None]:
    """What the ``layers`` entry for one folder should become: ("set", entry), ("drop", None) or ("keep", None)."""
    layer = Path(env_dir) / image_id
    has_upper = (layer / "upper").is_dir()
    has_rootfs = (layer / "rootfs").is_dir()
    if not has_upper and not has_rootfs:
        # No writable layer left for this folder (or it never had one): drop
        # any stale entry rather than keep describing packages that no
        # longer have a layer behind them.
        return "drop", None
    info = _layer_info(layer)
    image = store.get(image_id)
    if image is None:
        log.warning("image %s of env layer %s is not in the image store; skipping its packages",
                    image_id[:12], layer)
        return "keep", None
    base = store.package_inventory(image)
    if has_upper:
        merged, engine = ns_layer_packages(layer / "upper", base), "ns"
    else:
        merged, engine = scan_tree(layer / "rootfs"), "proot"
    ref = info.get("image_ref")
    return "set", {"ref": ref if isinstance(ref, str) else None,
                   "engine": info.get("engine") if isinstance(info.get("engine"), str) else engine,
                   "packages": changes(base, merged)}


def record_layer(env_dir: Path, image_id: str, store) -> None:
    env_dir = Path(env_dir)
    action, entry = _layer_entry(env_dir, image_id, store)
    if action == "drop":
        update(env_dir, lambda doc: doc["layers"].pop(image_id, None))
    elif action == "set":
        update(env_dir, lambda doc: doc["layers"].__setitem__(image_id, entry))


def _explicit_entry(prefix: Path, project: Path) -> dict | None:
    f = prefix / "conda-explicit.txt"
    data = read_bytes(f, _MAX_EXPLICIT)
    if data is None:
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


def _conda_entries(env_dir: Path) -> dict[str, dict]:
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
    registry = read_bytes(root / ".home" / ".conda" / "environments.txt", _MAX_SMALL)
    listed = [Path(line.strip()) for line in (registry or b"").decode("utf-8", errors="replace").splitlines()
              if line.strip() and Path(line.strip()).is_absolute()]
    for prefix in listed:
        if not _inside(prefix, project) or _inside(prefix, root):
            continue
        prefix = prefix.resolve()
        e = _explicit_entry(prefix, project)
        if e:
            entries[f"path:{os.path.relpath(prefix, project)}"] = e
    return entries


def record_conda(env_dir: Path) -> None:
    entries = _conda_entries(env_dir)
    update(env_dir, lambda doc: doc.__setitem__("conda", entries))


def _layer_folders(env_dir: Path) -> list[Path]:
    if not env_dir.is_dir():
        return []
    return sorted(d for d in env_dir.iterdir() if _HEX64.match(d.name) and d.is_dir() and not d.is_symlink())


def _after_scan() -> None:
    """Called between record_all's scan and its write. A test hook; does nothing."""


def _merge_ref(doc: dict, ref: str, items: list[tuple[str, str]], entries: dict, now: str) -> None:
    """Refresh one ``images`` entry in ``doc``. Never removes it: the images section is history.

    ``items`` are (first_used, hex) of this ref's layer folders, oldest
    first; ``entries`` maps hex to the fresh image entry (None when that image
    is not in the store). The record's current id stays current whenever its
    image is in the store. Only a ref with no entry yet, or whose recorded
    image is gone, moves to its newest folder whose image is in the store.
    """
    cur = doc["images"].get(ref)
    cur = cur if isinstance(cur, dict) else {}
    cur_id = cur.get("id") if isinstance(cur.get("id"), str) else None
    cur_hex = _hex(cur_id)
    candidates = [hexid for _, hexid in items]
    prev = [p for p in cur.get("previous_ids", []) if isinstance(p, str)] \
        if isinstance(cur.get("previous_ids"), list) else []
    if cur_hex and cur_hex not in entries:
        return  # written after the scan (a concurrent create): leave it as it is
    if cur_hex and entries[cur_hex] is not None:
        current, keep = cur_hex, True
    else:
        current = next((h for h in reversed(candidates) if entries.get(h) is not None), None)
        keep = False
        if current is None:
            return  # nothing usable in the store for this ref; keep the entry as history
    entry = dict(entries[current])
    for hexid in candidates:
        sid = "sha256:" + hexid
        if hexid != current and sid not in prev:
            prev.append(sid)
    if not keep and cur_id and cur_id not in prev:
        prev.append(cur_id)
    entry["previous_ids"] = [p for p in prev if p != entry["id"]]
    if keep:
        entry["first_used"] = cur.get("first_used") or now
        entry["last_used"] = cur.get("last_used") or entry["first_used"]
    else:
        # A ref that switches ids starts a fresh last_used from the new
        # entry's first_used, never the old id's last_used.
        entry["first_used"] = next((fu for fu, hx in items if hx == current and fu), "") or now
        entry["last_used"] = entry["first_used"]
    doc["images"][ref] = entry


def record_all(env_dir: Path, store) -> Path:
    """Rebuild ``layers`` and ``conda`` from what is on disk, and refresh ``images``.

    The scans run outside the lock; one locked update then writes the
    result. ``images`` entries are history and are never removed. A
    ``layers`` entry is dropped only when its folder is gone at write time,
    so a folder created during the scan keeps whatever entry it got.
    """
    env_dir = Path(env_dir)
    folders = _layer_folders(env_dir)
    recorded = load(env_dir)["images"]
    by_ref: dict[str, list[tuple[str, str]]] = {}
    for d in folders:
        info = _layer_info(d)
        ref = info.get("image_ref")
        if isinstance(ref, str) and ref:
            first = info.get("first_used")
            by_ref.setdefault(ref, []).append((first if isinstance(first, str) else "", d.name))
    for items in by_ref.values():
        items.sort()

    entries: dict[str, dict | None] = {}

    def entry_for(hexid: str) -> dict | None:
        if hexid not in entries:
            image = store.get(hexid)
            entries[hexid] = image_entry(store, image, env_dir) if image is not None else None
        return entries[hexid]

    refs = sorted(set(by_ref) | {r for r, e in recorded.items() if isinstance(e, dict)})
    for ref in refs:
        cur = recorded.get(ref)
        cur_hex = _hex(cur.get("id")) if isinstance(cur, dict) else None
        if cur_hex and entry_for(cur_hex) is not None:
            continue
        for _, hexid in reversed(by_ref.get(ref, [])):
            if entry_for(hexid) is not None:
                break
    layer_entries = {d.name: _layer_entry(env_dir, d.name, store) for d in folders}
    conda = _conda_entries(env_dir)
    _after_scan()
    now = _now()

    def fn(doc: dict) -> None:
        present = {d.name for d in _layer_folders(env_dir)}
        for hexid in list(doc["layers"]):
            if hexid not in present:
                del doc["layers"][hexid]
        for hexid, (action, entry) in layer_entries.items():
            if hexid not in present:
                continue
            if action == "set":
                doc["layers"][hexid] = entry
            elif action == "drop":
                doc["layers"].pop(hexid, None)
        doc["conda"] = conda
        for ref in refs:
            _merge_ref(doc, ref, by_ref.get(ref, []), entries, now)

    return update(env_dir, fn)


def _short(image_id) -> str:
    return _hex(image_id)[:12] if _hex(image_id) else "?"


def _pkg_lines(pkgs, indent: str) -> list[str]:
    out = []
    for p in pkgs if isinstance(pkgs, list) else []:
        if isinstance(p, dict):
            out.append(f"{indent}{p.get('manager', '?')!s:<6} {p.get('name', '?')}  "
                       f"{p.get('change', '?')} {p.get('version', '?')}")
    return out


def show(env_dir: Path) -> str:
    doc = load(env_dir)
    out = [f"Environment record: {Path(env_dir) / RECORD_NAME}", "", "Images:"]
    for ref, e in sorted(doc["images"].items()):
        if not isinstance(e, dict):
            continue
        digests = e.get("repo_digests") if isinstance(e.get("repo_digests"), list) else []
        digest = digests[0] if digests else "no registry digest"
        out.append(f"  {ref}  {_short(e.get('id'))}  {e.get('source', '?')}  {digest}")
        counts = e.get("package_counts")
        if isinstance(counts, dict):
            listed = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
            out.append(f"    platform {e.get('platform', 'unknown')}; packages: {listed}")
        facts = ["Dockerfile on record" if e.get("dockerfile") else "no Dockerfile on record"]
        if e.get("parent"):
            facts.append(f"parent {_short(e['parent'])}")
        if e.get("base"):
            facts.append(f"built from {_short(e['base'])}")
        out.append("    " + "; ".join(facts))
        if isinstance(e.get("previous_ids"), list) and e["previous_ids"]:
            out.append(f"    earlier: {', '.join(_short(i) for i in e['previous_ids'])}")
        if "packages" in e:
            lines = _pkg_lines(e["packages"], "      ")
            out.append("    built-in package changes:" if lines else "    built-in package changes: none")
            out += lines
    out += ["", "Container layers:"]
    for hexid, e in sorted(doc["layers"].items()):
        if not isinstance(e, dict):
            continue
        out.append(f"  {hexid[:12]}  {e.get('ref')}  ({e.get('engine')})")
        lines = _pkg_lines(e.get("packages"), "    ")
        out += lines or ["    no package changes"]
    out += ["", "Conda environments:"]
    for key, e in sorted(doc["conda"].items()):
        if not isinstance(e, dict):
            continue
        out.append(f"  {key}  {e.get('packages')} packages  {e.get('explicit')}")
    return "\n".join(out) + "\n"
