"""Forwarding macOS commands to the VM, run on Linux against a fake Lima. See spec section 16.4."""

import io
import json
import os
import signal
import subprocess

import pytest

from tests.fake_lima import log
from xcodon_runtime import __version__, macvm
from xcodon_runtime.macvm import forward_env, mac_main, mac_path, prepare_argv, split_globals, wants_tty


def test_split_globals():
    assert split_globals(["-v", "--engine", "ns", "run", "--rm", "img"]) == (["-v", "--engine", "ns"], "run",
                                                                              ["--rm", "img"])
    assert split_globals(["--home=/h", "ps"]) == (["--home=/h"], "ps", [])
    assert split_globals(["--version"]) == (["--version"], None, [])


@pytest.mark.parametrize("path,want", [("/tmp", "/private/tmp"), ("/tmp/a/b", "/private/tmp/a/b"),
                                       ("/tmpfile", "/tmpfile"), ("/Users/me/tmp", "/Users/me/tmp"),
                                       ("rel/tmp", "rel/tmp")])
def test_mac_path(path, want):
    assert mac_path(path) == want


ENV = {"FOO": "bar"}


def test_run_rewrites_options_but_not_the_command():
    argv = ["run", "--rm", "-e", "FOO", "-e", "UNSET", "-e", "A=1", "--env=FOO", "-v", "/tmp/x:/data:ro",
            "--mount", "type=bind,source=/tmp/m,target=/m", "--env-dir", "/tmp/env", "--cidfile=/tmp/cid",
            "-w", "/tmp", "alpine", "sh", "-c", "-e X", "-v", "/tmp/y:/y"]
    assert prepare_argv(argv, ENV) == [
        "run", "--rm", "-e", "FOO=bar", "-e", "A=1", "--env=FOO=bar", "-v", "/private/tmp/x:/data:ro",
        "--mount", "type=bind,source=/private/tmp/m,target=/m", "--env-dir", "/private/tmp/env",
        "--cidfile=/private/tmp/cid", "-w", "/tmp", "alpine", "sh", "-c", "-e X", "-v", "/tmp/y:/y"]


def test_docker_forms_and_globals():
    assert prepare_argv(["-v", "--engine", "ns", "docker", "run", "-e", "FOO", "img", "-e", "FOO"], ENV) == \
        ["-v", "--engine", "ns", "docker", "run", "-e", "FOO=bar", "img", "-e", "FOO"]
    assert prepare_argv(["docker", "exec", "-it", "-e", "FOO", "c1", "env", "-e", "FOO"], ENV) == \
        ["docker", "exec", "-it", "-e", "FOO=bar", "c1", "env", "-e", "FOO"]
    assert prepare_argv(["docker", "image", "inspect", "/tmp/x"], ENV) == ["docker", "image", "inspect", "/tmp/x"]


def test_exec_build_commit_env():
    assert prepare_argv(["exec", "-e", "UNSET", "-w", "/w", "c1", "ls"], ENV) == ["exec", "-w", "/w", "c1", "ls"]
    assert prepare_argv(["build", "-t", "x:1", "-f", "/tmp/Dockerfile", "/tmp/ctx"], ENV) == \
        ["build", "-t", "x:1", "-f", "/private/tmp/Dockerfile", "/private/tmp/ctx"]
    assert prepare_argv(["build", "--tag=x", "--file=/tmp/D", "."], ENV) == ["build", "--tag=x", "--file=/private/tmp/D", "."]
    assert prepare_argv(["commit", "--env-dir", "/tmp/e", "--image", "img", "tag"], ENV) == \
        ["commit", "--env-dir", "/private/tmp/e", "--image", "img", "tag"]
    assert prepare_argv(["env", "record", "--env-dir=/tmp/e"], ENV) == ["env", "record", "--env-dir=/private/tmp/e"]


def test_other_commands_pass_unchanged():
    for argv in (["pull", "alpine"], ["ps", "-a"], ["rm", "-f", "c1"], ["images"], ["--version"]):
        assert prepare_argv(argv, ENV) == argv


def test_wants_tty():
    assert wants_tty(["run", "-it", "img", "sh"])
    assert wants_tty(["docker", "exec", "-t", "c", "sh"])
    assert wants_tty(["create", "--tty", "img"])
    assert not wants_tty(["run", "-i", "img", "sh", "-t"])
    assert not wants_tty(["build", "-t", "tag", "."])
    assert not wants_tty(["ps"])


def test_forward_env_keeps_only_the_allowlist():
    environ = {"XRUNNER_ENV_DIR": "/p/.xrunner-env", "XCODON_LOG": "debug", "https_proxy": "http://p:3128",
               "XCODON_RUNTIME_HOME": "/mac/home", "XRUNNER_CONTAINER_DIR": "/c", "PATH": "/bin",
               "SECRET_TOKEN": "x"}
    assert forward_env(environ) == {"XRUNNER_ENV_DIR": "/p/.xrunner-env", "XCODON_LOG": "debug",
                                    "https_proxy": "http://p:3128"}


# -- routing ---------------------------------------------------------------------------------


@pytest.fixture
def routed(monkeypatch):
    calls = {"local": [], "forwarded": [], "machine": []}
    monkeypatch.setattr(macvm, "run_forwarded", lambda m, argv, env, err, capture=False:
                        calls["forwarded"].append(list(argv)) or (0, b""))
    monkeypatch.setattr(macvm, "machine_main", lambda argv, env, out, err: calls["machine"].append(argv) or 0)
    monkeypatch.setattr(macvm.Machine, "from_env", classmethod(lambda cls, env: object()))
    return calls


def _local(calls):
    return lambda argv: calls["local"].append(argv) or 0


@pytest.mark.parametrize("argv", [["conda", "install", "x"], ["shim", "install", "conda"],
                                  ["sandbox", "activate", "/p/.xrunner-env"], ["--version"], [], ["-h"]])
def test_local_commands(routed, argv):
    assert mac_main(argv, _local(routed), environ={}) == 0
    assert routed["local"] == [argv] and not routed["forwarded"]


@pytest.mark.parametrize("argv", [["pull", "alpine"], ["run", "--rm", "img"], ["docker", "build", "."],
                                  ["-v", "ps"], ["env", "show"], ["build", "."], ["commit", "c", "t"]])
def test_forwarded_commands(routed, argv):
    assert mac_main(argv, _local(routed), environ={}) == 0
    assert routed["forwarded"] == [argv] and not routed["local"]


def test_machine_goes_to_machine_main(routed):
    assert mac_main(["machine", "status"], _local(routed), environ={}) == 0
    assert routed["machine"] == [["status"]]


def test_home_is_refused_for_forwarded_commands(routed):
    err = io.StringIO()
    assert mac_main(["--home", "/h", "ps"], _local(routed), environ={}, err=err) == 125
    assert "--home is not available on macOS" in err.getvalue() and not routed["forwarded"]
    assert mac_main(["--home", "/h", "conda", "list"], _local(routed), environ={}) == 0


def test_cli_main_hands_off_on_macos(monkeypatch):
    from xcodon_runtime import cli

    seen = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(macvm, "mac_main", lambda argv, local: seen.append((argv, local)) or 7)
    assert cli.main(["ps"]) == 7
    assert seen == [(["ps"], cli._main_local)]


# -- end to end through the fake ssh ----------------------------------------------------------


def _mac_main(argv, environ=None):
    out, err = io.StringIO(), io.StringIO()
    code = mac_main(argv, lambda a: 99, environ=dict(os.environ, **(environ or {})), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_a_forwarded_run_streams_output_and_exit_code(mac, busybox_image, engine_name, capfd):
    code, _, err = _mac_main(["--engine", engine_name, "run", "--rm", "-e", "FOO", "xcodon-test/busybox",
                              "/bin/sh", "-c", "echo got $FOO; exit 3"], {"FOO": "from-mac"})
    assert code == 3, err
    assert "got from-mac" in capfd.readouterr().out
    (call,) = [a for a in log(mac["state"], "ssh") if "xcodon_runtime.forwarded" in a[-1]]
    assert call[:2] == ["-F", str(mac["lima_home"] / "xrunner" / "ssh.config")]
    assert "-T" in call and "lima-xrunner" in call
    assert f"--expect-version {__version__}" in call[-1] and f"--cwd {mac['mac_home']}" in call[-1]


def test_images_and_info_through_the_vm(mac):
    code, _, err = _mac_main(["images"])
    assert code == 0, err
    code, out, err = _mac_main(["info"])
    assert code == 0, err
    doc = json.loads(out)
    assert doc["platform"] == "macos" and doc["machine"]["status"] == "Running"
    assert doc["vm"]["version"] == __version__ and "engine" in doc["vm"]


def test_an_unshared_folder_fails_before_the_vm_starts(mac, monkeypatch):
    monkeypatch.chdir("/")  # pytest's tmp_path is under /tmp, which maps to the shared /private/tmp
    code, _, err = _mac_main(["ps"])
    assert code == 125 and "/ is not shared with the xrunner VM" in err
    assert log(mac["state"], "ssh") == [] and "create" not in [a[0] for a in log(mac["state"], "limactl")]


def test_ssh_failure_is_reported(mac, monkeypatch):
    macvm.Machine.from_env(dict(os.environ)).ensure_ready(io.StringIO())
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    code, _, err = _mac_main(["ps"])
    assert code == 125 and "cannot reach the xrunner VM" in err


def test_sigterm_on_the_mac_ends_ssh(mac, monkeypatch):
    m = macvm.Machine.from_env(dict(os.environ))
    m.ensure_ready(io.StringIO())
    monkeypatch.setattr(m, "ensure_ready", lambda err: {})
    terminated = []

    class FakeProc:
        returncode = 143

        def __init__(self, argv, stdout=None):
            pass

        def communicate(self):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return None, None

        def terminate(self):
            terminated.append(True)

    monkeypatch.setattr(macvm.subprocess, "Popen", FakeProc)
    before = signal.getsignal(signal.SIGTERM)
    code, _ = macvm.run_forwarded(m, ["ps"], dict(os.environ), io.StringIO())
    assert terminated == [True] and code == 143
    assert signal.getsignal(signal.SIGTERM) is before


def test_subprocess_is_the_real_one_after_the_fake():
    assert macvm.subprocess.Popen is subprocess.Popen
