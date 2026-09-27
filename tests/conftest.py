from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from xcodon_runtime.home import RuntimeHome
from xcodon_runtime.spec import DEFAULT_PATH

_PROBE_HOME: Path | None = None


def probe_home() -> Path:
    """A throwaway runtime home for the probes.

    The probes run at collection time and on fixture setup. Without a home of
    their own they would create the developer's real ``~/.xcodon/runtime``.
    """
    global _PROBE_HOME
    if _PROBE_HOME is None:
        _PROBE_HOME = Path(tempfile.mkdtemp(prefix="xcodon-probe-home-"))
    return _PROBE_HOME


def pytest_sessionfinish(session, exitstatus):
    if _PROBE_HOME is not None:
        shutil.rmtree(_PROBE_HOME, ignore_errors=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> RuntimeHome:
    """A fresh runtime home under tmp_path. Also exported via env for subprocesses."""
    root = tmp_path / "runtime-home"
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(root))
    monkeypatch.delenv("XCRUNNER_CONTAINER_DIR", raising=False)
    monkeypatch.delenv("XCRUNNER_ENV_LAYER_DIR", raising=False)
    return RuntimeHome(root)


def _busybox() -> Path | None:
    for candidate in ("/usr/bin/busybox", "/bin/busybox"):
        p = Path(candidate)
        if p.exists():
            out = subprocess.run(["file", "-L", str(p)], capture_output=True, text=True).stdout
            if "statically linked" in out:
                return p
    return None


BUSYBOX = _busybox()
APPLETS = ["sh", "id", "hostname", "ls", "cat", "head", "touch", "sleep", "echo", "env", "pwd", "true", "false",
           "rm", "mkdir", "chmod", "mkfifo"]


def build_busybox_rootfs(dest: Path) -> Path:
    """A tiny rootfs with a static busybox and the applets tests need."""
    assert BUSYBOX is not None
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    for d in ("etc", "tmp", "root", "home/user", "proc", "sys", "dev", "workspace"):
        (dest / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUSYBOX, dest / "bin" / "busybox")
    for applet in APPLETS:
        link = dest / "bin" / applet
        if not link.exists():
            link.symlink_to("busybox")
    (dest / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\nuser:x:1000:1000:user:/home/user:/bin/sh\n"
    )
    (dest / "etc" / "group").write_text("root:x:0:\nuser:x:1000:\nstaff:x:50:\n")
    os.chmod(dest / "tmp", 0o777)
    return dest


@pytest.fixture
def busybox_rootfs(tmp_path: Path) -> Path:
    if BUSYBOX is None:
        pytest.skip("no static busybox on this host")
    return build_busybox_rootfs(tmp_path / "bbroot")


def pytest_collection_modifyitems(config, items):
    """Skip marked tests whose prerequisites are missing."""
    probes = None
    for item in items:
        if "ns" in item.keywords:
            from xcodon_runtime.probe import run_probes

            if probes is None:
                probes = run_probes(probe_home())
            if not all(probes[k]["ok"] for k in ("userns", "overlay", "pidns_proc")):
                item.add_marker(pytest.mark.skip(reason=f"ns engine unavailable: {probes}"))
        if "docker" in item.keywords:
            if shutil.which("docker") is None or subprocess.run(
                ["docker", "version"], capture_output=True
            ).returncode != 0:
                item.add_marker(pytest.mark.skip(reason="no docker daemon"))
        if "cwltool" in item.keywords and shutil.which("cwltool") is None:
            item.add_marker(pytest.mark.skip(reason="cwltool not installed"))
        if "network" in item.keywords and os.environ.get("XCODON_TEST_NETWORK") != "1":
            item.add_marker(pytest.mark.skip(reason="set XCODON_TEST_NETWORK=1 to run network tests"))
        if "proot" in item.keywords:
            from xcodon_runtime.engine_proot import find_proot

            if find_proot() is None:
                item.add_marker(pytest.mark.skip(reason="no proot binary"))


def _ns_available() -> bool:
    from xcodon_runtime.probe import run_probes

    return all(v["ok"] for v in run_probes(probe_home()).values())


def _proot_available() -> bool:
    from xcodon_runtime.engine_proot import find_proot

    return find_proot() is not None


@pytest.fixture(params=["ns", "proot"])
def engine_name(request) -> str:
    name = request.param
    if name == "ns" and not _ns_available():
        pytest.skip("ns engine unavailable on this host")
    if name == "proot" and not _proot_available():
        pytest.skip("no proot binary")
    return name


def pack_rootfs_as_image(home: RuntimeHome, rootfs: Path, ref: str, config: dict | None = None):
    """Import a directory as a one-layer image into ``home`` under ``ref``. Returns the Image."""
    from xcodon_runtime.imagestore import ImageStore
    from xcodon_runtime.registry import FetchedImage, FetchedLayer

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        t.add(rootfs, arcname=".")
    raw = buf.getvalue()
    gz = gzip.compress(raw)
    blob = home.blobs / hashlib.sha256(gz).hexdigest()
    blob.write_bytes(gz)
    cfg = {
        "architecture": "amd64",
        "os": "linux",
        # A full default PATH (matching build_spec.DEFAULT_PATH), the way a
        # real busybox image declares one, so a COPY into /usr/local/bin is
        # actually reachable by a bare command name.
        "config": {"Env": [f"PATH={DEFAULT_PATH}"], "Cmd": ["/bin/sh"], "WorkingDir": "/workspace"},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(raw).hexdigest()]},
    }
    if config:
        cfg["config"].update(config)
    cbytes = json.dumps(cfg).encode()
    fetched = FetchedImage("sha256:" + hashlib.sha256(cbytes).hexdigest(), cfg,
                           [FetchedLayer("sha256:" + hashlib.sha256(gz).hexdigest(), "tar+gzip", len(gz), blob)],
                           source="test")
    from xcodon_runtime.reference import parse_reference

    return ImageStore(home, sources=[]).import_fetched(fetched, parse_reference(ref).name)


@pytest.fixture
def busybox_image(home, busybox_rootfs):
    return pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/busybox:latest")


@pytest.fixture
def mac(home, tmp_path, monkeypatch):
    """A faked Apple silicon Mac with a fake Lima on PATH. Yields the fake folders and an environ."""
    from tests.fake_lima import make_fakes
    from xcodon_runtime import macvm

    fakes = make_fakes(tmp_path / "fakes")
    mac_home = tmp_path / "machome"
    mac_home.mkdir()
    monkeypatch.setenv("HOME", str(mac_home))
    monkeypatch.setenv("PATH", f"{fakes['bin']}:/usr/bin:/bin")
    monkeypatch.setenv("LIMA_HOME", str(fakes["lima_home"]))
    monkeypatch.setenv("FAKE_LIMA_STATE", str(fakes["state"]))
    monkeypatch.setenv("FAKE_VM_HOME", str(fakes["vm_home"]))
    for name in ("XCRUNNER_MACHINE_NAME", "XCRUNNER_MACHINE_CPUS", "XCRUNNER_MACHINE_MEMORY",
                 "XCRUNNER_MACHINE_DISK", "XCRUNNER_MACHINE_MOUNTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(macvm, "check_host", lambda: None)
    if not Path("/private/tmp").is_dir():
        # pytest's tmp_path is under /tmp. On a Mac, the VM shares the Mac's /tmp as
        # /private/tmp; this Linux host has none, so keep paths as they are here.
        # The rewrite itself is covered by the unit tests in test_macvm_forward.py.
        monkeypatch.setattr(macvm, "mac_path", lambda path: path)
    monkeypatch.setattr(macvm, "is_macos", lambda: True)
    monkeypatch.chdir(mac_home)
    fakes["mac_home"] = mac_home
    return fakes
