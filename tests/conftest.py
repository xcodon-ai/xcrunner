from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from xcodon_runtime.home import RuntimeHome


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> RuntimeHome:
    """A fresh runtime home under tmp_path. Also exported via env for subprocesses."""
    root = tmp_path / "runtime-home"
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(root))
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
APPLETS = ["sh", "id", "hostname", "ls", "cat", "head", "touch", "sleep", "echo", "env", "pwd", "true", "false"]


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
            try:
                from xcodon_runtime.probe import run_probes
            except ImportError:
                item.add_marker(pytest.mark.skip(reason="probe module not yet available (Task 9)"))
                continue
            if probes is None:
                probes = run_probes()
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
            try:
                from xcodon_runtime.engine_proot import find_proot
            except ImportError:
                item.add_marker(pytest.mark.skip(reason="engine_proot module not yet available (Task 11)"))
                continue

            if find_proot() is None:
                item.add_marker(pytest.mark.skip(reason="no proot binary"))
