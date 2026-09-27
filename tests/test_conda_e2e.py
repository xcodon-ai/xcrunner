"""The agent's conda calls, through the shim, with the real pinned micromamba and bioconda."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.network


def _run(args, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)


def test_conda_shim_end_to_end(tmp_path):
    home = tmp_path / "rt-home"
    shim_dir = tmp_path / "shim"
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    project = tmp_path / "project"
    (project / ".xcrunner-env").mkdir(parents=True)
    (project / ".xcrunner-env").chmod(0o755)
    xcrunner = Path(sys.executable).with_name("xcrunner")
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(user_home), "XCODON_RUNTIME_HOME": str(home)}

    r = _run([str(xcrunner), "shim", "install", "conda", "--dir", str(shim_dir)], tmp_path, {**env, "PATH": str(empty)})
    assert r.returncode == 0, r.stdout + r.stderr

    r = _run(["conda", "create", "-n", "bwa_env", "-c", "bioconda", "seqtk"], project, env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    env_prefix = project / ".xcrunner-env" / "conda" / "envs" / "bwa_env"
    assert (env_prefix / "bin" / "seqtk").exists()
    assert "seqtk" in (env_prefix / "conda-explicit.txt").read_text()

    r = _run(["conda", "run", "-n", "bwa_env", "seqtk"], project, env)
    assert "Usage" in r.stdout + r.stderr
    assert _run(["conda", "run", "-n", "bwa_env", "sh", "-c", "exit 3"], project, env).returncode == 3

    r = _run(["conda", "create", "-p", "workspace/conda_env", "-c", "bioconda", "seqtk"], project, env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    record = (project / "workspace" / "conda_env" / "conda-explicit.txt").read_text()
    assert "@EXPLICIT" in record and "seqtk" in record
    assert _run([str(project / "workspace" / "conda_env" / "bin" / "seqtk")], project, env).returncode == 1

    r = _run(["conda", "env", "list"], project, env)
    assert r.returncode == 0 and "bwa_env" in r.stdout and "conda_env" in r.stdout

    assert _run(["mamba", "--version"], project, env).stdout.startswith("conda 2.9.0")
    r = _run(["conda", "activate", "bwa_env"], project, env)
    assert r.returncode == 1 and "conda run" in r.stderr

    assert not (user_home / ".conda").exists() and not (user_home / ".cache").exists()
    assert (home / "conda-pkgs").is_dir()
