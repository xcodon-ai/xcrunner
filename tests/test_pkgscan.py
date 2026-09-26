import json
import os
import stat
from pathlib import Path

import pytest

from xcodon_runtime import pkgscan
from xcodon_runtime.pkgscan import Pkg, changes, ns_layer_packages, parse_dpkg_status, scan_tree

SP = "usr/local/lib/python3.12/site-packages"


def put(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def pip_pkg(root: Path, name: str, version: str, sp: str = SP, url: str | None = None) -> None:
    d = f"{sp}/{name}-{version}.dist-info"
    put(root, f"{d}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nbody\n")
    if url:
        put(root, f"{d}/direct_url.json", json.dumps({"url": url}))


def r_pkg(root: Path, lib: str, name: str, version: str) -> None:
    put(root, f"{lib}/{name}/DESCRIPTION", f"Package: {name}\nVersion: {version}\nRepository: CRAN\n")
    put(root, f"{lib}/{name}/Meta/package.rds", "x")


STATUS = """Package: adduser
Status: install ok installed
Architecture: all
Version: 3.134

Package: libc6
Status: install ok installed
Architecture: amd64
Version: 2.36-9

Package: gone
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0
"""


def test_parse_dpkg_status():
    pkgs = parse_dpkg_status(STATUS)
    assert [(p.name, p.version) for p in pkgs] == [("adduser", "3.134"), ("libc6:amd64", "2.36-9")]
    assert {p.location for p in pkgs} == {"/var/lib/dpkg"}


def test_scan_tree_finds_every_manager_and_ignores_caches(tmp_path):
    root = tmp_path / "rootfs"
    pip_pkg(root, "numpy", "2.1.0", url="https://files.pythonhosted.org/numpy.whl")
    pip_pkg(root, "et_xmlfile", "2.0.0", sp="root/.cache/uv/archive-v0/abc")  # a cache, not an install
    put(root, "usr/lib/python3/dist-packages/six-1.16.0.egg-info", "Name: six\nVersion: 1.16.0\n")
    r_pkg(root, "usr/local/lib/R/site-library", "dplyr", "1.1.4")
    put(root, "usr/local/lib/R/site-library/srcpkg/DESCRIPTION", "Package: srcpkg\nVersion: 0.1\n")  # no Meta/
    put(root, "opt/conda/conda-meta/zlib-1.3.1-0.json",
        json.dumps({"name": "zlib", "version": "1.3.1", "url": "https://conda.anaconda.org/conda-forge/zlib.conda"}))
    put(root, "var/lib/dpkg/status", STATUS)
    got = {(p.manager, p.name, p.version, p.location, p.url) for p in scan_tree(root)}
    assert got == {
        ("pip", "numpy", "2.1.0", "/" + SP, "https://files.pythonhosted.org/numpy.whl"),
        ("pip", "six", "1.16.0", "/usr/lib/python3/dist-packages", None),
        ("R", "dplyr", "1.1.4", "/usr/local/lib/R/site-library", "CRAN"),
        ("conda", "zlib", "1.3.1", "/opt/conda", "https://conda.anaconda.org/conda-forge/zlib.conda"),
        ("apt", "adduser", "3.134", "/var/lib/dpkg", None),
        ("apt", "libc6:amd64", "2.36-9", "/var/lib/dpkg", None),
    }


def test_unreadable_metadata_is_skipped(tmp_path):
    root = tmp_path / "rootfs"
    put(root, f"{SP}/broken-1.0.dist-info/METADATA", "no headers here")
    put(root, "opt/conda/conda-meta/bad.json", "{not json")
    assert scan_tree(root) == []


def test_changes_classifies_added_changed_removed():
    base = [Pkg("pip", "a", "1", "/sp", "/sp/a-1.dist-info"), Pkg("pip", "b", "1", "/sp", "/sp/b-1.dist-info")]
    merged = [Pkg("pip", "a", "2", "/sp", "/sp/a-2.dist-info"), Pkg("pip", "c", "3", "/sp", "/sp/c-3.dist-info", "u")]
    assert changes(base, merged) == [
        {"manager": "pip", "name": "a", "version": "2", "change": "changed", "location": "/sp"},
        {"manager": "pip", "name": "b", "version": "1", "change": "removed", "location": "/sp"},
        {"manager": "pip", "name": "c", "version": "3", "change": "added", "location": "/sp", "url": "u"},
    ]


def test_ns_upper_adds_replaces_and_whiteouts(tmp_path, monkeypatch):
    base_root = tmp_path / "base"
    pip_pkg(base_root, "old", "1.0")
    pip_pkg(base_root, "keep", "1.0")
    pip_pkg(base_root, "bump", "1.0")
    put(base_root, "var/lib/dpkg/status", STATUS)
    base = scan_tree(base_root)
    upper = tmp_path / "upper"
    pip_pkg(upper, "new", "2.0")
    pip_pkg(upper, "bump", "1.1")
    put(upper, f"{SP}/old-1.0.dist-info", "")      # stands in for a whiteout (char device 0:0)
    put(upper, f"{SP}/bump-1.0.dist-info", "")     # pip replaced bump 1.0 with 1.1
    put(upper, "var/lib/dpkg/status", STATUS.replace("Version: 3.134", "Version: 3.135"))
    monkeypatch.setattr(pkgscan, "is_whiteout",
                        lambda p, st: stat.S_ISREG(st.st_mode) and st.st_size == 0 and p.name.endswith(".dist-info"))
    got = {(c["manager"], c["name"], c["version"], c["change"]) for c in changes(base, ns_layer_packages(upper, base))}
    assert got == {("pip", "new", "2.0", "added"), ("pip", "bump", "1.1", "changed"),
                   ("pip", "old", "1.0", "removed"), ("apt", "adduser", "3.135", "changed")}


def test_ns_opaque_directory_hides_base_packages(tmp_path):
    base_root = tmp_path / "base"
    pip_pkg(base_root, "old", "1.0")
    upper = tmp_path / "upper"
    pip_pkg(upper, "fresh", "1.0")
    try:
        os.setxattr(upper / SP, "user.overlay.opaque", b"y")
    except OSError:
        pytest.skip("user xattrs unsupported here")
    base = scan_tree(base_root)
    got = {(c["name"], c["change"]) for c in changes(base, ns_layer_packages(upper, base))}
    assert got == {("fresh", "added"), ("old", "removed")}


def test_pkg_json_round_trip():
    p = Pkg("R", "x", "1", "/lib", "/lib/x", "CRAN")
    assert pkgscan.pkg_from_json(pkgscan.pkg_to_json(p)) == p
