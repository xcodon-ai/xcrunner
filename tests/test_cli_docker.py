import json
import os
import subprocess

import pytest

from xcodon_runtime import cli


def test_translate_docker_argv():
    t = cli.translate_docker_argv
    assert t(["build", "-t", "x/y:1", "ctx"]) == ["build", "-t", "x/y:1", "ctx"]
    assert t(["image", "inspect", "x/y:1"]) == ["inspect", "x/y:1"]
    # --format is kept, not dropped: cmd_inspect understands "{{.Id}}" itself.
    assert t(["image", "inspect", "--format", "{{.Id}}", "x"]) == ["inspect", "--format", "{{.Id}}", "x"]
    assert t(["image", "inspect", "--type", "image", "x"]) == ["inspect", "x"]
    assert t(["images"]) == ["images"] and t(["image", "ls"]) == ["images"]
    assert t(["image", "rm", "x"]) == ["rmi", "x"] and t(["rmi", "x"]) == ["rmi", "x"]
    assert t(["run", "--rm", "img", "sh", "-c", "x"]) == ["run", "--rm", "img", "sh", "-c", "x"]
    assert t(["version"]) == ["info"]
    with pytest.raises(cli.UsageError, match="compose"):
        t(["compose", "up"])


def test_translate_docker_argv_image_error_names_the_verb():
    """Regression: the message used to be `docker image ['prune']: ...` (a Python list
    repr), not the verb text."""
    with pytest.raises(cli.UsageError, match=r"^docker image prune: not supported by xcrunner$"):
        cli.translate_docker_argv(["image", "prune"])


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


def test_inspect_format_id_and_multiple_images(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    assert cli.main([*e, "tag", "xcodon-test/busybox:latest", "xcodon-test/busybox2:latest"]) == 0
    capfd.readouterr()

    # `--format '{{.Id}}'` (whitespace inside the braces allowed) prints just the id.
    assert cli.main([*e, "docker", "image", "inspect", "--format", "{{.Id}}",
                      "xcodon-test/busybox:latest"]) == 0
    out = capfd.readouterr().out
    assert out.strip().startswith("sha256:") and "\n" not in out.strip()

    assert cli.main([*e, "docker", "image", "inspect", "--format", "{{ .Id }}",
                      "xcodon-test/busybox:latest"]) == 0
    assert capfd.readouterr().out.strip().startswith("sha256:")

    # any other --format keeps printing JSON and warns (on stderr) that it is ignored.
    assert cli.main([*e, "docker", "image", "inspect", "--format", "{{.RepoTags}}",
                      "xcodon-test/busybox:latest"]) == 0
    captured = capfd.readouterr()
    assert json.loads(captured.out)[0]["Id"].startswith("sha256:")
    assert "ignoring" in captured.err

    # several images: one list covering both.
    assert cli.main([*e, "docker", "image", "inspect", "xcodon-test/busybox:latest",
                      "xcodon-test/busybox2:latest"]) == 0
    docs = json.loads(capfd.readouterr().out)
    assert len(docs) == 2 and all(d["Id"].startswith("sha256:") for d in docs)

    # exits 1 if any of several is missing, still printing the ones that exist.
    assert cli.main([*e, "docker", "image", "inspect", "xcodon-test/busybox:latest",
                      "nope/none:latest"]) == 1
    docs = json.loads(capfd.readouterr().out)
    assert len(docs) == 1 and docs[0]["Id"].startswith("sha256:")


def test_commit_change_env_expands_against_earlier_changes(home, busybox_image, engine_name, capfd):
    """`-c 'ENV A=1' -c 'ENV B=$A'` must give B=1: a later -c sees an earlier -c's Env,
    not just the base image's own."""
    e = ["--engine", engine_name]
    assert cli.main([*e, "create", "--name", "chaintest", "xcodon-test/busybox", "/bin/sh"]) == 0
    assert cli.main([*e, "start", "chaintest"]) == 0
    assert cli.main([*e, "stop", "chaintest"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "commit", "-c", "ENV A=1", "-c", "ENV B=$A", "chaintest",
                      "xcodon-test/chain:1"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "inspect", "xcodon-test/chain:1"]) == 0
    doc = json.loads(capfd.readouterr().out)[0]
    env = dict(item.split("=", 1) for item in doc["Config"]["Env"])
    assert env["B"] == "1"
    assert cli.main([*e, "rm", "chaintest"]) == 0


def test_shim_install_force_replaces_symlink_without_writing_through_it(tmp_path, monkeypatch):
    """Probe 1: DIR/docker is a symlink to a real docker. --force must replace the
    directory entry (os.replace), never open-and-write through the link onto whatever
    it points at."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    real_docker = tmp_path / "realbin" / "docker"
    real_docker.parent.mkdir()
    real_docker.write_text("#!/bin/sh\necho real\n")
    real_docker.chmod(0o755)
    d3 = tmp_path / "d3"
    d3.mkdir()
    link = d3 / "docker"
    link.symlink_to(real_docker)

    assert cli.main(["shim", "install", "--dir", str(d3), "--force"]) == 0

    assert not link.is_symlink(), "the symlink must be replaced by a regular file"
    assert cli.SHIM_MARKER in link.read_text()
    assert real_docker.read_text() == "#!/bin/sh\necho real\n", "the link's old target must be untouched"


def test_shim_install_refuses_existing_non_shim_file_even_when_dir_is_off_path(tmp_path, monkeypatch):
    """Probe 2: DIR/docker is a plain user script and DIR is not on PATH at all (so the
    PATH-wide check cannot see it). Installing without --force must still refuse and
    leave the file untouched."""
    empty = tmp_path / "empty-path2"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    d2 = tmp_path / "d2"
    d2.mkdir()
    user_script = d2 / "docker"
    user_script.write_text("#!/bin/sh\necho mine\n")
    user_script.chmod(0o755)

    assert cli.main(["shim", "install", "--dir", str(d2)]) == 125
    assert user_script.read_text() == "#!/bin/sh\necho mine\n"

    assert cli.main(["shim", "install", "--dir", str(d2), "--force"]) == 0
    assert cli.SHIM_MARKER in user_script.read_text()


def test_shim_install_force_onto_a_directory_is_a_clean_error(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-path3"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    d3 = tmp_path / "d3"
    (d3 / "docker").mkdir(parents=True)
    (d3 / "docker" / "keep").write_text("k")

    assert cli.main(["shim", "install", "--dir", str(d3), "--force"]) == 125
    err = capsys.readouterr().err
    assert f"{d3 / 'docker'} is a directory; remove it first" in err
    assert "Traceback" not in err and "IsADirectoryError" not in err
    assert (d3 / "docker" / "keep").read_text() == "k"
    assert sorted(p.name for p in d3.iterdir()) == ["docker"], "no temp file left behind"


def test_shim_install_refuses_real_docker_hidden_behind_a_stale_shim_on_path(tmp_path, monkeypatch):
    """PATH is shim1:realbin, and shim1/docker is itself an old xcrunner shim. The
    resolver must keep walking PATH past it and find the real docker in realbin."""
    shim1 = tmp_path / "shim1"
    shim1.mkdir()
    old_shim = shim1 / "docker"
    old_shim.write_text(f"#!/bin/sh\n{cli.SHIM_MARKER}\nexit 0\n")
    old_shim.chmod(0o755)
    real_bin = tmp_path / "realbin2"
    real_bin.mkdir()
    (real_bin / "docker").write_text("#!/bin/sh\necho real\n")
    (real_bin / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim1}:{real_bin}")

    assert cli.main(["shim", "install", "--dir", str(tmp_path / "installed")]) == 125


def test_docker_dispatch_preserves_outer_verbosity(home, engine_name, monkeypatch):
    """`cmd_docker` re-enters `main` with a translated argv that carries no `-v` of its
    own; the inner call must not reconfigure logging (and so reset verbosity to 0)."""
    calls = []
    orig = cli._configure_logging

    def spy(v):
        calls.append(v)
        orig(v)

    monkeypatch.setattr(cli, "_configure_logging", spy)
    assert cli.main(["-v", "--engine", engine_name, "docker", "version"]) == 0
    assert calls == [1]


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
