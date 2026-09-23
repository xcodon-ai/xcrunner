#!/usr/bin/env python3
# scripts/fetch_proot.py
"""Download the PRoot static binary we vendor and record its checksum.

PRoot is GPL-2.0. It is distributed next to this MIT package as a separate
program; its license text is kept beside the binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.request
from pathlib import Path

VERSION = "v5.4.1"
URL = f"https://github.com/proot-me/proot/releases/download/{VERSION}/proot"
SHA256 = "19f44283f5c0e73091c60195f5fcd4f4c1165505e44410d434e2ab1b677c1a09"
LICENSE_URL = f"https://raw.githubusercontent.com/proot-me/proot/{VERSION}/COPYING"
DEST_DIR = Path(__file__).resolve().parents[1] / "src" / "xcodon_runtime" / "_bin"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main() -> None:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    data = download(URL)
    got = hashlib.sha256(data).hexdigest()
    if got != SHA256:
        raise SystemExit(f"checksum mismatch: expected {SHA256}, got {got}")
    target = DEST_DIR / "proot-x86_64"
    target.write_bytes(data)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (DEST_DIR / "LICENSE-proot").write_bytes(download(LICENSE_URL))
    (DEST_DIR / "MANIFEST").write_text(
        json.dumps({"proot-x86_64": {"version": VERSION, "sha256": SHA256, "url": URL}}, indent=2) + "\n"
    )
    print(f"wrote {target} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
