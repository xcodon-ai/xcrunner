import json
import logging
import os
import subprocess
import sys

import pytest

from xcodon_runtime import __version__, cli
from xcodon_runtime.engine import Bind


def test_parse_run_args_docker_style():
    opts = cli.parse_run_args(
        [
            "--mount=type=bind,source=/h/out,target=/var/spool/cwl",
            "--mount=type=bind,source=/h/tmp,target=/tmp,readonly",
            "-v", "/a:/b:ro", "--volume=/c:/d",
            "--workdir=/var/spool/cwl", "--env=HOME=/var/spool/cwl", "-e", "TMPDIR=/tmp",
            "--rm", "-i", "--user=1000:1000", "--name", "job1", "--entrypoint=/bin/sh",
            "--memory=512m", "--net=none", "--read-only=true", "--log-driver=none", "--cpus", "2", "--gpus=1",
            "--cidfile=/h/cid", "--pull=always",
            "busybox:latest", "sh", "-c", "echo --not-a-flag",
        ]
    )
    assert opts.binds == [
        Bind("/h/out", "/var/spool/cwl", False),
        Bind("/h/tmp", "/tmp", True),
        Bind("/a", "/b", True),
        Bind("/c", "/d", False),
    ]
    assert opts.workdir == "/var/spool/cwl"
    assert opts.env == {"HOME": "/var/spool/cwl", "TMPDIR": "/tmp"}
    assert opts.rm and opts.user == "1000:1000" and opts.name == "job1"
    assert opts.entrypoint == ["/bin/sh"]
    assert opts.cidfile == "/h/cid" and opts.pull == "always"
    assert opts.image == "busybox:latest"
    assert opts.command == ["sh", "-c", "echo --not-a-flag"]
    assert {"--memory", "--net", "--read-only", "--log-driver", "--cpus", "--gpus"} <= set(opts.ignored)


def test_split_argv_keeps_command_flags_out_of_argparse():
    head, rest = cli._split_argv(["--engine", "proot", "-v", "run", "--rm", "img", "grep", "-v", "x"])
    assert head == ["--engine", "proot", "-v", "run"]
    assert rest == ["--rm", "img", "grep", "-v", "x"]
    assert cli._split_argv(["ps", "-a"]) == (["ps", "-a"], None)
    assert cli._split_argv(["create", "img"]) == (["create"], ["img"])


def test_run_help_exits_zero(home, capsys):
    assert cli.main(["run", "--help"]) == 0
    assert "IMAGE [COMMAND...]" in capsys.readouterr().out


def test_parse_run_args_unknown_flag_is_error():
    with pytest.raises(cli.UsageError, match="--privileged"):
        cli.parse_run_args(["--privileged", "img"])


def test_parse_run_args_requires_image():
    with pytest.raises(cli.UsageError, match="image"):
        cli.parse_run_args(["--rm"])


def test_parse_mount_requires_source_and_target():
    with pytest.raises(cli.UsageError, match="target"):
        cli.parse_run_args(["--mount=type=bind,source=/x", "img"])


def test_runtime_error_exit_125(home, capsys):
    code = cli.main(["inspect-not-a-command"])
    assert code == 2
    code = cli.main(["--engine", "proot", "start", "nosuchcontainer"])
    assert code == 125
    assert "xcrunner:" in capsys.readouterr().err


def test_inspect_missing_image_prints_empty_array(home, capsys):
    assert cli.main(["inspect", "nobody/nothing:latest"]) == 1
    assert json.loads(capsys.readouterr().out) == []


def test_info_prints_json(home, engine_name, capsys):
    assert cli.main(["--engine", engine_name, "info"]) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == engine_name


def test_full_cli_flow(home, busybox_image, engine_name, capfd, tmp_path):
    # capfd, not capsys: `exec` and `run` let the child inherit our real stdio (docker-style
    # streaming), and capsys only sees writes made in-process through sys.stdout/sys.stderr.
    e = ["--engine", engine_name]
    assert cli.main([*e, "images"]) == 0
    assert "xcodon-test/busybox:latest" in capfd.readouterr().out
    assert cli.main([*e, "inspect", "xcodon-test/busybox"]) == 0
    assert json.loads(capfd.readouterr().out)[0]["Config"]["Cmd"] == ["/bin/sh"]

    assert cli.main([*e, "create", "--name", "c1", "xcodon-test/busybox", "/bin/sh"]) == 0
    cid = capfd.readouterr().out.strip()
    assert len(cid) == 64
    assert cli.main([*e, "start", "c1"]) == 0
    assert cli.main([*e, "exec", "-w", "/tmp", "--env=Q=1", "c1", "/bin/sh", "-c", "pwd; echo $Q; exit 3"]) == 3
    assert capfd.readouterr().out == "/tmp\n1\n"
    assert cli.main([*e, "ps"]) == 0
    assert "c1" in capfd.readouterr().out
    assert cli.main([*e, "stop", "c1"]) == 0
    assert cli.main([*e, "rm", "c1"]) == 0

    out = tmp_path / "o"
    out.mkdir()
    cid_file = tmp_path / "cid"
    code = cli.main([*e, "run", "--rm", f"--mount=type=bind,source={out},target=/o", "--workdir=/o",
                     "--env=NAME=cwl", f"--cidfile={cid_file}", "--memory=10m", "xcodon-test/busybox",
                     "/bin/sh", "-c", "echo hello $NAME > f; exit 6"])
    assert code == 6
    assert (out / "f").read_text() == "hello cwl\n"
    assert len(cid_file.read_text().strip()) == 64
    assert cli.main([*e, "ps", "-a"]) == 0
    assert len(capfd.readouterr().out.splitlines()) == 1, "run --rm must leave no container"
    assert cli.main([*e, "prune"]) == 0


def test_exec_dash_e_without_value_copies_host_env(home, busybox_image, engine_name, monkeypatch, capfd):
    """`-e VAR` (no `=`) forwards our own environment's value, as docker does. `parse_run_args`
    already worked this way for `run`/`create`; `cmd_exec` used to just set an empty string."""
    monkeypatch.setenv("HOSTVAL", "yes")
    e = ["--engine", engine_name]
    assert cli.main([*e, "create", "--name", "c2", "xcodon-test/busybox", "/bin/sh"]) == 0
    capfd.readouterr()
    assert cli.main([*e, "start", "c2"]) == 0
    assert cli.main([*e, "exec", "-e", "HOSTVAL", "c2", "/bin/sh", "-c", "echo $HOSTVAL"]) == 0
    assert capfd.readouterr().out == "yes\n"
    assert cli.main([*e, "stop", "c2"]) == 0
    assert cli.main([*e, "rm", "c2"]) == 0


def test_create_cidfile_error_exit_125(home, busybox_image, capsys):
    code = cli.main(["create", "--cidfile=/nonexistent/dir/cid", "xcodon-test/busybox:latest"])
    assert code == 125
    assert "xcrunner:" in capsys.readouterr().err


def test_verbosity_resets_between_invocations(home, capsys):
    """logging.basicConfig is a no-op after the first call unless forced, so a second main()
    in the same process must not keep the first call's level."""
    assert cli.main(["-vv", "info"]) == 0
    assert logging.getLogger().level == logging.DEBUG
    assert cli.main(["info"]) == 0
    assert logging.getLogger().level == logging.WARNING


def test_console_script_entry_point():
    r = subprocess.run([sys.executable, "-m", "xcodon_runtime.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "pull" in r.stdout


def test_xcrunner_console_script():
    """The console script is `xcrunner`; the `xcodon` name belongs to the agent."""
    exe = os.path.join(os.path.dirname(sys.executable), "xcrunner")
    assert os.path.exists(exe), "install the package so the xcrunner script exists"
    r = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert r.returncode == 0
    assert __version__ in r.stdout


def test_bare_read_only_does_not_swallow_the_image():
    """docker's --read-only is boolean; taking a value here ate the image name."""
    opts = cli.parse_run_args(["--read-only", "busybox", "echo", "hi"])
    assert opts.image == "busybox"
    assert opts.command == ["echo", "hi"]
    assert "--read-only" in opts.ignored


def test_mount_readonly_false_is_writable():
    assert cli.parse_run_args(["--mount=type=bind,source=/h,target=/t,readonly=false", "img"]).binds == [
        Bind("/h", "/t", False)
    ]
    assert cli.parse_run_args(["--mount=type=bind,source=/h,target=/t,ro=false", "img"]).binds == [
        Bind("/h", "/t", False)
    ]
    assert cli.parse_run_args(["--mount=type=bind,source=/h,target=/t,ro=true", "img"]).binds == [
        Bind("/h", "/t", True)
    ]
    assert cli.parse_run_args(["--mount=type=bind,source=/h,target=/t,readonly", "img"]).binds == [
        Bind("/h", "/t", True)
    ]


def test_bad_reference_exits_125_without_a_traceback(home, capsys):
    """cwltool passes user-typed dockerPull strings; a bare ValueError escaped main()."""
    assert cli.main(["pull", "BAD REF!!"]) == 125
    assert "xcrunner:" in capsys.readouterr().err


def test_bad_platform_exits_125(home, capsys):
    assert cli.main(["pull", "--platform", "junk", "busybox"]) == 125
    assert "xcrunner:" in capsys.readouterr().err


def test_logs_on_a_proot_container_explains_there_is_none(home, busybox_image, capfd):
    assert cli.main(["--engine", "proot", "create", "--name", "plog", "xcodon-test/busybox"]) == 0
    capfd.readouterr()
    assert cli.main(["--engine", "proot", "logs", "plog"]) == 0
    out, err = capfd.readouterr()
    assert out == ""
    assert "no keeper log: proot engine" in err


def test_parse_run_args_env_dir_is_absolutized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = cli.parse_run_args(["--env-dir", "myenv", "img", "true"])
    assert opts.env_dir == str(tmp_path / "myenv")
    opts = cli.parse_run_args(["--env-dir=/abs/env", "img"])
    assert opts.env_dir == "/abs/env"
    assert cli.parse_run_args(["img"]).env_dir is None


def test_parse_run_args_env_dir_rejects_empty():
    for argv in (["--env-dir=", "img"], ["--env-dir", "", "img"]):
        with pytest.raises(cli.UsageError, match="--env-dir"):
            cli.parse_run_args(argv)


def test_parse_run_args_env_dir_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    opts = cli.parse_run_args(["--env-dir", "~/envs/py", "img"])
    assert opts.env_dir == str(tmp_path / "envs" / "py")


def test_cli_env_dir_persists_installs(home, busybox_image, engine_name, tmp_path, capfd):
    e = ["--engine", engine_name]
    env = tmp_path / "env"
    assert cli.main([*e, "run", "--rm", f"--env-dir={env}", "xcodon-test/busybox", "/bin/sh", "-c", "mkdir -p /usr/local && echo tool > /usr/local/tool"]) == 0
    assert cli.main([*e, "run", "--rm", f"--env-dir={env}", "xcodon-test/busybox", "/bin/cat", "/usr/local/tool"]) == 0
    assert capfd.readouterr().out.strip().endswith("tool")
    assert cli.main([*e, "create", "--name", "envc", f"--env-dir={env}", "xcodon-test/busybox", "/bin/sh"]) == 0
    c = cli.Runtime(home.path, engine=engine_name).get_container("envc")
    assert c.env_dir == str(env)
    assert cli.main([*e, "rm", "envc"]) == 0
    assert (env / c.image_id).is_dir()


def test_rmi_reports_untagged_and_deleted(home, busybox_image, capfd):
    from xcodon_runtime import cli as cli_mod

    assert cli_mod.main(["tag", "xcodon-test/busybox", "xcodon-test/busybox:second"]) == 0
    capfd.readouterr()
    assert cli_mod.main(["rmi", "xcodon-test/busybox:second"]) == 0
    out = capfd.readouterr().out
    assert out == "Untagged: docker.io/xcodon-test/busybox:second\n", "a shared image is only untagged"
    assert cli_mod.main(["rmi", "xcodon-test/busybox"]) == 0
    out = capfd.readouterr().out.splitlines()
    assert out == ["Untagged: docker.io/xcodon-test/busybox:latest", f"Deleted: sha256:{busybox_image.id}"]
    assert cli_mod.main(["rmi", "xcodon-test/busybox"]) == 125
    assert "run: xcrunner pull" in capfd.readouterr().err
