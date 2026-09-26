"""Find installed packages from their metadata files. See spec section 13.3.

Nothing here runs a command in a container: packages are read from pip,
R, dpkg and conda metadata files, so a stopped layer can be read, on either
engine, and code in an image cannot steer the result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

PIP_PARENTS = ("site-packages", "dist-packages")
DPKG_STATUS = "/var/lib/dpkg/status"
OPAQUE_XATTRS = ("user.overlay.opaque", "trusted.overlay.opaque")
_SKIP_TOP = {"proc", "sys", "dev"}
_MAX_META = 1 << 20
_MAX_DPKG = 256 << 20  # 256 MiB for dpkg status


def _norm_name(name: str) -> str:
    """PEP 503 normalization: replace runs of -, _, . with single -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _natural_sort_key(version: str) -> tuple:
    """Split version on digit boundaries for natural sorting."""
    parts = []
    for part in re.split(r"(\d+)", version):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


@dataclass(frozen=True)
class Pkg:
    manager: str
    name: str
    version: str
    location: str
    path: str
    url: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        norm_name = _norm_name(self.name) if self.manager == "pip" else self.name
        return (self.manager, self.location, norm_name)


def pkg_to_json(p: Pkg) -> dict:
    return asdict(p)


def pkg_from_json(d: dict) -> Pkg:
    return Pkg(d["manager"], d["name"], d["version"], d["location"], d["path"], d.get("url"))


def is_whiteout(path: Path, st: os.stat_result) -> bool:
    """An overlay whiteout: a character device 0:0."""
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == 0


def is_opaque(path: Path) -> bool:
    for name in OPAQUE_XATTRS:
        try:
            if os.getxattr(path, name, follow_symlinks=False) == b"y":
                return True
        except OSError:
            continue
    return False


def _read(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_META).decode("utf-8-sig", errors="replace")
    except OSError:
        return None


def _read_large(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_DPKG).decode("utf-8-sig", errors="replace")
    except OSError:
        return None


def _headers(text: str | None) -> dict[str, str]:
    """`Key: value` headers up to the first blank line, with indented continuation lines."""
    out: dict[str, str] = {}
    last = None
    for line in (text or "").splitlines():
        if not line.strip():
            break
        if line[0] in " \t" and last:
            out[last] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        if sep and key and " " not in key.strip():
            last = key.strip()
            out.setdefault(last, value.strip())
    return out


def _parent(cpath: str) -> str:
    return str(PurePosixPath(cpath).parent)


def _pip(host: Path, cpath: str) -> Pkg | None:
    if host.is_dir():
        meta = host / ("METADATA" if host.name.endswith(".dist-info") else "PKG-INFO")
    else:
        meta = host
    h = _headers(_read(meta))
    if not h.get("Name") or not h.get("Version"):
        log.debug("skipping unreadable pip metadata %s", host)
        return None
    url = None
    direct = host / "direct_url.json"
    if host.is_dir() and direct.is_file():
        try:
            url = json.loads(_read(direct) or "{}").get("url")
        except ValueError:
            url = None
    return Pkg("pip", h["Name"], h["Version"], _parent(cpath), cpath, url)


def _r(host: Path, cpath: str) -> Pkg | None:
    h = _headers(_read(host / "DESCRIPTION"))
    if not h.get("Package") or not h.get("Version"):
        log.debug("skipping unreadable R DESCRIPTION in %s", host)
        return None
    return Pkg("R", h["Package"], h["Version"], _parent(cpath), cpath, h.get("Repository"))


def _conda(host: Path, cpath: str) -> Pkg | None:
    try:
        d = json.loads(_read(host) or "")
    except ValueError:
        log.debug("skipping unreadable conda metadata %s", host)
        return None
    if not isinstance(d, dict) or not d.get("name") or not d.get("version"):
        return None
    return Pkg("conda", str(d["name"]), str(d["version"]), _parent(_parent(cpath)), cpath, d.get("url"))


def parse_dpkg_status(text: str) -> list[Pkg]:
    out = []
    for stanza in text.split("\n\n"):
        h = _headers(stanza.strip("\n"))
        status = h.get("Status", "")
        if not status.endswith("ok installed") or not h.get("Package") or not h.get("Version"):
            continue
        arch = h.get("Architecture", "")
        name = h["Package"] if arch in ("", "all") else f"{h['Package']}:{arch}"
        out.append(Pkg("apt", name, h["Version"], _parent(DPKG_STATUS), DPKG_STATUS))
    return out


def _classify(host: Path, cpath: str, is_dir: bool) -> list[Pkg] | None:
    """Packages described by this entry, [] for unreadable metadata, None if it is not metadata."""
    p = PurePosixPath(cpath)
    if p.parent.name in PIP_PARENTS and (p.name.endswith(".dist-info") or p.name.endswith(".egg-info")):
        pkg = _pip(host, cpath)
        return [pkg] if pkg else []
    if is_dir and (host / "DESCRIPTION").is_file() and (host / "Meta" / "package.rds").is_file():
        pkg = _r(host, cpath)
        return [pkg] if pkg else []
    if not is_dir and p.parent.name == "conda-meta" and p.name.endswith(".json"):
        pkg = _conda(host, cpath)
        return [pkg] if pkg else []
    if not is_dir and cpath == DPKG_STATUS:
        return parse_dpkg_status(_read_large(host) or "")
    return None


def _cjoin(rel_dir: str, name: str) -> str:
    return (rel_dir.rstrip("/") or "") + "/" + name


def scan_tree(root: Path) -> list[Pkg]:
    """Every package whose metadata sits under ``root``, a whole rootfs."""
    found: dict[tuple[str, str, str], Pkg] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        rel_dir = "/" if rel == "." else "/" + rel
        if rel_dir == "/":
            dirnames[:] = [d for d in dirnames if d not in _SKIP_TOP]
        dirnames[:] = [d for d in dirnames if not d.startswith("00LOCK")]
        dirnames.sort()
        for name in list(dirnames) + sorted(filenames):
            host = Path(dirpath) / name
            is_dir = name in dirnames and not host.is_symlink()
            if host.is_symlink():
                continue
            pkgs = _classify(host, _cjoin(rel_dir, name), is_dir)
            if pkgs is None:
                continue
            for pkg in pkgs:
                if pkg.key not in found or _natural_sort_key(pkg.version) > _natural_sort_key(found[pkg.key].version):
                    found[pkg.key] = pkg
            if is_dir:
                dirnames.remove(name)
    return sorted(found.values(), key=lambda p: p.key)


def _drop_under(merged: dict, cprefix: str) -> None:
    prefix = cprefix.rstrip("/") + "/"
    for key, pkg in list(merged.items()):
        if pkg.path == cprefix or pkg.path.startswith(prefix):
            del merged[key]


def ns_layer_packages(upper: Path, base: list[Pkg]) -> list[Pkg]:
    """The packages visible through an overlay: ``base`` with the upper's changes applied."""
    merged = {p.key: p for p in base}
    for dirpath, dirnames, filenames in os.walk(upper, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, upper)
        rel_dir = "/" if rel == "." else "/" + rel
        if rel_dir != "/" and is_opaque(Path(dirpath)):
            _drop_under(merged, rel_dir)
        dirnames[:] = [d for d in dirnames if not d.startswith("00LOCK")]
        dirnames.sort()
        for name in list(dirnames) + sorted(filenames):
            host = Path(dirpath) / name
            cpath = _cjoin(rel_dir, name)
            try:
                st = os.lstat(host)
            except OSError:
                continue
            if is_whiteout(host, st):
                _drop_under(merged, cpath)
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            pkgs = _classify(host, cpath, is_dir)
            if pkgs is None:
                continue
            if pkgs == [] and is_dir and not is_opaque(host):
                if name in dirnames:
                    dirnames.remove(name)
                continue
            _drop_under(merged, cpath)
            for pkg in pkgs:
                merged[pkg.key] = pkg
            if is_dir:
                dirnames.remove(name)
    return sorted(merged.values(), key=lambda p: p.key)


def _entry(p: Pkg, change: str) -> dict:
    e = {"manager": p.manager, "name": p.name, "version": p.version, "change": change, "location": p.location}
    if p.url:
        e["url"] = p.url
    return e


def changes(base: list[Pkg], merged: list[Pkg]) -> list[dict]:
    b = {p.key: p for p in base}
    m = {p.key: p for p in merged}
    out = []
    for key in sorted(set(b) | set(m)):
        if key not in b:
            out.append(_entry(m[key], "added"))
        elif key not in m:
            out.append(_entry(b[key], "removed"))
        elif b[key].version != m[key].version:
            out.append(_entry(m[key], "changed"))
    return out
