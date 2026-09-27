"""The VM side of a forwarded macOS command. See spec section 16.4."""

import io
import os
import subprocess
import sys
import textwrap
import time
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from xcodon_runtime import __version__, forwarded
from xcodon_runtime.envdir import ENV_LAYER_DIR_ENV


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_run(cmd, env, **kw):
        calls.append({"cmd": cmd, "env": env, "cwd": os.getcwd()})
        return 0

    monkeypatch.setattr(forwarded, "_run", fake_run)
    return calls


def _main(argv):
    err = io.StringIO()
    with redirect_stderr(err):
        code = forwarded.main(argv)
    return code, err.getvalue()


def test_runs_the_linux_cli_in_the_working_folder(captured, tmp_path, monkeypatch):
    monkeypatch.chdir("/")
    monkeypatch.delenv(ENV_LAYER_DIR_ENV, raising=False)
    code, _ = _main(["--expect-version", __version__, "--cwd", str(tmp_path), "--env", "XCODON_LOG=debug",
                     "--env", "A=b=c", "--", "run", "--rm", "alpine", "echo", "--env"])
    assert code == 0
    (call,) = captured
    assert call["cmd"] == [sys.executable, "-m", "xcodon_runtime.cli", "run", "--rm", "alpine", "echo", "--env"]
    assert call["cwd"] == str(tmp_path)
    assert call["env"]["XCODON_LOG"] == "debug" and call["env"]["A"] == "b=c"
    assert call["env"][ENV_LAYER_DIR_ENV] == str(Path.home() / ".xrunner-vm" / "env-layers")


def test_a_passed_layer_dir_wins(captured, tmp_path):
    _main(["--cwd", str(tmp_path), "--env", f"{ENV_LAYER_DIR_ENV}=/x/layers", "--", "ps"])
    assert captured[0]["env"][ENV_LAYER_DIR_ENV] == "/x/layers"


def test_a_version_mismatch_fails_before_running(captured, tmp_path):
    code, err = _main(["--expect-version", "0.0.1", "--cwd", str(tmp_path), "--", "ps"])
    assert code == forwarded.EXIT_ERROR == 125
    assert f"the VM has xrunner {__version__} and the Mac has 0.0.1" in err
    assert "xrunner machine start" in err and not captured


def test_a_folder_that_is_not_shared_fails(captured, tmp_path):
    code, err = _main(["--cwd", str(tmp_path / "missing"), "--", "ps"])
    assert code == 125 and "is not a folder in the xrunner VM" in err and not captured


@pytest.mark.parametrize("argv", [["--cwd"], ["--env", "NOEQUALS", "--", "ps"], ["ps"], ["--bogus", "--", "ps"]])
def test_bad_arguments_fail(captured, argv):
    code, err = _main(argv)
    assert code == 125 and err.startswith("xrunner: ") and not captured


def test_exit_codes_and_signal_deaths_pass_through():
    assert forwarded._run([sys.executable, "-c", "import sys; sys.exit(7)"], dict(os.environ)) == 7
    kill_self = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert forwarded._run([sys.executable, "-c", kill_self], dict(os.environ)) == 128 + 9


def test_the_command_stops_when_the_mac_side_goes_away(tmp_path):
    marker = tmp_path / "marker"
    sleeper = textwrap.dedent("""
        import signal, sys, time
        def on_term(*a):
            open(sys.argv[1], "w").write("term")
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
        open(sys.argv[1] + ".ready", "w").write("ready")
        time.sleep(60)
    """)
    wrapper = ("import os, sys; from xcodon_runtime.forwarded import _run; "
               f"sys.exit(_run([sys.executable, '-c', {sleeper!r}, {str(marker)!r}], dict(os.environ), poll=0.1))")
    parent = textwrap.dedent(f"""
        import os, subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {wrapper!r}])
        deadline = time.time() + 10
        while not os.path.exists({str(marker) + ".ready"!r}) and time.time() < deadline:
            time.sleep(0.05)
        os._exit(0)
    """)
    subprocess.run([sys.executable, "-c", parent], check=True, timeout=30)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert marker.read_text() == "term"
