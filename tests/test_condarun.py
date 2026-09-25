import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from xcodon_runtime.condarun import RunUsageError, parse_run_args


def _xr(args, cwd, env, stdin=None):
    return subprocess.run([sys.executable, "-m", "xcodon_runtime.cli", *args], cwd=cwd, env=env,
                          input=stdin, capture_output=True, text=True, timeout=60)


def make_env(prefix: Path, tools: dict[str, str]) -> Path:
    (prefix / "conda-meta").mkdir(parents=True, exist_ok=True)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        tool = prefix / "bin" / name
        tool.write_text("#!/bin/sh\n" + body + "\n")
        tool.chmod(0o755)
    return prefix


TOOLS = {
    "echoargs": 'printf "%s|" "$@"; echo; echo "HOME=$HOME PREFIX=$CONDA_PREFIX ENV=$CONDA_DEFAULT_ENV LVL=$CONDA_SHLVL"',
    "rc": "exit 7",
    "catin": "cat",
}


@pytest.fixture
def project(tmp_path, home):
    proj = tmp_path / "proj"
    (proj / "workspace").mkdir(parents=True)
    make_env(proj / ".xrunner-env" / "conda" / "envs" / "tools", TOOLS)
    return proj


@pytest.fixture
def env(home):
    return {"PATH": "/usr/bin:/bin", "HOME": "/home/tester", "XCODON_RUNTIME_HOME": str(home.path)}


def test_parse_run_args():
    a = parse_run_args(["--no-capture-output", "-n", "tools", "--live-stream", "--cwd=w", "bwa", "-n", "x"])
    assert (a.name, a.cwd, a.command) == ("tools", "w", ["bwa", "-n", "x"])
    assert parse_run_args(["-p", "p", "--", "-weird"]).command == ["-weird"]
    with pytest.raises(RunUsageError):
        parse_run_args(["--bogus", "x"])
    with pytest.raises(RunUsageError):
        parse_run_args(["-n"])


def test_parse_run_args_attached_short_option():
    a = parse_run_args(["-ntools", "echoargs"])
    assert (a.name, a.command) == ("tools", ["echoargs"])


def test_parse_run_args_rejects_name_and_prefix_together():
    with pytest.raises(RunUsageError, match="not both"):
        parse_run_args(["-n", "x", "-p", "y", "tool"])


def test_parse_run_args_rejects_invalid_env_name():
    for bad in ("a/b", ".", ".."):
        with pytest.raises(RunUsageError, match="invalid environment name"):
            parse_run_args(["-n", bad, "tool"])


def test_parse_run_args_help_flag():
    assert parse_run_args(["--help"]).help is True
    assert parse_run_args(["-h", "-n", "x"]).help is True
    assert parse_run_args(["tool", "-h"]).help is False  # `-h` after the command is the command's own


def test_arguments_and_activation_variables(project, env):
    r = _xr(["conda", "run", "-n", "tools", "echoargs", "a b", "$HOME", ">x"], project, env)
    assert r.returncode == 0, r.stderr
    prefix = project / ".xrunner-env" / "conda" / "envs" / "tools"
    assert r.stdout.splitlines() == ["a b|$HOME|>x|", f"HOME=/home/tester PREFIX={prefix} ENV=tools LVL=1"]
    assert not (project / "x").exists()


def test_exit_code_stdin_and_path(project, env):
    assert _xr(["conda", "run", "-n", "tools", "rc"], project, env).returncode == 7
    assert _xr(["conda", "run", "-n", "tools", "catin"], project, env, stdin="hello\n").stdout == "hello\n"
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "command -v echoargs"], project, env)
    assert r.stdout.strip() == str(project / ".xrunner-env" / "conda" / "envs" / "tools" / "bin" / "echoargs")


def test_activate_d_scripts_are_sourced(project, env):
    d = project / ".xrunner-env" / "conda" / "envs" / "tools" / "etc" / "conda" / "activate.d"
    d.mkdir(parents=True)
    (d / "java.sh").write_text("export FROM_ACTIVATE=yes\n")
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "echo $FROM_ACTIVATE"], project, env)
    assert r.stdout == "yes\n"
    r = _xr(["conda", "run", "-n", "tools", "echoargs", "a b", "|"], project, env)
    assert r.stdout.splitlines()[0] == "a b|||"
    assert _xr(["conda", "run", "-n", "tools", "rc"], project, env).returncode == 7


def test_missing_env_and_missing_command(project, env):
    r = _xr(["conda", "run", "-n", "nope", "true"], project, env)
    assert r.returncode == 1 and "EnvironmentLocationNotFound: Not a conda environment:" in r.stderr
    r = _xr(["conda", "run", "-n", "tools", "no-such-tool"], project, env)
    assert r.returncode == 127 and "no-such-tool" in r.stderr


def test_prefix_env_cwd_and_ignored_flags(project, env):
    make_env(project / "workspace" / "penv", {"hello": "echo hi from $CONDA_DEFAULT_ENV"})
    r = _xr(["conda", "run", "-p", "workspace/penv", "hello"], project, env)
    assert r.stdout == f"hi from {project / 'workspace' / 'penv'}\n"
    r = _xr(["conda", "run", "-n", "tools", "--cwd", "workspace", "sh", "-c", "pwd"], project, env)
    assert r.stdout.strip() == str(project / "workspace")
    r = _xr(["conda", "run", "--no-capture-output", "--live-stream", "-n", "tools", "echoargs", "z"], project, env)
    assert r.stdout.splitlines()[0] == "z|"
    r = _xr(["conda", "run", "-n", "tools", "--", "echoargs", "-n"], project, env)
    assert r.stdout.splitlines()[0] == "-n|"


def test_usage_errors_exit_2(project, env):
    assert _xr(["conda", "run", "-n", "tools"], project, env).returncode == 2
    assert _xr(["conda", "run", "--bogus", "x"], project, env).returncode == 2


def test_env_in_the_home_root_is_found(project, env, home):
    make_env(home.path / "conda" / "envs" / "old", {"hello": "echo old"})
    assert _xr(["conda", "run", "-n", "old", "hello"], project, env).stdout == "old\n"


def test_base_env_is_the_root(project, env):
    make_env(project / ".xrunner-env" / "conda", {"basetool": "echo base"})
    assert _xr(["conda", "run", "basetool"], project, env).stdout == "base\n"


def test_run_ensures_a_fresh_root_is_a_valid_base_env(project, env):
    """A fresh project has `.xrunner-env` but no conda-meta at the root itself (only
    the `tools` env the fixture builds has it). `conda run` with no -n/-p targets the
    root as the base env, which real conda's own root always is; xrunner must make
    that true here too instead of reporting a missing environment."""
    root = project / ".xrunner-env" / "conda"
    assert not (root / "conda-meta").is_dir()
    r = _xr(["conda", "run", "sh", "-c", "echo hi"], project, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "hi\n"
    assert (root / "conda-meta").is_dir()


def test_name_and_prefix_together_exit_2(project, env):
    r = _xr(["conda", "run", "-n", "tools", "-p", "workspace", "echoargs"], project, env)
    assert r.returncode == 2 and "not both" in r.stderr


def test_invalid_env_name_exit_2(project, env):
    r = _xr(["conda", "run", "-n", "a/b", "echoargs"], project, env)
    assert r.returncode == 2 and "invalid environment name" in r.stderr
    r = _xr(["conda", "run", "-n", "..", "echoargs"], project, env)
    assert r.returncode == 2 and "invalid environment name" in r.stderr


def test_help_flag_prints_usage_and_exits_0(project, env):
    for flag in ("--help", "-h"):
        r = _xr(["conda", "run", flag], project, env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("usage: conda run"), r.stdout
        assert r.stderr == ""


def test_dispatch_accepts_options_before_the_run_verb(project, env, tmp_path):
    root = tmp_path / "altroot"
    make_env(root / "envs" / "x", {"tool": "echo from-x"})
    r = _xr(["conda", "-r", str(root), "run", "-n", "x", "tool"], project, env)
    assert r.stdout == "from-x\n", r.stderr
    r = _xr(["conda", "-q", "run", "-n", "tools", "echoargs", "z"], project, env)
    assert r.stdout.splitlines()[0] == "z|", r.stderr


def test_json_option_before_run_is_ignored(project, env):
    """`conda --json run ...` (an option before the verb, spec 12.5) used to fail with
    `unknown option --json`, because condarun only ignored -q/--quiet and friends."""
    r = _xr(["conda", "--json", "run", "-n", "tools", "echoargs", "z"], project, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "z|"


def test_missing_path_falls_back_to_os_defpath(project, home):
    env_no_path = {"HOME": "/home/tester", "XCODON_RUNTIME_HOME": str(home.path)}
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "command -v ls"], project, env_no_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("/ls")


def test_sigpipe_and_sigxfsz_are_not_inherited_ignored(project, env):
    r = _xr(["conda", "run", "-n", "tools", "sh", "-c", "grep SigIgn /proc/self/status"], project, env)
    assert r.returncode == 0, r.stderr
    sigign = int(r.stdout.split()[1], 16)
    assert not (sigign & 0x1000), r.stdout  # bit 13: SIGPIPE


def test_broken_pipe_dies_quietly_instead_of_printing_an_error(project, env):
    cmd = f"{shlex.quote(sys.executable)} -m xcodon_runtime.cli conda run -n tools yes | head -1"
    r = subprocess.run(cmd, shell=True, cwd=project, env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout == "y\n"
    assert r.returncode == 0
    assert "Broken pipe" not in r.stderr
