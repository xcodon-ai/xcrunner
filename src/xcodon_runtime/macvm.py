"""xcrunner on macOS: one Lima VM runs the Linux xcrunner. See spec section 16.

On macOS, ``cli.main`` hands every call to :func:`mac_main`. A few subcommands
run on the Mac itself (``conda``, ``shim``, ``sandbox``, ``machine``, and
``--version``/``--help``). The rest are forwarded over SSH to
``xcodon_runtime.forwarded`` inside the VM, which runs the Linux CLI unchanged.
"""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from xcodon_runtime import __version__
from xcodon_runtime.errors import XcodonError

EXIT_ERROR = 125
MIN_LIMA = (2, 0, 0)
MIN_MACOS = 26
DEFAULT_NAME = "xcrunner"
# Relative to the VM user's home, where SSH starts a remote command.
VM_VENV = ".xcrunner-vm/venv"
VM_PYTHON = f"{VM_VENV}/bin/python"
VM_RUN_DIR = ".xcrunner-vm/run"  # forwarded.py's pid files, one per command
# After an interrupt, how long the VM side gets to stop its command before SSH is ended.
STOP_GRACE_SECONDS = 20.0
INSTALL_RECORD = "xcrunner-installed.json"
MAC_TEMP_FOLDERS = ("/private/var/folders", "/private/tmp")
LOCAL_COMMANDS = frozenset({"conda", "shim", "sandbox"})
# The settings a forwarded command takes along. The VM keeps its own runtime home
# and container folder.
PASSED_SETTINGS = ("XCRUNNER_ENV_DIR", "XCRUNNER_ENV_LAYER_DIR", "XCODON_ENGINE", "XCODON_LOG",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
LIMA_MISSING = "xcrunner on macOS needs Lima 2.0 or newer: brew install lima"
# Testing only: drive a real Lima QEMU VM from a Linux host, because GitHub's macOS
# runners cannot start VMs. The VM is x86_64 without Rosetta, and only the home
# folder and XCRUNNER_MACHINE_MOUNTS are shared.
LINUX_TEST_ENV = "XCRUNNER_MACHINE_LINUX_TEST"
SSH_FAILED = 255
_TAIL_LINES = 30


def is_macos() -> bool:
    return sys.platform == "darwin"


def linux_test_mode(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return sys.platform.startswith("linux") and env.get(LINUX_TEST_ENV) == "1"


def uses_machine() -> bool:
    return is_macos() or linux_test_mode()


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# -- settings and host checks ------------------------------------------------------


@dataclass(frozen=True)
class MachineSettings:
    """Read when the VM is created (spec 16.8). A change needs `machine rm` and a new start."""

    name: str = DEFAULT_NAME
    cpus: int = 4
    memory: str = "4GiB"
    disk: str = "100GiB"
    mounts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "MachineSettings":
        name = environ.get("XCRUNNER_MACHINE_NAME") or DEFAULT_NAME
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise XcodonError(f"XCRUNNER_MACHINE_NAME={name!r} must be letters, digits, '-' or '_'")
        cpus_text = environ.get("XCRUNNER_MACHINE_CPUS") or "4"
        try:
            cpus = int(cpus_text)
        except ValueError:
            cpus = 0
        if cpus < 1:
            raise XcodonError(f"XCRUNNER_MACHINE_CPUS={cpus_text!r} must be a whole number of CPUs")
        mounts = tuple(m for m in (environ.get("XCRUNNER_MACHINE_MOUNTS") or "").split(":") if m)
        for m in mounts:
            if not os.path.isabs(m):
                raise XcodonError(f"XCRUNNER_MACHINE_MOUNTS folder {m!r} must be an absolute path")
        return cls(name, cpus, environ.get("XCRUNNER_MACHINE_MEMORY") or "4GiB",
                   environ.get("XCRUNNER_MACHINE_DISK") or "100GiB", tuple(m.rstrip("/") or "/" for m in mounts))


def check_host() -> None:
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    if platform.machine() != "arm64" or major < MIN_MACOS:
        raise XcodonError(f"xcrunner on macOS supports Apple silicon with macOS {MIN_MACOS} or newer; "
                          f"this Mac is {platform.machine()} with macOS {version or 'unknown'}")


def find_limactl() -> str:
    path = shutil.which("limactl")
    if path is None:
        raise XcodonError(LIMA_MISSING)
    return path


def lima_version(limactl: str) -> str:
    r = subprocess.run([limactl, "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout + r.stderr)
    if r.returncode != 0 or m is None:
        raise XcodonError(f"{LIMA_MISSING}; `limactl --version` failed: {_tail(r.stdout + r.stderr, 5)}")
    if tuple(int(x) for x in m.groups()) < MIN_LIMA:
        raise XcodonError(f"{LIMA_MISSING}; found Lima {m.group(0)}")
    return m.group(0)


def install_source() -> str | None:
    """The checkout path when this xcrunner is an editable install of a source tree, else None (PyPI)."""
    pkg = Path(__file__).resolve().parent
    repo = pkg.parent.parent
    if pkg.parent.name == "src" and (repo / "pyproject.toml").is_file():
        return str(repo)
    return None


def _under(path: str, folder: str) -> bool:
    folder = folder.rstrip("/") or "/"
    return folder == "/" or path == folder or path.startswith(folder + "/")


def shared_folders(settings: MachineSettings, home: Path, linux_test: bool = False) -> list[str]:
    """The writable folders the VM shares from the Mac, at the same paths."""
    return [str(home), *(() if linux_test else MAC_TEMP_FOLDERS), *settings.mounts]


def lima_yaml(settings: MachineSettings, home: Path, checkout: str | None, linux_test: bool = False) -> str:
    """The Lima config for the xcrunner VM (spec 16.3). ``linux_test``: a QEMU VM on a Linux host."""
    q = json.dumps
    lines = [
        "# Written by xcrunner (spec 16.3). Changing it needs `xcrunner machine rm` and a new start.",
        f"minimumLimaVersion: {q('.'.join(str(x) for x in MIN_LIMA))}",
        f"base: {q('template:ubuntu-24.04')}",
        *(['vmType: "qemu"'] if linux_test else ['vmType: "vz"', 'arch: "aarch64"']),
        f"cpus: {settings.cpus}",
        f"memory: {q(settings.memory)}",
        f"disk: {q(settings.disk)}",
        f"mountType: {q('9p' if linux_test else 'virtiofs')}",
        "mounts:",
    ]
    shared = shared_folders(settings, home, linux_test)
    for folder in shared:
        lines += [f"- location: {q(folder)}", f"  mountPoint: {q(folder)}", "  writable: true"]
    if checkout is not None and not any(_under(checkout, f) for f in shared):
        lines += [f"- location: {q(checkout)}", f"  mountPoint: {q(checkout)}", "  writable: false"]
        if linux_test:
            # Lima caches read-only 9p mounts hard ("fscache"), so the VM would keep
            # running old code after edits to an editable checkout.
            lines += ["  9p:", '    cache: "mmap"']
    if not linux_test:
        lines += ["vmOpts:", "  vz:", "    rosetta:", "      enabled: true", "      binfmt: true"]
    lines += [
        "containerd:",
        "  system: false",
        "  user: false",
        "provision:",
        "- mode: system",
        "  script: |",
        *("    " + s for s in _SYSTEM_SCRIPT.splitlines()),
        "- mode: user",
        "  script: |",
        *("    " + s for s in _USER_SCRIPT.splitlines()),
    ]
    return "\n".join(lines) + "\n"


_SYSTEM_SCRIPT = """#!/bin/sh
set -eu
# The ns engine needs unprivileged user namespaces, which Ubuntu 24.04 restricts.
printf 'kernel.apparmor_restrict_unprivileged_userns=0\\n' > /etc/sysctl.d/60-xcrunner.conf
sysctl -q -p /etc/sysctl.d/60-xcrunner.conf || true
# macOS hands out temp paths as both /var/folders/... and /private/var/folders/...
[ -e /var/folders ] || ln -s /private/var/folders /var/folders
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q python3-venv
fi"""

_USER_SCRIPT = f"""#!/bin/sh
set -eu
[ -x "$HOME/{VM_PYTHON}" ] || python3 -m venv "$HOME/{VM_VENV}\""""


# -- the VM -----------------------------------------------------------------------------


@dataclass
class Machine:
    settings: MachineSettings
    limactl: str
    home: Path
    runtime_home: Path
    lima_ver: str = ""
    environ: Mapping[str, str] = field(default_factory=dict)
    linux_test: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Machine":
        from xcodon_runtime.home import RuntimeHome

        linux_test = linux_test_mode(environ)
        if not linux_test:
            check_host()
        limactl = find_limactl()
        version = lima_version(limactl)
        return cls(MachineSettings.from_env(environ), limactl, Path.home(), RuntimeHome().path, version, environ,
                   linux_test)

    def shared(self) -> list[str]:
        return shared_folders(self.settings, self.home, self.linux_test)

    @property
    def name(self) -> str:
        return self.settings.name

    @property
    def state_dir(self) -> Path:
        d = self.runtime_home / "machine"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _lima(self, args: list[str], what: str) -> subprocess.CompletedProcess:
        r = subprocess.run([self.limactl, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise XcodonError(self._lima_failure(what, r))
        return r

    def _lima_failure(self, what: str, r: subprocess.CompletedProcess) -> str:
        msg = f"`limactl {what}` failed with exit code {r.returncode}"
        output = _tail(r.stdout + r.stderr)
        if output:
            msg += f":\n{output}"
        log = self.dir() / "ha.stderr.log"
        try:
            log_tail = _tail(log.read_text(errors="replace"))
        except OSError:
            log_tail = ""
        if log_tail:
            msg += f"\n{log}:\n{log_tail}"
        if "rosetta" in (output + log_tail).lower():
            msg += "\nRosetta may be missing: run `softwareupdate --install-rosetta`"
        return msg

    def info(self) -> dict | None:
        r = self._lima(["list", "--json"], "list --json")
        text = r.stdout.strip()
        items: list = []
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                items = []
        else:
            for line in text.splitlines():
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
        for item in items:
            if isinstance(item, dict) and item.get("name") == self.name:
                return item
        return None

    def dir(self, info: dict | None = None) -> Path:
        if info and isinstance(info.get("dir"), str):
            return Path(info["dir"])
        lima_home = self.environ.get("LIMA_HOME") or str(self.home / ".lima")
        return Path(lima_home) / self.name

    def create(self) -> None:
        path = self.state_dir / "lima.yaml"
        path.write_text(lima_yaml(self.settings, self.home, install_source(), self.linux_test))
        self._lima(["create", "--tty=false", f"--name={self.name}", str(path)], "create")

    def start(self) -> None:
        self._lima(["start", "--tty=false", self.name], f"start {self.name}")

    def ssh_argv(self, remote: str, tty: bool = False) -> list[str]:
        return ["ssh", "-F", str(self.dir() / "ssh.config"),
                # In Lima's short instance folder: a socket path must stay under about
                # 104 bytes, which a long runtime home plus ssh's %C would exceed.
                "-o", "ControlMaster=auto", "-o", f"ControlPath={self.dir() / 'xcrunner-ssh.sock'}",
                "-o", "ControlPersist=10m", "-tt" if tty else "-T", f"lima-{self.name}", "--", remote]

    # install ---------------------------------------------------------------------

    def _record_path(self) -> Path:
        return self.dir() / INSTALL_RECORD

    def wanted_record(self) -> dict:
        return {"version": __version__, "source": install_source() or "pypi"}

    def installed_record(self) -> dict | None:
        try:
            data = json.loads(self._record_path().read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def install(self, err: TextIO) -> None:
        checkout = install_source()
        pip_args = ["-e", f"{checkout}[zstd]"] if checkout else [f"xcrunner[zstd]=={__version__}"]
        script = (f'set -e; [ -x {VM_PYTHON} ] || python3 -m venv {VM_VENV}; '
                  f'exec {VM_PYTHON} -m pip install -q --upgrade "$@"')
        print(f"xcrunner: installing xcrunner {__version__} in the VM", file=err)
        r = subprocess.run(self.ssh_argv(shlex.join(["sh", "-c", script, "sh", *pip_args])),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise XcodonError(f"could not install xcrunner in the VM (exit code {r.returncode}); "
                              f"a PyPI install needs network access in the VM:\n{_tail(r.stdout + r.stderr)}")
        self._record_path().write_text(json.dumps(self.wanted_record(), indent=2) + "\n")

    def vm_version(self) -> str | None:
        r = subprocess.run(self.ssh_argv(shlex.join(
            [VM_PYTHON, "-c", "import xcodon_runtime; print(xcodon_runtime.__version__)"])),
            capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def ensure_ready(self, err: TextIO, check_version: bool = False) -> dict:
        """Create, start and install as needed. Returns the instance info."""
        info = self.info()
        if info is None:
            print(f"xcrunner: creating the {self.name} VM; the first start takes a few minutes", file=err)
            self.create()
            info = self.info()
            if info is None:
                raise XcodonError(f"`limactl create` finished but no VM named {self.name} exists")
        if info.get("status") != "Running":
            print(f"xcrunner: starting the {self.name} VM", file=err)
            self.start()
            info = self.info() or info
        if self.installed_record() != self.wanted_record() or (
                check_version and self.vm_version() != __version__):
            self.install(err)
        return info

    def status(self) -> dict:
        info = self.info()
        record = self.installed_record() if info is not None else None
        return {
            "name": self.name,
            "status": info.get("status", "unknown") if info else "absent",
            "cpus": info.get("cpus") if info else None,
            "memory": info.get("memory") if info else None,
            "disk": info.get("disk") if info else None,
            "dir": str(self.dir(info)) if info else None,
            "shared_folders": self.shared(),
            "settings": {"cpus": self.settings.cpus, "memory": self.settings.memory,
                         "disk": self.settings.disk, "mounts": list(self.settings.mounts)},
            "lima": self.lima_ver,
            "mac_xcrunner": __version__,
            "vm_xcrunner": record.get("version") if record else None,
        }


# -- `xcrunner machine` ----------------------------------------------------------------

MACHINE_USAGE = "usage: xcrunner machine {start|stop|status|shell|rm [-f]}\n"


def machine_main(argv: Sequence[str], environ: Mapping[str, str], out: TextIO, err: TextIO,
                 stdin: TextIO | None = None) -> int:
    if not uses_machine():
        raise XcodonError("machine commands are for macOS")
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(MACHINE_USAGE, end="", file=out if argv else err)
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    m = Machine.from_env(environ)
    if verb == "start" and not rest:
        m.ensure_ready(err, check_version=True)
        print(f"xcrunner: the {m.name} VM is running", file=err)
        return 0
    if verb == "stop" and not rest:
        if m.info() is not None:
            m._lima(["stop", m.name], f"stop {m.name}")
        return 0
    if verb == "status" and not rest:
        print(json.dumps(m.status(), indent=2), file=out)
        return 0
    if verb == "shell" and not rest:
        m.ensure_ready(err)
        return subprocess.run([m.limactl, "shell", m.name]).returncode
    if verb == "rm" and rest in ([], ["-f"], ["--force"]):
        if m.info() is None:
            return 0
        if not rest:
            stdin = sys.stdin if stdin is None else stdin
            if not stdin.isatty():
                raise XcodonError("`xcrunner machine rm` deletes the VM with all its images and env layers; "
                                  "pass -f to do it without a prompt")
            print(f"Delete the {m.name} VM with all its images and env layers? [y/N] ", end="", file=err, flush=True)
            if stdin.readline().strip().lower() not in ("y", "yes"):
                return 1
        m._lima(["delete", "--force", m.name], f"delete {m.name}")
        return 0
    print(MACHINE_USAGE, end="", file=err)
    return 2


# -- forwarding ------------------------------------------------------------------------

_GLOBALS_WITH_VALUE = ("--engine", "--home")


def split_globals(argv: Sequence[str]) -> tuple[list[str], str | None, list[str]]:
    """(global options, subcommand, the subcommand's arguments), as cli's own split reads them."""
    argv = list(argv)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBALS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return argv[:i], tok, argv[i + 1:]
    return argv, None, []


def mac_path(path: str) -> str:
    """Inside the VM, /tmp is the VM's own folder; the Mac's /tmp is shared as /private/tmp."""
    if linux_test_mode():
        return path  # a Linux host's /tmp is its own folder, and it is not shared
    if path == "/tmp" or path.startswith("/tmp/"):
        return "/private" + path
    return path


def forward_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: environ[k] for k in PASSED_SETTINGS if k in environ}


_DROP = object()


def _rewrite_env_item(value: str, environ: Mapping[str, str]):
    """Docker's bare `-e NAME` copies NAME from the caller: do it on the Mac, or drop an unset one."""
    if "=" in value:
        return value
    return f"{value}={environ[value]}" if value in environ else _DROP


def _rewrite_volume(value: str) -> str:
    src, sep, rest = value.partition(":")
    return mac_path(src) + sep + rest


def _rewrite_mount(value: str) -> str:
    fields = next(csv.reader(io.StringIO(value)), [])
    out = []
    for f in fields:
        k, sep, v = f.partition("=")
        if k.strip() in ("source", "src") and sep:
            f = f"{k}={mac_path(v.strip())}"
        out.append(f)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(out)
    return buf.getvalue()


def _walk_options(tokens: list[str], takes_value: Callable[[str], bool],
                  rewrite: Callable[[str, str], object], positional: Callable[[str], str] | None = None
                  ) -> list[str]:
    """Rewrite option values up to the first positional token; the rest passes unchanged.

    ``rewrite(flag, value)`` returns the new value, or ``_DROP`` to remove the option.
    ``positional`` rewrites the first positional token, when given.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or tok == "-":
            first = positional(tok) if positional else tok
            return out + [first] + tokens[i + 1:]
        flag, has_eq, inline = tok.partition("=")
        if not takes_value(flag):
            out.append(tok)
            i += 1
            continue
        if has_eq:
            value, i = inline, i + 1
        elif i + 1 < len(tokens):
            value, i = tokens[i + 1], i + 2
        else:
            return out + [tok]
        new = rewrite(flag, value)
        if new is _DROP:
            continue
        out += [f"{flag}={new}"] if has_eq else [flag, str(new)]
    return out


def _run_like(tokens: list[str], environ: Mapping[str, str]) -> list[str]:
    from xcodon_runtime.cli import IGNORED_FLAGS, RUN_FLAGS

    def takes(flag: str) -> bool:
        return bool(RUN_FLAGS.get(flag, IGNORED_FLAGS.get(flag, False)))

    def rewrite(flag: str, value: str):
        if flag in ("-e", "--env"):
            return _rewrite_env_item(value, environ)
        if flag in ("-v", "--volume"):
            return _rewrite_volume(value)
        if flag == "--mount":
            return _rewrite_mount(value)
        if flag in ("--env-dir", "--cidfile"):
            return mac_path(value)
        return value

    return _walk_options(tokens, takes, rewrite)


_EXEC_VALUE_FLAGS = {"-e", "--env", "-w", "--workdir", "-u", "--user", "--env-file", "--detach-keys"}


def _exec(tokens: list[str], environ: Mapping[str, str]) -> list[str]:
    def rewrite(flag: str, value: str):
        return _rewrite_env_item(value, environ) if flag in ("-e", "--env") else value

    return _walk_options(tokens, lambda f: f in _EXEC_VALUE_FLAGS, rewrite)


_BUILD_VALUE_FLAGS = {"-t", "--tag", "-f", "--file", "--build-arg", "--progress", "--platform", "--network",
                      "--label"}


def _build(tokens: list[str], environ: Mapping[str, str]) -> list[str]:
    return _walk_options(tokens, lambda f: f in _BUILD_VALUE_FLAGS,
                         lambda flag, v: mac_path(v) if flag in ("-f", "--file") else v, positional=mac_path)


def _env_dir_anywhere(tokens: list[str], environ: Mapping[str, str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--env-dir" and i + 1 < len(tokens):
            out += [tok, mac_path(tokens[i + 1])]
            i += 2
            continue
        if tok.startswith("--env-dir="):
            tok = "--env-dir=" + mac_path(tok.partition("=")[2])
        out.append(tok)
        i += 1
    return out


_HANDLERS = {"run": _run_like, "create": _run_like, "exec": _exec, "build": _build,
             "commit": _env_dir_anywhere, "env": _env_dir_anywhere}


def prepare_argv(argv: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """The argv to run in the VM: host paths under /tmp and bare `-e NAME` flags rewritten (spec 16.4)."""
    globals_, cmd, rest = split_globals(argv)
    if cmd is None:
        return list(argv)
    if cmd == "docker" and rest:
        handler = _HANDLERS.get(rest[0])
        return [*globals_, cmd, rest[0], *(handler(rest[1:], environ) if handler else rest[1:])]
    handler = _HANDLERS.get(cmd)
    return [*globals_, cmd, *(handler(rest, environ) if handler else rest)]


_TTY_FLAG = re.compile(r"^-[a-zA-Z]*t[a-zA-Z]*$")


def wants_tty(argv: Sequence[str]) -> bool:
    """True when a run, create or exec asks for a terminal (`-t`, `--tty`, `-it`) before its positional."""
    _, cmd, rest = split_globals(argv)
    if cmd == "docker" and rest:
        cmd, rest = rest[0], rest[1:]
    if cmd not in ("run", "create", "exec"):
        return False
    for tok in rest:
        if not tok.startswith("-"):
            return False
        if tok == "--tty" or _TTY_FLAG.match(tok):
            return True
    return False


def _check_cwd(m: Machine, cwd: str) -> None:
    checkout = install_source()
    folders = m.shared() + ([checkout] if checkout else [])
    if not any(_under(cwd, f) for f in folders):
        raise XcodonError(f"{cwd} is not shared with the xcrunner VM; it shares {', '.join(folders)}. "
                          "Add folders with XCRUNNER_MACHINE_MOUNTS, then run `xcrunner machine rm` and "
                          "`xcrunner machine start`")


def run_forwarded(m: Machine, argv: Sequence[str], environ: Mapping[str, str], err: TextIO,
                  capture: bool = False) -> tuple[int, bytes]:
    """Run ``argv`` in the VM. Returns (exit code, captured stdout when ``capture``)."""
    cwd = mac_path(os.getcwd())
    _check_cwd(m, cwd)
    m.ensure_ready(err)
    token = secrets.token_hex(8)
    words = [VM_PYTHON, "-m", "xcodon_runtime.forwarded", "--expect-version", __version__, "--cwd", cwd,
             "--token", token]
    for k, v in forward_env(environ).items():
        words += ["--env", f"{k}={v}"]
    words += ["--", *prepare_argv(argv, environ)]
    tty = wants_tty(argv) and sys.stdin.isatty()
    proc = subprocess.Popen(m.ssh_argv(shlex.join(words), tty), stdout=subprocess.PIPE if capture else None)
    state: dict = {}

    def stop(signum, _frame) -> None:
        """Signal the command in the VM, as a direct call on Linux would get the signal.

        Lima's shared SSH connection does not end the remote command when this
        side's SSH client goes, so a second SSH call signals it through its pid
        file. SSH itself is ended after a grace period, or on a second signal.
        """
        if "signal" in state:
            proc.terminate()
            return
        state["signal"] = signum
        kill = shlex.join(["sh", "-c", f'kill -{int(signum)} "$(cat {VM_RUN_DIR}/{token}.pid)" 2>/dev/null || true'])
        try:
            subprocess.Popen(m.ssh_argv(kill), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            proc.terminate()
            return
        timer = threading.Timer(STOP_GRACE_SECONDS, proc.terminate)
        timer.daemon = True
        timer.start()
        state["timer"] = timer

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, stop)
        except ValueError:
            pass  # not the main thread
    try:
        output, _ = proc.communicate()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if "timer" in state:
            state["timer"].cancel()
    code = proc.returncode
    if "signal" in state and (code == SSH_FAILED or code < 0):
        return 128 + int(state["signal"]), output or b""  # ended by our own interrupt, not a lost VM
    if code == SSH_FAILED:
        raise XcodonError("cannot reach the xcrunner VM; run `xcrunner machine status`")
    return (128 - code if code < 0 else code), output or b""


def _mac_info(m: Machine, globals_: list[str], environ: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    doc: dict = {"platform": "macos", "machine": m.status()}
    if doc["machine"]["status"] != "Running":
        # `info` reports; it does not create or start the VM.
        doc["vm"] = {"error": "the xcrunner VM is not running; start it with `xcrunner machine start`"}
        print(json.dumps(doc, indent=2), file=out)
        return 0
    try:
        code, output = run_forwarded(m, [*globals_, "info"], environ, err, capture=True)
        doc["vm"] = json.loads(output) if code == 0 else {"error": f"`xcrunner info` in the VM exited {code}"}
    except (XcodonError, ValueError) as e:
        doc["vm"] = {"error": str(e)}
    print(json.dumps(doc, indent=2), file=out)
    return 0


def mac_main(argv: Sequence[str], local_main: Callable[[list[str]], int],
             environ: Mapping[str, str] | None = None, out: TextIO | None = None,
             err: TextIO | None = None) -> int:
    """The macOS entry point: run on the Mac, or forward to the VM (spec 16.4)."""
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    argv = list(argv)
    globals_, cmd, rest = split_globals(argv)
    if cmd is None or cmd in LOCAL_COMMANDS:
        return local_main(argv)
    try:
        if cmd == "machine":
            return machine_main(rest, environ, out, err)
        if any(g == "--home" or g.startswith("--home=") for g in globals_):
            raise XcodonError("--home is not available on macOS for commands that run in the xcrunner VM; "
                              "the VM keeps its own runtime home")
        m = Machine.from_env(environ)
        if cmd == "info":
            return _mac_info(m, globals_, environ, out, err)
        code, _ = run_forwarded(m, argv, environ, err)
        return code
    except XcodonError as e:
        print(f"xcrunner: {e}", file=err)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
