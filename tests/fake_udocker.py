"""A stand-in for the udocker command: records each call and fakes install, import, setup and run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPT = r'''
import json, os, sys, tarfile, uuid
args = sys.argv[1:]
log = os.environ["FAKE_UDOCKER_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": args, "containers": os.environ.get("UDOCKER_CONTAINERS"),
                        "udocker_dir": os.environ.get("UDOCKER_DIR")}) + "\n")
if args and args[0] == "--quiet":
    args = args[1:]
cmd = args[0]
cdir = os.environ.get("UDOCKER_CONTAINERS", "")
if cmd == "install":
    lib = os.path.join(os.environ["UDOCKER_DIR"], "lib")
    os.makedirs(lib, exist_ok=True)
    open(os.path.join(lib, "libfakechroot-Fake-x86_64.so"), "w").close()
    sys.exit(int(os.environ.get("FAKE_UDOCKER_INSTALL_EXIT", "0")))
if cmd == "import":
    name = next(a.split("=", 1)[1] for a in args if a.startswith("--name="))
    cid = str(uuid.uuid4())
    root = os.path.join(cdir, cid, "ROOT")
    os.makedirs(root)
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as t:
        t.extractall(root, filter="tar")
    os.symlink(cid, os.path.join(cdir, name))
    print(cid)
    sys.exit(0)
if cmd == "setup":
    mode = next(a.split("=", 1)[1] for a in args if a.startswith("--execmode="))
    open(os.path.join(cdir, args[-1], "execmode"), "w").write(mode)
    sys.exit(int(os.environ.get("FAKE_UDOCKER_SETUP_EXIT", "0")))
if cmd == "run":
    print("fake udocker ran", " ".join(a for a in args if not a.startswith("--")))
    sys.exit(int(os.environ.get("FAKE_UDOCKER_EXIT", "0")))
sys.exit(2)
'''


def make_fake_udocker(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / "udocker"
    p.write_text(f"#!{sys.executable}\n{_SCRIPT}")
    p.chmod(0o755)
    return p


def read_log(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines()]
    except FileNotFoundError:
        return []
