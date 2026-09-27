"""Stand-ins for `limactl` and Lima's `ssh`, for the macOS tests on Linux.

The fake limactl keeps its instances in $FAKE_LIMA_STATE/instances.json and makes
instance folders under $LIMA_HOME. The fake ssh runs the remote command locally
with `sh -c`, in $FAKE_VM_HOME, where `.xrunner-vm/venv/bin/python` is this
test's Python. So a forwarded command runs the real forwarded.py and CLI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_LIMACTL = r'''
import json, os, shutil, sys
state = os.environ["FAKE_LIMA_STATE"]
path = os.path.join(state, "instances.json")
lima_home = os.environ["LIMA_HOME"]
args = sys.argv[1:]
with open(os.path.join(state, "limactl.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
try:
    inst = json.load(open(path))
except FileNotFoundError:
    inst = {}
def save():
    json.dump(inst, open(path, "w"))
if args == ["--version"]:
    print("limactl version " + os.environ.get("FAKE_LIMA_VERSION", "2.0.3"))
    sys.exit(0)
cmd = args[0]
if cmd == "list":
    for v in inst.values():
        print(json.dumps(v))
    sys.exit(0)
if cmd == "create":
    code = int(os.environ.get("FAKE_LIMA_CREATE_EXIT", "0"))
    if code:
        print("create failed", file=sys.stderr)
        sys.exit(code)
    name = next(a.split("=", 1)[1] for a in args if a.startswith("--name="))
    d = os.path.join(lima_home, name)
    os.makedirs(d, exist_ok=True)
    shutil.copy(args[-1], os.path.join(d, "lima.yaml"))
    open(os.path.join(d, "ssh.config"), "w").write("Host lima-" + name + "\n")
    inst[name] = {"name": name, "status": "Stopped", "dir": d, "cpus": 4, "memory": 4294967296,
                  "disk": 107374182400}
    save()
    sys.exit(0)
name = args[-1]
if cmd == "start":
    code = int(os.environ.get("FAKE_LIMA_START_EXIT", "0"))
    if code:
        open(os.path.join(inst[name]["dir"], "ha.stderr.log"), "w").write("boot\nrosetta is not installed\n")
        print("start failed", file=sys.stderr)
        sys.exit(code)
    inst[name]["status"] = "Running"
elif cmd == "stop":
    inst[name]["status"] = "Stopped"
elif cmd == "delete":
    shutil.rmtree(inst.pop(name)["dir"], ignore_errors=True)
elif cmd == "shell":
    sys.exit(0)
save()
'''

_SSH = r'''
import json, os, subprocess, sys
state = os.environ["FAKE_LIMA_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "ssh.log"), "a") as f:
    f.write(json.dumps(args) + "\n")
if "FAKE_SSH_EXIT" in os.environ:
    sys.exit(int(os.environ["FAKE_SSH_EXIT"]))
remote = args[args.index("--") + 1]
if "pip install" in remote:
    sys.exit(int(os.environ.get("FAKE_SSH_PIP_EXIT", "0")))
if "print(xcodon_runtime.__version__)" in remote and "FAKE_VM_VERSION" in os.environ:
    print(os.environ["FAKE_VM_VERSION"])
    sys.exit(0)
sys.exit(subprocess.run(["sh", "-c", remote], cwd=os.environ["FAKE_VM_HOME"]).returncode)
'''


def make_fakes(root: Path) -> dict[str, Path]:
    """Write fake `limactl` and `ssh` into ``root/bin``; return the folders tests point env vars at."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (("limactl", _LIMACTL), ("ssh", _SSH)):
        p = bin_dir / name
        p.write_text(f"#!{sys.executable}\n{body}")
        p.chmod(0o755)
    state = root / "state"
    state.mkdir()
    vm_home = root / "vm-home"
    venv_bin = vm_home / ".xrunner-vm" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    # A wrapper, not a link: a link to a venv's python loses the venv's packages.
    py = venv_bin / "python"
    py.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    py.chmod(0o755)
    lima_home = root / "lima-home"
    lima_home.mkdir()
    return {"bin": bin_dir, "state": state, "vm_home": vm_home, "lima_home": lima_home}


def log(state: Path, name: str) -> list[list[str]]:
    try:
        return [json.loads(line) for line in (state / f"{name}.log").read_text().splitlines()]
    except FileNotFoundError:
        return []


def instances(state: Path) -> dict:
    try:
        return json.loads((state / "instances.json").read_text())
    except FileNotFoundError:
        return {}


__all__ = ["make_fakes", "log", "instances", "os"]
