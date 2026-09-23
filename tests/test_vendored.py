# tests/test_vendored.py
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "src" / "xcodon_runtime" / "_bin"


def test_manifest_matches_binaries():
    manifest = json.loads((BIN / "MANIFEST").read_text())
    assert "proot-x86_64" in manifest
    for name, meta in manifest.items():
        path = BIN / name
        assert path.exists(), f"{name} missing; run scripts/fetch_proot.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]
        assert os.access(path, os.X_OK)
    assert "GNU" in (BIN / "LICENSE-proot").read_text()[:300]


@pytest.mark.skipif(platform.machine() != "x86_64", reason="vendored binary is x86_64")
def test_vendored_proot_runs():
    out = subprocess.run([str(BIN / "proot-x86_64"), "--version"], capture_output=True, text=True)
    assert out.returncode == 0
    assert "proot" in (out.stdout + out.stderr).lower()
