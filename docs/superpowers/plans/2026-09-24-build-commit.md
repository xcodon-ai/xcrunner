# Build and Commit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let xrunner turn a container's or env folder's writable layer into a named image (`commit`), run a Dockerfile subset in its own containers (`build`), speak docker's verbs for that subset through `xrunner docker` and an installable `docker` shim, and stop serving a stale image when a daemon rebuilds a tag.

**Architecture:** `layerdiff.py` turns an overlay upper dir or a proot rootfs diff into an OCI layer directory and hashes it. `ImageStore.commit` composes a new image from a base image plus that layer and config changes, reusing the existing flatten and layout. `build.py` parses the Dockerfile subset and drives `Runtime` (create/start/exec/commit) step by step with a cache. The CLI adds `build`, `commit`, `tag`, `image`, `docker`, and `shim`. `Runtime.resolve_image` re-imports a daemon image whose id changed.

**Tech Stack:** Python 3.10+, stdlib only. Existing fixtures `home`, `busybox_image`, `busybox_rootfs`, `engine_name`.

**Spec:** `docs/superpowers/specs/2026-09-23-xcodon-runtime-design.md`, Section 11.

## Global Constraints

- Python `>=3.10`, stdlib only, plain English docstrings and messages; every error derives from `XcodonError`.
- Whiteout translation exactly as spec 11.2: char device 0:0 -> `.wh.<name>`; dir with `user.overlay.opaque` or `trusted.overlay.opaque` = `y` -> `.wh..wh..opq` inside; `overlay.*` attributes dropped; special files skipped.
- Diff id = SHA-256 of a deterministic tar (sorted paths, uid/gid 0, mtime 0, uname/gname empty); image id = SHA-256 of `json.dumps(config, sort_keys=True, separators=(",", ":"))`; `manifest.json` has `source: "commit"` and `parent`.
- A commit source must not be in use: container state not `running`; env layer `.lock` must be acquirable with `LOCK_NB`.
- Build cache file `<home>/build-cache.json`, key = sha256 of `f"{parent_id}|{instruction.name}|{expanded args}|{content hash or ''}"`; `prune --all` removes untagged images.
- Docker dispatcher exits 125 for unknown verbs; `docker image inspect` prints a JSON array and exits 1 when absent; the shim refuses to shadow a real docker unless `--force`.
- Daemon-id check applies only to images whose manifest `source` is `daemon`.
- Existing behavior unchanged; full suite stays green; `ruff check src tests` clean. Branch `feat/build-commit`.

## File Structure

```
src/xcodon_runtime/layerdiff.py        snapshot_upper, snapshot_diff, hash_layer_dir, link_tree      (new)
src/xcodon_runtime/imagestore.py       commit, tag, layer_dirs, untagged, prune(all) removes untagged
src/xcodon_runtime/daemon.py           DaemonSource.image_id
src/xcodon_runtime/api.py              Runtime.commit, Runtime.build, daemon-id check in resolve_image
src/xcodon_runtime/build.py            Instruction, parse_dockerfile, Builder                            (new)
src/xcodon_runtime/cli.py              build, commit, tag, image, docker, shim; _split_argv for docker run
tests/test_layerdiff.py, tests/test_commit.py, tests/test_build.py, tests/test_cli_docker.py   (new)
README.md, docs (spec already written)
```

---

### Task 1: Layer snapshots and hashing

**Files:**
- Create: `src/xcodon_runtime/layerdiff.py`
- Test: `tests/test_layerdiff.py`

**Interfaces:**
- Produces: `layerdiff.snapshot_upper(upper: Path, dest: Path) -> int` (entries written), `layerdiff.snapshot_diff(rootfs: Path, base_rootfs: Path, dest: Path) -> int`, `layerdiff.hash_layer_dir(layer: Path) -> str` (`"sha256:<hex>"`), `layerdiff.link_tree(src: Path, dst: Path) -> None` (hardlink files, recreate dirs and symlinks, copy on EXDEV), constants `WHITEOUT_PREFIX`, `OPAQUE`, `OPAQUE_XATTRS`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_layerdiff.py
import os
import stat
import tarfile
import io
from pathlib import Path

import pytest

from xcodon_runtime.layerdiff import OPAQUE, hash_layer_dir, link_tree, snapshot_diff, snapshot_upper


def mk(root: Path, files: dict[str, str | None]) -> Path:
    for rel, content in files.items():
        p = root / rel
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
        elif content.startswith("LINK:"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(content[5:])
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return root


def test_snapshot_upper_translates_opaque_dirs_and_copies_files(tmp_path):
    upper = mk(tmp_path / "upper", {"bin/added": "m", "newdir": None, "replaced/new": "n", "lnk": "LINK:/etc/hosts"})
    try:
        os.setxattr(upper / "replaced", "user.overlay.opaque", b"y")
        os.setxattr(upper / "bin", "user.overlay.origin", b"\x00")
    except OSError:
        pytest.skip("user xattrs unsupported on this filesystem")
    dest = tmp_path / "layer"
    n = snapshot_upper(upper, dest)
    assert (dest / "bin/added").read_text() == "m"
    assert os.stat(dest / "bin/added").st_ino == os.stat(upper / "bin/added").st_ino
    assert (dest / "replaced" / OPAQUE).exists()
    assert (dest / "replaced/new").read_text() == "n"
    assert os.readlink(dest / "lnk") == "/etc/hosts"
    assert (dest / "newdir").is_dir()
    assert not os.listxattr(dest / "bin"), "overlay attributes are not carried over"
    assert n >= 5


@pytest.mark.ns
def test_snapshot_upper_translates_whiteout_devices(home, busybox_image):
    """Real overlay whiteouts need a user namespace to create; use a container."""
    from xcodon_runtime.api import Runtime
    from xcodon_runtime.engine_ns import NsEngine

    rt = Runtime(home.path, engine="ns")
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    assert rt.exec(c, "rm /bin/ls; rm -rf /home/user && mkdir /home/user && echo n > /home/user/x").code == 0
    rt.stop(c)
    upper, _ = NsEngine().layer_paths(c)
    dest = home.path / "snap"
    snapshot_upper(upper, dest)
    assert (dest / "bin" / ".wh.ls").is_file()
    assert (dest / "home/user" / OPAQUE).exists()
    assert (dest / "home/user/x").read_text() == "n\n"
    assert not (dest / "bin" / "ls").exists()


def test_snapshot_diff_finds_added_changed_and_deleted(tmp_path):
    base = mk(tmp_path / "base", {"keep": "k", "changed": "old", "gone": "g", "d/inner": "i", "gonedir/f": "f"})
    cur = mk(tmp_path / "cur", {"keep": "k", "changed": "new!", "d/inner": "i", "d/added": "a", "newlink": "LINK:keep"})
    # same mtime for unchanged files so only content/size decides
    for rel in ("keep", "d/inner"):
        st = os.stat(base / rel)
        os.utime(cur / rel, ns=(st.st_atime_ns, st.st_mtime_ns))
    dest = tmp_path / "layer"
    snapshot_diff(cur, base, dest)
    assert (dest / "changed").read_text() == "new!"
    assert (dest / "d/added").read_text() == "a"
    assert (dest / ".wh.gone").is_file()
    assert (dest / ".wh.gonedir").is_file()
    assert os.readlink(dest / "newlink") == "keep"
    assert not (dest / "keep").exists()
    assert not (dest / "d/inner").exists()


def test_hash_layer_dir_is_deterministic_and_content_sensitive(tmp_path):
    a = mk(tmp_path / "a", {"x/y": "1", "z": "2"})
    b = mk(tmp_path / "b", {"x/y": "1", "z": "2"})
    os.utime(b / "z", ns=(1, 1))
    assert hash_layer_dir(a) == hash_layer_dir(b)
    assert hash_layer_dir(a).startswith("sha256:")
    (b / "z").write_text("3")
    assert hash_layer_dir(a) != hash_layer_dir(b)
    assert hash_layer_dir(mk(tmp_path / "empty", {})) == hash_layer_dir(mk(tmp_path / "empty2", {}))


def test_link_tree(tmp_path):
    src = mk(tmp_path / "src", {"a/b": "x", "l": "LINK:a/b"})
    os.chmod(src / "a", 0o750)
    link_tree(src, tmp_path / "dst")
    assert os.stat(tmp_path / "dst/a/b").st_ino == os.stat(src / "a/b").st_ino
    assert os.readlink(tmp_path / "dst/l") == "a/b"
    assert stat.S_IMODE(os.stat(tmp_path / "dst/a").st_mode) == 0o750
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_layerdiff.py`
Expected: `ModuleNotFoundError: No module named 'xcodon_runtime.layerdiff'`

- [ ] **Step 3: Write layerdiff.py**

```python
# src/xcodon_runtime/layerdiff.py
"""Turn a writable layer into an OCI layer directory, and hash layer directories.

The ns engine's overlay upper directory records a deleted file as a
character device 0:0 and a replaced directory with an ``overlay.opaque``
attribute. OCI layers record the same facts as ``.wh.<name>`` files and a
``.wh..wh..opq`` file. The proot engine has no upper directory, so its layer
is the difference between the rootfs copy and the image rootfs.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import shutil
import stat
import tarfile
from pathlib import Path

WHITEOUT_PREFIX = ".wh."
OPAQUE = ".wh..wh..opq"
OPAQUE_XATTRS = ("user.overlay.opaque", "trusted.overlay.opaque")


def _is_whiteout(st: os.stat_result) -> bool:
    return stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == 0 and os.minor(st.st_rdev) == 0


def _is_opaque(path: Path) -> bool:
    for name in OPAQUE_XATTRS:
        try:
            if os.getxattr(path, name, follow_symlinks=False) == b"y":
                return True
        except OSError:
            continue
    return False


def _place_file(src: Path, dst: Path) -> None:
    """Hardlink a regular file into the layer, copying when linking is not possible."""
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
            shutil.copy2(src, dst, follow_symlinks=False)
        else:
            raise


def link_tree(src: Path, dst: Path) -> None:
    """Recreate ``src`` under ``dst``: directories with their mode, symlinks as is, files hardlinked."""
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, stat.S_IMODE(os.lstat(src).st_mode) | 0o700)
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        target = dst / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif entry.is_dir(follow_symlinks=False):
            link_tree(Path(entry.path), target)
        elif entry.is_file(follow_symlinks=False):
            _place_file(Path(entry.path), target)


def snapshot_upper(upper: Path, dest: Path) -> int:
    """Translate an overlay upper directory into an OCI layer directory. Returns entries written."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in sorted(os.scandir(upper), key=lambda e: e.name):
        src = Path(entry.path)
        st = os.lstat(src)
        target = dest / entry.name
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), target)
            count += 1
        elif stat.S_ISDIR(st.st_mode):
            target.mkdir(exist_ok=True)
            os.chmod(target, stat.S_IMODE(st.st_mode) | 0o700)
            count += 1
            if _is_opaque(src):
                (target / OPAQUE).touch()
                count += 1
            count += snapshot_upper(src, target)
        elif _is_whiteout(st):
            (dest / (WHITEOUT_PREFIX + entry.name)).touch()
            count += 1
        elif stat.S_ISREG(st.st_mode):
            _place_file(src, target)
            count += 1
        # sockets, fifos, other devices: skipped
    return count


def _same_file(a: Path, b: Path, sa: os.stat_result, sb: os.stat_result) -> bool:
    if stat.S_IFMT(sa.st_mode) != stat.S_IFMT(sb.st_mode):
        return False
    if stat.S_ISLNK(sa.st_mode):
        return os.readlink(a) == os.readlink(b)
    if stat.S_ISREG(sa.st_mode):
        return (sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns
                and stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode))
    return stat.S_IMODE(sa.st_mode) == stat.S_IMODE(sb.st_mode)


def snapshot_diff(rootfs: Path, base_rootfs: Path, dest: Path) -> int:
    """Write the difference between ``rootfs`` and ``base_rootfs`` as an OCI layer directory."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    cur_names = {e.name for e in os.scandir(rootfs)}
    base_names = {e.name for e in os.scandir(base_rootfs)} if base_rootfs.is_dir() else set()
    for name in sorted(base_names - cur_names):
        (dest / (WHITEOUT_PREFIX + name)).touch()
        count += 1
    for name in sorted(cur_names):
        cur = rootfs / name
        base = base_rootfs / name
        sc = os.lstat(cur)
        try:
            sb: os.stat_result | None = os.lstat(base)
        except FileNotFoundError:
            sb = None
        target = dest / name
        if stat.S_ISDIR(sc.st_mode):
            if sb is not None and not stat.S_ISDIR(sb.st_mode):
                (dest / (WHITEOUT_PREFIX + name)).touch()
                sb = None
            if sb is None:
                link_tree(cur, target)
                count += 1
                continue
            sub = snapshot_diff(cur, base, target)
            if sub or stat.S_IMODE(sc.st_mode) != stat.S_IMODE(sb.st_mode):
                target.mkdir(exist_ok=True)
                os.chmod(target, stat.S_IMODE(sc.st_mode) | 0o700)
                count += sub + 1
            elif target.exists():
                os.rmdir(target)
            continue
        if sb is not None and _same_file(cur, base, sc, sb):
            continue
        if sb is not None and stat.S_ISDIR(sb.st_mode):
            (dest / (WHITEOUT_PREFIX + name)).touch()
        if stat.S_ISLNK(sc.st_mode):
            os.symlink(os.readlink(cur), target)
            count += 1
        elif stat.S_ISREG(sc.st_mode):
            _place_file(cur, target)
            count += 1
    return count


class _Hasher(io.RawIOBase):
    def __init__(self) -> None:
        self.h = hashlib.sha256()

    def write(self, b) -> int:  # type: ignore[override]
        self.h.update(b)
        return len(b)


def hash_layer_dir(layer: Path) -> str:
    """SHA-256 of a deterministic tar of the layer: sorted paths, owner 0, mtime 0."""
    sink = _Hasher()
    with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(layer):
            dirs.sort()
            for name in sorted(dirs + files):
                full = Path(root) / name
                info = tar.gettarinfo(str(full), arcname=str(full.relative_to(layer)))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if info.isfile():
                    with open(full, "rb") as f:
                        tar.addfile(info, f)
                else:
                    tar.addfile(info)
    return "sha256:" + sink.h.hexdigest()
```

Note: `tarfile` in `w|` stream mode writes through `sink.write`; the `_Hasher` only needs `write`. If `tarfile` complains that the object is not writable, set `writable = lambda self: True` on `_Hasher`.

- [ ] **Step 4: Run tests, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_layerdiff.py` then the full suite.

```bash
git add src/xcodon_runtime/layerdiff.py tests/test_layerdiff.py
git commit -m "feat: layer snapshots from overlay uppers and proot diffs, deterministic layer hashes"
```

---

### Task 2: Commit, tag, untagged prune, and the daemon-id check

**Files:**
- Modify: `src/xcodon_runtime/imagestore.py`
- Modify: `src/xcodon_runtime/daemon.py`
- Modify: `src/xcodon_runtime/api.py`
- Test: `tests/test_commit.py`

**Interfaces:**
- Produces: `ImageStore.layer_dirs(image: Image) -> list[Path]`; `ImageStore.commit(base: Image, layer_dir: Path | None, *, changes: dict | None = None, ref: str | None = None, created_by: str = "") -> Image`; `ImageStore.tag(ref_or_id: str, new_ref: str) -> Image`; `ImageStore.untagged() -> list[Image]`; `ImageStore.prune(all=True)` also removes untagged images; `imagestore.apply_config_changes(config: dict, changes: dict) -> None` (keys `Env` list of `K=V`, `Cmd`, `Entrypoint`, `WorkingDir`, `User`, `Labels` dict; `Env` merges by key); `DaemonSource.image_id(ref: Reference) -> str | None` (`docker image inspect --format {{.Id}}`); `Runtime.commit(container: Container | None = None, tag: str | None = None, *, env_dir: str | Path | None = None, image: str | None = None, changes: dict | None = None, message: str = "") -> Image`; `Runtime.resolve_image` re-imports when the daemon's id differs for a `daemon`-sourced image.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commit.py
import json
import os
import stat
from pathlib import Path

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.imagestore import ImageStore, apply_config_changes


def test_apply_config_changes_merges_env_and_labels():
    cfg = {"config": {"Env": ["A=1", "PATH=/bin"], "Labels": {"x": "1"}}}
    apply_config_changes(cfg, {"Env": ["A=2", "B=3"], "Labels": {"y": "2"}, "WorkingDir": "/w", "Cmd": ["sh"]})
    assert cfg["config"]["Env"] == ["A=2", "PATH=/bin", "B=3"]
    assert cfg["config"]["Labels"] == {"x": "1", "y": "2"}
    assert cfg["config"]["WorkingDir"] == "/w" and cfg["config"]["Cmd"] == ["sh"]


def test_store_commit_layer_and_config(home, busybox_image, tmp_path):
    st = ImageStore(home, sources=[])
    layer = tmp_path / "layer"
    (layer / "opt").mkdir(parents=True)
    (layer / "opt" / "tool").write_text("t")
    (layer / "bin").mkdir()
    (layer / "bin" / ".wh.ls").touch()
    img = st.commit(busybox_image, layer, changes={"Env": ["TOOL=1"]}, ref="xcodon-test/committed:v1", created_by="test")
    assert img.id != busybox_image.id and len(img.id) == 64
    assert (img.rootfs / "opt" / "tool").read_text() == "t"
    assert not (img.rootfs / "bin" / "ls").exists(), "whiteout applied"
    assert (img.rootfs / "bin" / "busybox").exists(), "base layers kept"
    assert "TOOL=1" in img.config["config"]["Env"]
    assert img.config["rootfs"]["diff_ids"][:-1] == busybox_image.config["rootfs"]["diff_ids"]
    m = json.loads((img.dir / "manifest.json").read_text())
    assert m["source"] == "commit" and m["parent"] == busybox_image.id
    assert img.config["history"][-1]["created_by"] == "test"
    assert st.get("xcodon-test/committed:v1").id == img.id
    assert st.layer_dirs(img)[-1].exists()


def test_store_commit_is_deterministic_and_config_only(home, busybox_image, tmp_path):
    st = ImageStore(home, sources=[])
    a = st.commit(busybox_image, None, changes={"Cmd": ["/bin/true"]}, created_by="x")
    assert a.config["rootfs"]["diff_ids"] == busybox_image.config["rootfs"]["diff_ids"]
    assert a.config["config"]["Cmd"] == ["/bin/true"]
    assert a.refs == []
    assert a.id in {i.id for i in st.untagged()}
    st.tag(a.id, "xcodon-test/cfg:latest")
    assert st.get("xcodon-test/cfg:latest").id == a.id
    assert a.id not in {i.id for i in st.untagged()}


def test_prune_all_removes_untagged_images(home, busybox_image):
    st = ImageStore(home, sources=[])
    a = st.commit(busybox_image, None, changes={"Cmd": ["/bin/true"]}, created_by="x")
    removed = st.prune(all=True)
    assert any(p.name == a.id for p in removed)
    assert st.get(a.id) is None and st.get("xcodon-test/busybox") is not None


def test_runtime_commit_container(home, busybox_image, engine_name):
    rt = Runtime(home.path, engine=engine_name)
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    assert rt.exec(c, "mkdir -p /opt/t && echo v > /opt/t/f && rm /bin/ls").code == 0
    with pytest.raises(XcodonError, match="running"):
        rt.commit(c, "xcodon-test/snap:latest")
    rt.stop(c)
    img = rt.commit(c, "xcodon-test/snap:latest", changes={"Env": ["SNAP=1"]}, message="snap")
    rt.remove(c)
    code = rt.run("xcodon-test/snap:latest", command=["/bin/sh", "-c", "cat /opt/t/f; echo $SNAP; ls /bin/ls 2>/dev/null || echo gone"], rm=True, stdout=open(home.path / "out", "wb"))
    assert code == 0
    assert (home.path / "out").read_bytes() == b"v\n1\ngone\n"


def test_runtime_commit_env_dir(home, busybox_image, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    env = tmp_path / "env"
    assert rt.run("xcodon-test/busybox", command=["/bin/sh", "-c", "mkdir -p /usr/local/x && echo tool > /usr/local/x/t"], rm=True, env_dir=env) == 0
    img = rt.commit(None, "xcodon-test/fromenv:latest", env_dir=env, image="xcodon-test/busybox")
    assert (img.rootfs / "usr/local/x/t").read_text() == "tool\n"
    # a locked layer is refused
    c = rt.create("xcodon-test/busybox", env_dir=env)
    rt.start(c)
    if engine_name == "ns":
        with pytest.raises(XcodonError, match="in use"):
            rt.commit(None, "xcodon-test/fromenv:v2", env_dir=env, image="xcodon-test/busybox")
    rt.stop(c)


def test_resolve_reimports_when_daemon_id_changed(home, busybox_image, monkeypatch, tmp_path):
    """A daemon-sourced image whose tag now points at a different id is pulled again."""
    from xcodon_runtime import api as api_mod
    from xcodon_runtime.daemon import DaemonSource
    from xcodon_runtime.reference import Reference

    m = json.loads((busybox_image.dir / "manifest.json").read_text())
    m["source"] = "daemon"
    (busybox_image.dir / "manifest.json").write_text(json.dumps(m))
    rt = Runtime(home.path, engine="ns")
    calls = []
    monkeypatch.setattr(DaemonSource, "available", lambda self: True)
    monkeypatch.setattr(DaemonSource, "has_image", lambda self, ref: True)
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + "f" * 64)
    monkeypatch.setattr(ImageStore, "pull", lambda self, ref, platform=None: calls.append(ref) or busybox_image)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"]
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + busybox_image.id)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"], "same id: no second pull"
    m["source"] = "commit"
    (busybox_image.dir / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setattr(DaemonSource, "image_id", lambda self, ref: "sha256:" + "e" * 64)
    rt.resolve_image("xcodon-test/busybox")
    assert calls == ["xcodon-test/busybox"], "locally built images are never replaced"
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_commit.py`
Expected: ImportError for `apply_config_changes`.

- [ ] **Step 3: Store changes**

Add to `imagestore.py` (imports: `copy`, `datetime`, `from xcodon_runtime.layerdiff import hash_layer_dir, link_tree`, `from xcodon_runtime.errors import XcodonError`):

```python
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"


def apply_config_changes(config: dict, changes: dict) -> None:
    """Apply docker-style config changes in place: Env merges by key, Labels merge, others replace."""
    cfg = config.setdefault("config", {})
    for key, value in (changes or {}).items():
        if key == "Env":
            merged: dict[str, str] = {}
            for item in list(cfg.get("Env") or []) + list(value):
                k, _, v = item.partition("=")
                merged[k] = v
            cfg["Env"] = [f"{k}={v}" for k, v in merged.items()]
        elif key == "Labels":
            cfg["Labels"] = {**(cfg.get("Labels") or {}), **value}
        elif key in ("Cmd", "Entrypoint", "WorkingDir", "User"):
            cfg[key] = value
        else:
            raise XcodonError(f"unsupported config change {key!r}")
```

Methods on `ImageStore`:

```python
    def layer_dirs(self, image: Image) -> list[Path]:
        """The extracted layer directories of an image, in order."""
        manifest = json.loads((image.dir / "manifest.json").read_text())
        dirs = []
        for diff_id in manifest.get("diff_ids", []):
            d = self.home.layers / diff_id.split(":", 1)[1]
            if not d.is_dir():
                raise XcodonError(f"layer {diff_id[:19]} of image {image.short_id} is missing; pull the image again")
            dirs.append(d)
        return dirs

    def commit(self, base: Image, layer_dir: Path | None, *, changes: dict | None = None,
               ref: str | None = None, created_by: str = "") -> Image:
        """Compose a new image from ``base`` plus one layer directory and config changes.

        ``layer_dir`` is an OCI layer directory (whiteouts as ``.wh.`` files) or None for a
        config-only image. Holds ``store`` shared like a pull. Lock order: store -> layer/image/refs.
        """
        with self.home.lock("store", shared=True):
            base_dirs = self.layer_dirs(base)
            config = copy.deepcopy(base.config)
            apply_config_changes(config, changes or {})
            diff_ids = list(config.setdefault("rootfs", {}).setdefault("diff_ids", []))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if layer_dir is not None:
                diff_id = hash_layer_dir(layer_dir)
                hexd = diff_id.split(":", 1)[1]
                dest = self.home.layers / hexd
                if not dest.exists():
                    with self.home.lock(f"layer-{hexd}"):
                        if not dest.exists():
                            with self.home.atomic_dir(dest) as tmp:
                                link_tree(layer_dir, tmp)
                diff_ids.append(diff_id)
                base_dirs = base_dirs + [dest]
            config["rootfs"]["diff_ids"] = diff_ids
            config["created"] = now
            config.setdefault("history", []).append(
                {"created": now, "created_by": created_by or "xrunner commit", "empty_layer": layer_dir is None})
            canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            image_id = hashlib.sha256(canonical).hexdigest()
            image_dir = self.home.images / image_id
            with self.home.lock(f"image-{image_id}"):
                if not image_dir.exists():
                    with self.home.atomic_dir(image_dir) as tmp:
                        (tmp / "config.json").write_text(json.dumps(config, indent=2))
                        (tmp / "manifest.json").write_text(json.dumps({
                            "config": f"sha256:{image_id}", "diff_ids": diff_ids,
                            "layers": [{"digest": d, "mediaType": LAYER_MEDIA_TYPE, "size": 0} for d in diff_ids],
                            "source": "commit", "parent": base.id,
                        }, indent=2))
                        build_rootfs(base_dirs, tmp / "rootfs")
            if ref:
                self._set_ref(ref, image_id)
            return self._load(image_id)

    def _set_ref(self, ref: str, image_id: str) -> None:
        name = parse_reference(ref).name
        with self.home.lock("refs"):
            refs = self.home.read_refs()
            refs[name] = image_id
            self.home.write_refs(refs)

    def tag(self, ref_or_id: str, new_ref: str) -> Image:
        img = self.require(ref_or_id)
        self._set_ref(new_ref, img.id)
        return self._load(img.id)

    def untagged(self) -> list[Image]:
        return [i for i in self.images() if not i.refs]
```

In `prune(all=True)`, before computing `used`, remove untagged images:

```python
            if all:
                for img in self.untagged():
                    with self.home.lock(f"image-{img.id}"):
                        shutil.rmtree(img.dir, ignore_errors=True)
                    removed.append(img.dir)
```

Use `_set_ref` inside `import_fetched` too, replacing its inline refs write.

- [ ] **Step 4: Daemon image id**

In `daemon.py`:

```python
    def image_id(self, ref: Reference) -> str | None:
        """The daemon's image id for a tag (``sha256:<hex>``), or None if it has no such tag."""
        try:
            r = subprocess.run([self.docker, "image", "inspect", "--format", "{{.Id}}", ref.name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = r.stdout.strip()
        return out if r.returncode == 0 and out.startswith("sha256:") else None
```

- [ ] **Step 5: Runtime.commit and the daemon-id check**

In `api.py` (imports: `fcntl`, `json`, `shutil`, `tempfile`, `from xcodon_runtime.daemon import DaemonSource`, `from xcodon_runtime.envdir import ENV_LOCK_NAME, env_layer_dir`, `from xcodon_runtime.layerdiff import snapshot_diff, snapshot_upper`, `from xcodon_runtime.reference import parse_reference`):

```python
    def resolve_image(self, ref: str, pull: str = "missing") -> Image:
        if pull == "always":
            return self.images.pull(ref)
        img = self.images.get(ref)
        if img is not None:
            if self._daemon_tag_moved(ref, img):
                log.info("image %s changed in the local docker daemon; importing it again", ref)
                return self.images.pull(ref)
            return img
        if pull == "never":
            raise ImageNotFound(f"image {ref!r} is not in the local store")
        log.info("image %s not found locally; pulling", ref)
        return self.images.pull(ref)

    def _daemon_tag_moved(self, ref: str, img: Image) -> bool:
        """True when a daemon-sourced tag now points at a different image in the daemon."""
        try:
            manifest = json.loads((img.dir / "manifest.json").read_text())
        except OSError:
            return False
        if manifest.get("source") != "daemon":
            return False
        try:
            reference = parse_reference(ref)
        except XcodonError:
            return False
        daemon = next((s for s in self.images.sources if isinstance(s, DaemonSource)), None)
        if daemon is None or not daemon.available() or not daemon.has_image(reference):
            return False
        current = daemon.image_id(reference)
        return bool(current) and current != f"sha256:{img.id}"

    def commit(self, container: Container | None = None, tag: str | None = None, *,
               env_dir: str | Path | None = None, image: str | None = None,
               changes: dict | None = None, message: str = "") -> Image:
        """Snapshot a stopped container's layer, or an env folder layer, as a new image."""
        if container is not None:
            base = self.images.require(container.image_id)
            if container.state == "running" and self._engine(container).is_running(container):
                raise XcodonError(f"container {container.short_id} is running; stop it before commit")
            if container.engine == "ns":
                from xcodon_runtime.engine_ns import NsEngine
                upper, _ = NsEngine().layer_paths(container)
                source = ("upper", upper)
            else:
                from xcodon_runtime.engine_proot import ProotEngine
                source = ("rootfs", ProotEngine().rootfs_path(container))
            created_by = message or f"xrunner commit {container.short_id}"
        elif env_dir is not None and image is not None:
            base = self.images.require(image)
            layer = env_layer_dir(str(env_dir), base.id)
            if not layer.is_dir():
                raise XcodonError(f"no env layer for image {base.short_id} under {env_dir}")
            lock_path = layer / ENV_LOCK_NAME
            if lock_path.exists():
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise XcodonError(f"env layer {layer} is in use by a running container") from None
                finally:
                    os.close(fd)
            source = ("upper", layer / "upper") if (layer / "upper").is_dir() else ("rootfs", layer / "rootfs")
            created_by = message or f"xrunner commit --env-dir {env_dir}"
        else:
            raise XcodonError("commit needs a container, or --env-dir together with the base image")
        kind, path = source
        if not path.is_dir():
            raise XcodonError(f"nothing to commit: {path} does not exist")
        work = Path(tempfile.mkdtemp(prefix="commit-", dir=self.home.path))
        try:
            layer_dir = work / "layer"
            if kind == "upper":
                snapshot_upper(path, layer_dir)
            else:
                snapshot_diff(path, base.rootfs, layer_dir)
            return self.images.commit(base, layer_dir, changes=changes, ref=tag, created_by=created_by)
        finally:
            shutil.rmtree(work, ignore_errors=True)
```

`parse_reference` raises `InvalidReference`, which is an `XcodonError`; keep that import. Add `commit` to the API list in `info()` if there is one; otherwise nothing else.

- [ ] **Step 6: Run tests, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_commit.py tests/test_imagestore.py` then the full suite.

```bash
git add src/xcodon_runtime/imagestore.py src/xcodon_runtime/daemon.py src/xcodon_runtime/api.py tests/test_commit.py
git commit -m "feat: commit containers and env layers to images, tag, prune untagged, re-import moved daemon tags"
```

---

### Task 3: Dockerfile build

**Files:**
- Create: `src/xcodon_runtime/build.py`
- Modify: `src/xcodon_runtime/api.py` (`Runtime.build`)
- Test: `tests/test_build.py`

**Interfaces:**
- Produces: `build.Instruction(name: str, args: str, line: int)`; `build.parse_dockerfile(text: str) -> list[Instruction]`; `build.Builder(runtime: Runtime, context: Path, out: Callable[[str], None] | None = None)` with `build(dockerfile_text: str, tags: Sequence[str] = (), build_args: Mapping[str, str] | None = None, no_cache: bool = False) -> Image`; `build.expand_args(text: str, args: Mapping[str, str]) -> str` (`$NAME`, `${NAME}`, `${NAME:-default}`); `build.parse_command(args: str, shell: list[str]) -> list[str]` (JSON exec form or shell form); `build.parse_env(args: str) -> dict[str, str]`; `build.CACHE_FILE = "build-cache.json"`; `Runtime.build(context, dockerfile: Path | None = None, tags=(), build_args=None, no_cache=False, out=None) -> Image`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build.py
import json
import os
from pathlib import Path

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.build import Builder, expand_args, parse_command, parse_dockerfile, parse_env
from xcodon_runtime.errors import XcodonError


def test_parse_dockerfile_handles_comments_continuations_and_case():
    text = """# comment
FROM base:1  # not a comment in docker, kept as args
run echo a \\
    && echo b
COPY ["a b", "/dst/"]
ENV X=1 Y="two words"
"""
    ins = parse_dockerfile(text)
    assert [i.name for i in ins] == ["FROM", "RUN", "COPY", "ENV"]
    assert ins[1].args == "echo a     && echo b" or ins[1].args == "echo a && echo b"
    assert ins[2].args.startswith('["a b"')
    assert ins[3].line == 6


def test_helpers():
    assert expand_args("pip install $PKG ${VER:-1.0} ${X}", {"PKG": "numpy", "X": "x"}) == "pip install numpy 1.0 x"
    assert parse_command('["/bin/sh", "-c", "ls"]', ["/bin/sh", "-c"]) == ["/bin/sh", "-c", "ls"]
    assert parse_command("ls -la", ["/bin/sh", "-c"]) == ["/bin/sh", "-c", "ls -la"]
    assert parse_env('A=1 B="two words" C=x=y') == {"A": "1", "B": "two words", "C": "x=y"}
    assert parse_env("KEY some value here") == {"KEY": "some value here"}


@pytest.fixture
def rt(home, busybox_image, engine_name):
    return Runtime(home.path, engine=engine_name)


def test_build_full_subset(rt, tmp_path, home):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "tool.sh").write_text("#!/bin/sh\necho tool-ran $GREETING\n")
    (ctx / "data").mkdir()
    (ctx / "data" / "a.txt").write_text("A")
    (ctx / "Dockerfile").write_text("""
ARG GREETING=hello
FROM xcodon-test/busybox
ARG GREETING
ENV GREETING=$GREETING OTHER="two words"
WORKDIR /app
COPY tool.sh /usr/local/bin/tool
COPY data/*.txt ./
RUN chmod +x /usr/local/bin/tool && echo built > /app/built
LABEL maintainer="test"
EXPOSE 8080
CMD ["tool"]
""")
    lines = []
    img = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, out=lines.append)
    assert img.refs == ["docker.io/xcodon-test/built:latest"]
    cfg = img.config["config"]
    assert "GREETING=hi" in cfg["Env"] and "OTHER=two words" in cfg["Env"]
    assert cfg["WorkingDir"] == "/app" and cfg["Cmd"] == ["tool"] and cfg["Labels"]["maintainer"] == "test"
    assert (img.rootfs / "usr/local/bin/tool").exists() and (img.rootfs / "app/a.txt").read_text() == "A"
    assert any("EXPOSE" in l and "ignored" in l for l in lines)
    out = home.path / "out"
    with open(out, "wb") as f:
        assert rt.run("xcodon-test/built:latest", rm=True, stdout=f) == 0
    assert out.read_bytes() == b"tool-ran hi\n"
    # cache: a second identical build performs no RUN
    lines.clear()
    img2 = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, out=lines.append)
    assert img2.id == img.id
    assert sum("CACHED" in l for l in lines) >= 4
    # a changed RUN invalidates only from that step on
    (ctx / "Dockerfile").write_text((ctx / "Dockerfile").read_text().replace("echo built", "echo rebuilt"))
    img3 = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, no_cache=False)
    assert img3.id != img.id and (img3.rootfs / "app/built").read_text() == "rebuilt\n"


def test_build_failures(rt, tmp_path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("RUN echo before-from\n")
    with pytest.raises(XcodonError, match="FROM"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN exit 3\n")
    with pytest.raises(XcodonError, match="exit 3"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox AS stage\n")
    with pytest.raises(XcodonError, match="multi-stage"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY missing.txt /x\n")
    with pytest.raises(XcodonError, match="missing.txt"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY ../outside /x\n")
    with pytest.raises(XcodonError, match="context"):
        rt.build(ctx)
    assert rt.containers(all=True) == [], "no build containers left behind"
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_build.py`

- [ ] **Step 3: Write build.py**

```python
# src/xcodon_runtime/build.py
"""Run a Dockerfile subset in xrunner containers. See spec section 11.3."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
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
DEFAULT_SHELL = ["/bin/sh", "-c"]
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass
class Instruction:
    name: str
    args: str
    line: int


def parse_dockerfile(text: str) -> list[Instruction]:
    out: list[Instruction] = []
    buf: list[str] = []
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
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
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2)
        if name in args:
            return args[name]
        return default if default is not None else ""
    return _VAR.sub(sub, text)


def parse_command(args: str, shell: list[str]) -> list[str]:
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


def parse_env(args: str) -> dict[str, str]:
    s = args.strip()
    if "=" not in s.split(None, 1)[0]:
        key, _, value = s.partition(" ")
        return {key: value.strip()}
    out: dict[str, str] = {}
    for token in shlex.split(s):
        k, _, v = token.partition("=")
        out[k] = v
    return out


def _parse_paths(args: str) -> list[str]:
    s = args.strip()
    if s.startswith("["):
        parts = json.loads(s)
        return [str(p) for p in parts]
    tokens = shlex.split(s)
    return [t for t in tokens if not t.startswith("--")]


def _hash_tree(paths: Sequence[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        for root, dirs, files in os.walk(p) if p.is_dir() else [(str(p.parent), [], [p.name])]:
            dirs.sort()
            for name in sorted(files):
                full = Path(root) / name
                h.update(str(full.relative_to(p.parent)).encode())
                h.update(full.read_bytes() if full.is_file() else os.readlink(full).encode())
    return h.hexdigest()


class Builder:
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

    # -- build -------------------------------------------------------------------

    def build(self, dockerfile_text: str, tags: Sequence[str] = (), build_args: Mapping[str, str] | None = None,
              no_cache: bool = False) -> Image:
        instructions = parse_dockerfile(dockerfile_text)
        if not instructions:
            raise XcodonError("Dockerfile has no instructions")
        overrides = dict(build_args or {})
        args: dict[str, str] = {}
        shell = list(DEFAULT_SHELL)
        image: Image | None = None
        for n, ins in enumerate(instructions, 1):
            self.out(f"Step {n}/{len(instructions)} : {ins.name} {ins.args}")
            if ins.name == "ARG":
                name, has_default, default = ins.args.partition("=")
                name = name.strip()
                args[name] = overrides.get(name, args.get(name, expand_args(default, args) if has_default else ""))
                continue
            if ins.name == "FROM":
                ref = expand_args(ins.args, args).split()
                if len(ref) > 1:
                    raise XcodonError("multi-stage builds (FROM ... AS name) are not supported")
                image = self.rt.resolve_image(ref[0])
                self.out(f" ---> {image.short_id}")
                continue
            if image is None:
                raise XcodonError(f"line {ins.line}: FROM must come before {ins.name}")
            if ins.name in IGNORED:
                self.out(f" ---> {ins.name} is ignored by xrunner")
                log.warning("Dockerfile line %d: %s is ignored by xrunner", ins.line, ins.name)
                continue
            if ins.name == "SHELL":
                shell = parse_command(ins.args, shell)
                continue
            expanded = expand_args(ins.args, args)
            content_hash = ""
            sources: list[Path] = []
            if ins.name in ("COPY", "ADD"):
                sources = self._resolve_sources(_parse_paths(expanded)[:-1], ins)
                content_hash = _hash_tree(sources)
            key = hashlib.sha256(f"{image.id}|{ins.name}|{expanded}|{content_hash}".encode()).hexdigest()
            cached = None if no_cache else self._cache_get(key)
            if cached and self.rt.images.get(cached) is not None:
                image = self.rt.images.get(cached)
                self.out(f" ---> CACHED {image.short_id}")
                continue
            if ins.name == "RUN":
                image = self._run_step(image, parse_command(expanded, shell), args, expanded)
            elif ins.name in ("COPY", "ADD"):
                image = self._copy_step(image, sources, _parse_paths(expanded)[-1], expanded)
            else:
                image = self._config_step(image, ins.name, expanded)
            self.out(f" ---> {image.short_id}")
            self._cache_put(key, image.id)
        assert image is not None
        for t in tags:
            self.rt.images.tag(image.id, t)
        self.out(f"Successfully built {image.short_id}")
        return image

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

    def _run_step(self, image: Image, argv: list[str], args: Mapping[str, str], text: str) -> Image:
        c = self.rt.create(image.id, command=argv)
        try:
            self.rt.start(c)
            p = self.rt.popen(c, argv, env=dict(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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

    def _copy_step(self, image: Image, sources: list[Path], dest: str, text: str) -> Image:
        workdir = image.config.get("config", {}).get("WorkingDir") or "/"
        dest_abs = dest if dest.startswith("/") else os.path.join(workdir, dest)
        into_dir = dest.endswith("/") or len(sources) > 1 or dest_abs.endswith(".")
        work = Path(tempfile.mkdtemp(prefix="copy-", dir=self.rt.home.path))
        try:
            layer = work / "layer"
            for src in sources:
                target = layer / dest_abs.lstrip("/")
                if into_dir or src.is_dir() and target.exists():
                    target = target / src.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, target, follow_symlinks=False)
            return self.rt.images.commit(image, layer, created_by=f"COPY {text}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _config_step(self, image: Image, name: str, args: str) -> Image:
        if name == "ENV":
            changes = {"Env": [f"{k}={v}" for k, v in parse_env(args).items()]}
        elif name == "LABEL":
            changes = {"Labels": parse_env(args)}
        elif name == "WORKDIR":
            base = image.config.get("config", {}).get("WorkingDir") or "/"
            changes = {"WorkingDir": args if args.startswith("/") else os.path.join(base, args)}
        elif name == "USER":
            changes = {"User": args}
        elif name in ("CMD", "ENTRYPOINT"):
            changes = {"Cmd" if name == "CMD" else "Entrypoint": parse_command(args, DEFAULT_SHELL)}
        else:
            raise XcodonError(f"unsupported Dockerfile instruction {name}")
        return self.rt.images.commit(image, None, changes=changes, created_by=f"{name} {args}")
```

Note on `COPY` destination into a directory: when `src` is a directory and the destination does not end with `/`, docker copies the directory's *contents* into dest; `shutil.copytree(..., dirs_exist_ok=True)` onto `target` does that. When a single file is copied to a path ending in `/`, the file goes inside.

`Runtime.build`:

```python
    def build(self, context: str | Path, dockerfile: str | Path | None = None, tags: Sequence[str] = (),
              build_args: Mapping[str, str] | None = None, no_cache: bool = False,
              out: Callable[[str], None] | None = None) -> Image:
        from xcodon_runtime.build import Builder
        context = Path(context)
        if not context.is_dir():
            raise XcodonError(f"build context {context} is not a directory")
        df = Path(dockerfile) if dockerfile else context / "Dockerfile"
        try:
            text = df.read_text()
        except OSError as e:
            raise XcodonError(f"cannot read Dockerfile {df}: {e}") from e
        return Builder(self, context, out).build(text, tags=tags, build_args=build_args, no_cache=no_cache)
```

- [ ] **Step 4: Run tests, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_build.py` (both engines) then the full suite; `pgrep -af xcodon_runtime.keeper` empty.

```bash
git add src/xcodon_runtime/build.py src/xcodon_runtime/api.py tests/test_build.py
git commit -m "feat: xrunner build runs a Dockerfile subset in its own containers with a step cache"
```

---

### Task 4: CLI verbs, docker dispatcher, and the shim

**Files:**
- Modify: `src/xcodon_runtime/cli.py`
- Modify: `README.md`
- Test: `tests/test_cli_docker.py`

**Interfaces:**
- Produces CLI: `xrunner build [-t TAG]... [-f FILE] [--build-arg K=V]... [--no-cache] [-q] CONTEXT`; `xrunner commit [-m MSG] [-c CHANGE]... [--env-dir DIR --image IMG | CONTAINER] TAG`; `xrunner tag SRC DST`; `xrunner image inspect|ls|rm ...`; `xrunner docker VERB ...`; `xrunner shim install [--dir DIR] [--force]`; `cli.translate_docker_argv(argv: list[str]) -> list[str]` (docker argv -> xrunner argv, raises `UsageError` for unknown verbs); `cli.SHIM_NAME = "docker"`.
- `_split_argv` also splits after `run`/`create` when they follow `docker`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_docker.py
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from xcodon_runtime import cli


def test_translate_docker_argv():
    t = cli.translate_docker_argv
    assert t(["build", "-t", "x/y:1", "ctx"]) == ["build", "-t", "x/y:1", "ctx"]
    assert t(["image", "inspect", "x/y:1"]) == ["inspect", "x/y:1"]
    assert t(["image", "inspect", "--format", "{{.Id}}", "x"]) == ["inspect", "x"]
    assert t(["images"]) == ["images"] and t(["image", "ls"]) == ["images"]
    assert t(["image", "rm", "x"]) == ["rmi", "x"] and t(["rmi", "x"]) == ["rmi", "x"]
    assert t(["run", "--rm", "img", "sh", "-c", "x"]) == ["run", "--rm", "img", "sh", "-c", "x"]
    assert t(["version"]) == ["info"]
    with pytest.raises(cli.UsageError, match="compose"):
        t(["compose", "up"])


def test_split_argv_handles_docker_run():
    assert cli._split_argv(["docker", "run", "--rm", "img", "grep", "-v", "x"]) == (["docker", "run"], ["--rm", "img", "grep", "-v", "x"])
    assert cli._split_argv(["docker", "build", "-t", "x", "."]) == (["docker", "build", "-t", "x", "."], None)


def test_build_commit_tag_flow(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo built > /built\nCMD [\"/bin/cat\", \"/built\"]\n")
    assert cli.main([*e, "build", "-t", "xcodon-test/cli-built:1", str(ctx)]) == 0
    out = capfd.readouterr().out
    assert "Successfully built" in out
    assert cli.main([*e, "run", "--rm", "xcodon-test/cli-built:1"]) == 0
    assert capfd.readouterr().out == "built\n"
    assert cli.main([*e, "tag", "xcodon-test/cli-built:1", "xcodon-test/cli-built:latest"]) == 0
    assert cli.main([*e, "image", "inspect", "xcodon-test/cli-built:latest"]) == 0
    assert json.loads(capfd.readouterr().out)[0]["Config"]["Cmd"] == ["/bin/cat", "/built"]
    assert cli.main([*e, "docker", "image", "inspect", "nope/none:latest"]) == 1
    capfd.readouterr()
    assert cli.main([*e, "create", "--name", "cc", "xcodon-test/busybox", "/bin/sh"]) == 0
    assert cli.main([*e, "start", "cc"]) == 0
    assert cli.main([*e, "exec", "cc", "/bin/sh", "-c", "echo c > /committed"]) == 0
    assert cli.main([*e, "stop", "cc"]) == 0
    assert cli.main([*e, "commit", "-m", "snap", "-c", "ENV SNAP=1", "cc", "xcodon-test/snap:1"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "run", "--rm", "xcodon-test/snap:1", "/bin/sh", "-c", "cat /committed; echo $SNAP"]) == 0
    assert capfd.readouterr().out == "c\n1\n"
    assert cli.main([*e, "rm", "cc"]) == 0


def test_docker_dispatcher_build_and_inspect(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo d > /d\n")
    assert cli.main([*e, "docker", "build", "-t", "xcodon-test/via-docker:latest", str(ctx)]) == 0
    capfd.readouterr()
    assert cli.main([*e, "docker", "image", "inspect", "xcodon-test/via-docker:latest"]) == 0
    assert json.loads(capfd.readouterr().out)[0]["Id"].startswith("sha256:")
    assert cli.main([*e, "docker", "compose", "up"]) == 125


def test_shim_install_and_use(home, busybox_image, engine_name, tmp_path, monkeypatch):
    shim_dir = tmp_path / "shimbin"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no real docker visible
    assert cli.main(["shim", "install", "--dir", str(shim_dir)]) == 0
    shim = shim_dir / "docker"
    assert shim.exists() and os.access(shim, os.X_OK)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo s > /s\n")
    env = {**os.environ, "PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(["docker", "build", "-t", "xcodon-test/shim:latest", str(ctx)], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(["docker", "image", "inspect", "xcodon-test/shim:latest"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)[0]["Id"].startswith("sha256:")
    r = subprocess.run(["docker", "image", "inspect", "nope/none"], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    # refuses to shadow a real docker unless forced
    fake = tmp_path / "realbin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\necho real\n")
    (fake / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "other")]) == 125
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "other"), "--force"]) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_cli_docker.py`

- [ ] **Step 3: Implement in cli.py**

Add imports `shlex`, `shutil`, `stat`. New commands:

```python
SHIM_NAME = "docker"
DOCKER_VERBS = {
    "build": ["build"], "pull": ["pull"], "images": ["images"], "rmi": ["rmi"], "tag": ["tag"],
    "run": ["run"], "create": ["create"], "start": ["start"], "exec": ["exec"], "stop": ["stop"],
    "rm": ["rm"], "ps": ["ps"], "logs": ["logs"], "commit": ["commit"], "inspect": ["inspect"],
    "version": ["info"], "info": ["info"],
}
DOCKER_IMAGE_VERBS = {"inspect": ["inspect"], "ls": ["images"], "list": ["images"], "rm": ["rmi"], "remove": ["rmi"]}
_INSPECT_DROP = {"--format", "-f", "--type"}


def translate_docker_argv(argv: list[str]) -> list[str]:
    """Map docker's verbs to xrunner's. Raises UsageError for a verb xrunner does not offer."""
    if not argv:
        raise UsageError("docker: a verb is required (build, image inspect, run, ...)")
    verb, rest = argv[0], list(argv[1:])
    if verb == "image":
        if not rest or rest[0] not in DOCKER_IMAGE_VERBS:
            raise UsageError(f"docker image {rest[:1]}: not supported by xrunner")
        head = DOCKER_IMAGE_VERBS[rest[0]]
        rest = rest[1:]
        verb = "inspect" if head == ["inspect"] else head[0]
    elif verb in DOCKER_VERBS:
        head = DOCKER_VERBS[verb]
    else:
        raise UsageError(f"docker {verb}: not supported by xrunner")
    if verb == "inspect":
        cleaned = []
        skip = False
        for tok in rest:
            if skip:
                skip = False
                continue
            if tok in _INSPECT_DROP:
                skip = True
                continue
            if tok.startswith("--format=") or tok.startswith("--type="):
                continue
            cleaned.append(tok)
        rest = cleaned
    return head + rest


def cmd_build(rt: Runtime, args) -> int:
    build_args = {}
    for item in args.build_arg or []:
        k, _, v = item.partition("=")
        build_args[k] = v if _ else os.environ.get(k, "")
    out = (lambda line: None) if args.quiet else (lambda line: print(line, flush=True))
    img = rt.build(args.context, dockerfile=args.file, tags=args.tag or [], build_args=build_args, no_cache=args.no_cache, out=out)
    if args.quiet:
        print(f"sha256:{img.id}")
    return 0


def _parse_change(spec: str) -> dict:
    from xcodon_runtime.build import DEFAULT_SHELL, parse_command, parse_env
    name, _, rest = spec.strip().partition(" ")
    name = name.upper()
    if name == "ENV":
        return {"Env": [f"{k}={v}" for k, v in parse_env(rest).items()]}
    if name == "LABEL":
        return {"Labels": parse_env(rest)}
    if name == "WORKDIR":
        return {"WorkingDir": rest.strip()}
    if name == "USER":
        return {"User": rest.strip()}
    if name in ("CMD", "ENTRYPOINT"):
        return {"Cmd" if name == "CMD" else "Entrypoint": parse_command(rest, DEFAULT_SHELL)}
    raise UsageError(f"unsupported --change {spec!r}; use ENV, LABEL, WORKDIR, USER, CMD, or ENTRYPOINT")


def cmd_commit(rt: Runtime, args) -> int:
    changes: dict = {}
    for spec in args.change or []:
        for k, v in _parse_change(spec).items():
            if k == "Env":
                changes.setdefault("Env", []).extend(v)
            elif k == "Labels":
                changes.setdefault("Labels", {}).update(v)
            else:
                changes[k] = v
    if args.env_dir:
        if not args.image:
            raise UsageError("commit --env-dir also needs --image IMAGE")
        img = rt.commit(None, args.tag, env_dir=os.path.abspath(os.path.expanduser(args.env_dir)), image=args.image, changes=changes, message=args.message or "")
    else:
        if not args.container:
            raise UsageError("commit needs a CONTAINER, or --env-dir DIR --image IMAGE")
        img = rt.commit(rt.get_container(args.container), args.tag, changes=changes, message=args.message or "")
    print(f"sha256:{img.id}")
    return 0


def cmd_tag(rt: Runtime, args) -> int:
    rt.images.tag(args.source, args.target)
    return 0


def cmd_docker(rt: Runtime, args) -> int:
    translated = translate_docker_argv(list(args.rest))
    return main(translated, _runtime=rt)


def cmd_shim(rt: Runtime, args) -> int:
    target_dir = Path(args.dir or os.path.dirname(sys.executable)).expanduser().resolve()
    existing = shutil.which(SHIM_NAME)
    if existing and not args.force:
        try:
            is_ours = "xrunner docker" in Path(existing).read_text(errors="ignore")
        except OSError:
            is_ours = False
        if not is_ours:
            raise UsageError(f"a real docker is on PATH at {existing}; pass --force to install the shim anyway")
    xrunner = os.path.join(os.path.dirname(sys.executable), "xrunner")
    if not os.access(xrunner, os.X_OK):
        xrunner = shutil.which("xrunner") or "xrunner"
    target_dir.mkdir(parents=True, exist_ok=True)
    shim = target_dir / SHIM_NAME
    shim.write_text(f"#!/bin/sh\n# docker shim installed by xrunner; forwards docker verbs to xrunner\nexec {shlex.quote(xrunner)} docker \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {shim}")
    if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f'add it to PATH: export PATH="{target_dir}:$PATH"')
    return 0
```

Parser additions in `build_parser`:

```python
    s = sub.add_parser("build", help="build an image from a Dockerfile subset")
    s.add_argument("-t", "--tag", action="append")
    s.add_argument("-f", "--file")
    s.add_argument("--build-arg", action="append")
    s.add_argument("--no-cache", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    for ignored in ("--pull", "--rm", "--force-rm", "--progress", "--platform", "--network", "--label"):
        s.add_argument(ignored, nargs="?", default=None, help=argparse.SUPPRESS)
    s.add_argument("context")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("commit", help="snapshot a stopped container or an env folder layer as an image")
    s.add_argument("-m", "--message")
    s.add_argument("-c", "--change", action="append")
    s.add_argument("--env-dir")
    s.add_argument("--image")
    s.add_argument("container", nargs="?")
    s.add_argument("tag")
    s.set_defaults(func=cmd_commit)

    s = sub.add_parser("tag", help="add a tag to an image")
    s.add_argument("source")
    s.add_argument("target")
    s.set_defaults(func=cmd_tag)

    s = sub.add_parser("image", help="docker-style image commands")
    isub = s.add_subparsers(dest="image_cmd", required=True)
    i = isub.add_parser("inspect"); i.add_argument("image"); i.set_defaults(func=cmd_inspect)
    isub.add_parser("ls").set_defaults(func=cmd_images)
    i = isub.add_parser("rm"); i.add_argument("image"); i.set_defaults(func=cmd_rmi)

    s = sub.add_parser("docker", help="accept docker verbs (build, image inspect, run, ...)", add_help=False)
    s.set_defaults(func=cmd_docker, rest=[])

    s = sub.add_parser("shim", help="install a docker command that forwards to xrunner")
    ssub = s.add_subparsers(dest="shim_cmd", required=True)
    i = ssub.add_parser("install"); i.add_argument("--dir"); i.add_argument("--force", action="store_true"); i.set_defaults(func=cmd_shim)
```

`commit` with `--env-dir`: the positional `container` is optional, so `xrunner commit --env-dir D --image I TAG` parses TAG into `container` and leaves `tag` empty; handle that in `cmd_commit` by shifting: if `args.env_dir and args.tag is None: args.tag = args.container; args.container = None`. Make `tag` `nargs="?"` for that.

`_split_argv`: treat `docker` as a pass-through token so that `docker run ...` and `docker create ...` split after the verb, and everything else after `docker` is left whole:

```python
        if tok == "docker":
            if i + 1 < len(argv) and argv[i + 1] in ("run", "create"):
                return argv[: i + 2], argv[i + 2 :]
            return argv[: i + 1], argv[i + 1 :]
```

`main` gains a keyword `_runtime: Runtime | None = None` used by `cmd_docker` to re-enter with the same `Runtime`; when `rest` is not None and the subcommand is `docker`, set `args.rest = rest` (the whole tail). The `run`/`create` pre-split logic stays for those verbs.

README: add a "Build and commit" section with the three commands and the shim instructions, and note in Limits: no multi-stage, no `.dockerignore`.

- [ ] **Step 4: Run tests, commit**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q tests/test_cli_docker.py tests/test_cli.py` then the full suite; `ruff check src tests`.

```bash
git add src/xcodon_runtime/cli.py README.md tests/test_cli_docker.py
git commit -m "feat: build, commit, tag, image verbs, docker dispatcher, and an installable docker shim"
```

---

### Task 5: End to end with the agent's own recipe

**Files:**
- Test: `tests/test_build_e2e.py`

**Interfaces:** none new.

- [ ] **Step 1: Write the test**

```python
# tests/test_build_e2e.py
"""The agent's real recipe: FROM the stock coala-runtime image, RUN pip install, reuse by tag via the docker shim."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from xcodon_runtime import cli
from xcodon_runtime.api import Runtime

pytestmark = pytest.mark.docker  # needs the stock image, which comes from the local daemon


def test_agent_recipe_builds_and_runs_without_docker_commands(home, engine_name, tmp_path, monkeypatch):
    rt = Runtime(home.path, engine=engine_name)
    rt.pull("coala-runtime-python:latest")
    ctx = tmp_path / "built-python-deps"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM coala-runtime-python:latest\nRUN pip install --no-cache-dir tabulate\n")
    shim_dir = tmp_path / "shimbin"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.main(["shim", "install", "--dir", str(shim_dir)]) == 0
    env = {**os.environ, "PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(["docker", "build", "-t", "xcodon/e2e-python-deps:latest", str(ctx)], env=env, capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    r = subprocess.run(["docker", "image", "inspect", "xcodon/e2e-python-deps:latest"], env=env, capture_output=True, text=True)
    assert r.returncode == 0
    out = tmp_path / "out"
    with open(out, "wb") as f:
        assert rt.run("xcodon/e2e-python-deps:latest", command=["python", "-c", "import tabulate; print(tabulate.__version__)"], rm=True, stdout=f) == 0
    assert out.read_bytes().strip()
```

- [ ] **Step 2: Run it**

Run: `PATH=$PWD/.venv/bin:/usr/bin:/bin .venv/bin/pytest -q -m docker tests/test_build_e2e.py -v` (both engines). Expected: pass; the pip install runs inside an xrunner container, no docker command is invoked by the build.

- [ ] **Step 3: Commit**

```bash
git add tests/test_build_e2e.py
git commit -m "test: the agent's pip recipe builds through the docker shim without docker"
```

---

## Plan Self-Review

- Spec 11.2 commit: Task 1 (translation, hashing), Task 2 (compose image, config changes, refusal when in use, config-only). 11.3 build: Task 3 (instructions, cache, ignored set, errors), untagged prune in Task 2. 11.4 aliases, dispatcher, shim: Task 4. 11.5 daemon-id check: Task 2. 11.6 out of scope: nothing added.
- Names consistent: `snapshot_upper`, `snapshot_diff`, `hash_layer_dir`, `link_tree`; `ImageStore.commit/tag/layer_dirs/untagged`; `Runtime.commit/build`; `Builder`, `parse_dockerfile`, `expand_args`, `parse_command`, `parse_env`; `translate_docker_argv`, `SHIM_NAME`.
- No placeholders.
