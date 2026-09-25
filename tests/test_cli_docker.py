import json
import os
import subprocess

import pytest

from xcodon_runtime import cli


def test_translate_docker_argv():
    t = cli.translate_docker_argv
    assert t(["build", "-t", "x/y:1", "ctx"]) == ["build", "-t", "x/y:1", "ctx"]
    assert t(["image", "inspect", "x/y:1"]) == ["inspect", "x/y:1"]
    assert t(["image", "inspect", "--format", "{{.Id}}", "x"]) == ["inspect", "x"]
    assert t(["images"]) == ["images"] and t(["image", "ls"]) == ["images"]
    assert t(["image", "rm", "x"]) == ["rmi", "x"] and t(["rmi", "x"]) == ["rmi", "x"]
    assert t(["run", "--rm", "img", "sh", "-c", "x"]) == ["run", "--rm", "img", "sh", "-c", "x"]
    assert t(["version"]) == ["info"]
    with pytest.raises(cli.UsageError, match="compose"):
        t(["compose", "up"])


def test_split_argv_handles_docker_run():
    assert cli._split_argv(["docker", "run", "--rm", "img", "grep", "-v", "x"]) == (
        ["docker", "run"], ["--rm", "img", "grep", "-v", "x"]
    )
    assert cli._split_argv(["docker", "build", "-t", "x", "."]) == (["docker", "build", "-t", "x", "."], None)


def test_split_argv_handles_docker_create_and_bare_docker():
    assert cli._split_argv(["docker", "create", "--name", "c", "img"]) == (
        ["docker", "create"], ["--name", "c", "img"]
    )
    assert cli._split_argv(["docker"]) == (["docker"], None)
    assert cli._split_argv(["--engine", "ns", "docker", "build", "-t", "x", "."]) == (
        ["--engine", "ns", "docker", "build", "-t", "x", "."], None
    )


def test_build_ignored_flags_do_not_swallow_context(home, tmp_path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")
    parser = cli.build_parser()
    args = parser.parse_args(["build", "--rm", "-t", "x", str(ctx)])
    assert args.context == str(ctx) and args.tag == ["x"] and args.rm is True
    args = parser.parse_args(["build", "--pull", str(ctx)])
    assert args.context == str(ctx) and args.pull is True


def test_build_commit_tag_flow(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo built > /built\nCMD [\"/bin/cat\", \"/built\"]\n")
    assert cli.main([*e, "build", "-t", "xcodon-test/cli-built:1", str(ctx)]) == 0
    out = capfd.readouterr().out
    assert "Successfully built" in out
    assert cli.main([*e, "run", "--rm", "xcodon-test/cli-built:1"]) == 0
    assert capfd.readouterr().out == "built\n"
    assert cli.main([*e, "tag", "xcodon-test/cli-built:1", "xcodon-test/cli-built:latest"]) == 0
    assert cli.main([*e, "image", "inspect", "xcodon-test/cli-built:latest"]) == 0
    assert json.loads(capfd.readouterr().out)[0]["Config"]["Cmd"] == ["/bin/cat", "/built"]
    assert cli.main([*e, "docker", "image", "inspect", "nope/none:latest"]) == 1
    capfd.readouterr()
    assert cli.main([*e, "create", "--name", "cc", "xcodon-test/busybox", "/bin/sh"]) == 0
    assert cli.main([*e, "start", "cc"]) == 0
    assert cli.main([*e, "exec", "cc", "/bin/sh", "-c", "echo c > /committed"]) == 0
    assert cli.main([*e, "stop", "cc"]) == 0
    assert cli.main([*e, "commit", "-m", "snap", "-c", "ENV SNAP=1", "cc", "xcodon-test/snap:1"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "run", "--rm", "xcodon-test/snap:1", "/bin/sh", "-c", "cat /committed; echo $SNAP"]) == 0
    assert capfd.readouterr().out == "c\n1\n"
    assert cli.main([*e, "rm", "cc"]) == 0


def test_commit_env_dir_shifts_positional_and_expands_scope(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    env_dir = tmp_path / "envdir"
    assert cli.main([*e, "run", "--rm", f"--env-dir={env_dir}", "xcodon-test/busybox",
                      "/bin/sh", "-c", "echo x > /f"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "commit", "--env-dir", str(env_dir), "--image", "xcodon-test/busybox",
                      "-c", "ENV P=$PATH", "xcodon-test/envcommit:1"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "inspect", "xcodon-test/envcommit:1"]) == 0
    doc = json.loads(capfd.readouterr().out)[0]
    env = dict(item.split("=", 1) for item in doc["Config"]["Env"])
    assert env["P"] == env["PATH"]


def test_docker_dispatcher_build_and_inspect(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo d > /d\n")
    assert cli.main([*e, "docker", "build", "-t", "xcodon-test/via-docker:latest", str(ctx)]) == 0
    capfd.readouterr()
    assert cli.main([*e, "docker", "image", "inspect", "xcodon-test/via-docker:latest"]) == 0
    assert json.loads(capfd.readouterr().out)[0]["Id"].startswith("sha256:")
    assert cli.main([*e, "docker", "compose", "up"]) == 125
    assert cli.main([*e, "docker", "version"]) == 0
    assert json.loads(capfd.readouterr().out)["version"]


def test_shim_install_and_use(home, busybox_image, engine_name, tmp_path, monkeypatch):
    shim_dir = tmp_path / "shimbin"
    # The dev host has a real /usr/bin/docker, so the first install must not see it.
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert cli.main(["shim", "install", "--dir", str(shim_dir)]) == 0
    shim = shim_dir / "docker"
    assert shim.exists() and os.access(shim, os.X_OK)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo s > /s\n")
    env = {**os.environ, "PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path),
           "XCODON_ENGINE": engine_name}
    r = subprocess.run(["docker", "build", "-t", "xcodon-test/shim:latest", str(ctx)], env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(["docker", "image", "inspect", "xcodon-test/shim:latest"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)[0]["Id"].startswith("sha256:")
    r = subprocess.run(["docker", "image", "inspect", "nope/none"], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    # refuses to shadow a real docker unless forced
    fake = tmp_path / "realbin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\necho real\n")
    (fake / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "other")]) == 125
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "other"), "--force"]) == 0
