# tests/fake_micromamba.py
"""A stand-in for micromamba: records each call and fakes just enough behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPT = r'''
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_MM_LOG")
keys = ["HOME", "MAMBA_ROOT_PREFIX", "CONDA_PKGS_DIRS", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "CONDARC", "CONDA_PREFIX"]
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd(),
                            "env": {k: os.environ.get(k) for k in keys}}) + "\n")
if "-d" in args:
    # Real micromamba 2.9.0 has no `-d` (only `--dry-run`) and rejects an unknown
    # option outright; a caller must translate conda's `-d` itself before this point.
    sys.stderr.write("The following argument was not expected: -d\n")
    sys.exit(2)
if args == ["--version"]:
    print("2.9.0")
    sys.exit(0)
if args[:2] == ["env", "export"]:
    code = int(os.environ.get("FAKE_MM_EXPORT_EXIT", "0"))
    if code == 0:
        print("@EXPLICIT")
        print("https://conda.anaconda.org/bioconda/linux-64/seqtk-1.5-h577a1d6_1.tar.bz2#0bc157aea007a7895e6f2e8f44a0b407")
    sys.exit(code)
code = int(os.environ.get("FAKE_MM_EXIT", "0"))
if code == 0 and args and args[0] in ("create", "install"):
    root = os.environ["MAMBA_ROOT_PREFIX"]
    prefix = root
    for i, a in enumerate(args):
        if a in ("-p", "--prefix"):
            prefix = os.path.join(os.getcwd(), args[i + 1])
        elif a in ("-n", "--name") and args[i + 1] != "base":
            prefix = os.path.join(root, "envs", args[i + 1])
    os.makedirs(os.path.join(prefix, "conda-meta"), exist_ok=True)
    os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
elif code == 0 and args[:2] == ["env", "create"]:
    # Real micromamba 2.9.0: -p wins, else -n, else the file's top-level `name:` only
    # (a `prefix:` line -- which `conda env export` also writes -- is ignored); with
    # none of those it exits 1, "No target prefix specified".
    root = os.environ["MAMBA_ROOT_PREFIX"]
    prefix = None
    name = None
    for i, a in enumerate(args):
        if a in ("-p", "--prefix"):
            prefix = os.path.join(os.getcwd(), args[i + 1])
        elif a in ("-n", "--name") and args[i + 1] != "base":
            name = args[i + 1]
        elif a in ("-f", "--file") and name is None:
            try:
                with open(args[i + 1]) as fh:
                    text = fh.read()
            except OSError:
                text = ""
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line[5:].split("#", 1)[0].strip().strip("'\"")
                    break
    if prefix is None and name and name != "base":
        prefix = os.path.join(root, "envs", name)
    if prefix is None:
        sys.stderr.write("No target prefix specified\n")
        sys.exit(1)
    os.makedirs(os.path.join(prefix, "conda-meta"), exist_ok=True)
    os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
sys.exit(code)
'''


def make_fake_micromamba(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "micromamba"
    path.write_text(f"#!{sys.executable}\n{_SCRIPT}")
    path.chmod(0o755)
    return path


def read_log(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
