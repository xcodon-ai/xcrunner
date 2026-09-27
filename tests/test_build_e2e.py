"""The agent's real recipe: FROM the stock coala-runtime image, RUN pip install, reuse by tag via the docker shim."""

import os
import subprocess

import pytest

from xcodon_runtime import cli
from xcodon_runtime.api import Runtime

BASE = "coala-runtime-python:latest"


def _daemon_has_base() -> bool:
    try:
        return subprocess.run(["/usr/bin/docker", "image", "inspect", BASE], capture_output=True).returncode == 0
    except OSError:
        return False


# Needs the stock image from the local daemon, and pip needs PyPI, so both markers apply.
# A daemon without that image (a CI runner, for example) cannot run these tests either:
# the name exists only where coala-runtime built it, never on a registry.
pytestmark = [
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.skipif(not _daemon_has_base(), reason=f"the local docker daemon has no {BASE}"),
]


def test_agent_recipe_builds_and_runs_without_docker_commands(home, engine_name, tmp_path, monkeypatch):
    tag = "xcodon/e2e-python-deps:latest"

    # No stale tag on the real docker daemon before we start, so a leftover from a
    # previous run (or a bug that shells out to real docker) cannot make this pass.
    pre = subprocess.run(["/usr/bin/docker", "image", "inspect", tag], capture_output=True)
    assert pre.returncode != 0

    rt = Runtime(home.path, engine=engine_name)
    rt.pull("coala-runtime-python:latest")
    ctx = tmp_path / "built-python-deps"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM coala-runtime-python:latest\nRUN pip install --no-cache-dir tabulate\n")

    shim_dir = tmp_path / "shimbin"
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()
    # An empty PATH during install: this host has a real docker at /usr/bin/docker,
    # and shim install refuses to run alongside a real docker on PATH without --force.
    monkeypatch.setenv("PATH", str(empty_path_dir))
    assert cli.main(["shim", "install", "--dir", str(shim_dir)]) == 0

    # The shim comes first on PATH, so `docker` resolves to it. xcrunner's own daemon
    # source skips shims when resolving the real docker for the Dockerfile's FROM image,
    # so it still finds /usr/bin/docker for that lookup.
    env = {**os.environ, "PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path),
           "XCODON_ENGINE": engine_name}

    r = subprocess.run(["docker", "build", "-t", tag, str(ctx)], env=env, capture_output=True, text=True,
                        timeout=1200)
    build_text = r.stdout[-2000:] + r.stderr[-2000:]
    assert r.returncode == 0, build_text

    r = subprocess.run(["docker", "image", "inspect", tag], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

    out = tmp_path / "out"
    with open(out, "wb") as f:
        run_rc = rt.run(tag, command=["python", "-c", "import tabulate; print(tabulate.__version__)"],
                         rm=True, stdout=f)
    with open(out, "rb") as f:
        run_text = f.read()
    assert run_rc == 0, build_text + run_text.decode(errors="replace")[-2000:]
    assert run_text.strip(), "python printed no tabulate version:\n" + build_text

    # The real docker daemon never received the tag: the whole build ran inside xcrunner.
    post = subprocess.run(["/usr/bin/docker", "image", "inspect", tag], capture_output=True)
    assert post.returncode != 0


def test_agent_recipe_builds_on_a_host_without_docker(home, engine_name, tmp_path, monkeypatch):
    """No docker at all during the build: the base image was seeded into the store beforehand.

    PATH is the shim directory only. /bin is a link to /usr/bin on some hosts,
    so adding /bin would expose the real docker. The shim starts with
    ``#!/bin/sh`` and runs xcrunner by its absolute path, so it needs no PATH.
    """
    tag = "xcodon/e2e-nodocker-python-deps:latest"
    pre = subprocess.run(["/usr/bin/docker", "image", "inspect", tag], capture_output=True)
    assert pre.returncode != 0

    rt = Runtime(home.path, engine=engine_name)
    rt.pull("coala-runtime-python:latest")  # seeding, in-process, while docker is still reachable
    ctx = tmp_path / "built-python-deps"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM coala-runtime-python:latest\nRUN pip install --no-cache-dir tabulate\n")

    shim_dir = tmp_path / "shimbin"
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_path_dir))
    assert cli.main(["shim", "install", "--dir", str(shim_dir)]) == 0

    env = {**os.environ, "PATH": str(shim_dir), "XCODON_RUNTIME_HOME": str(home.path),
           "XCODON_ENGINE": engine_name}
    docker = str(shim_dir / "docker")
    r = subprocess.run([docker, "build", "-t", tag, str(ctx)], env=env, capture_output=True, text=True,
                        timeout=1200)
    build_text = r.stdout[-2000:] + r.stderr[-2000:]
    assert r.returncode == 0, build_text
    assert "changed in the local docker daemon" not in build_text

    r = subprocess.run([docker, "image", "inspect", tag], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

    out = tmp_path / "out"
    with open(out, "wb") as f:
        run_rc = rt.run(tag, command=["python", "-c", "import tabulate; print(tabulate.__version__)"],
                         rm=True, stdout=f, pull="never")
    run_text = out.read_bytes()
    assert run_rc == 0, build_text + run_text.decode(errors="replace")[-2000:]
    assert run_text.strip(), "python printed no tabulate version:\n" + build_text

    post = subprocess.run(["/usr/bin/docker", "image", "inspect", tag], capture_output=True)
    assert post.returncode != 0
