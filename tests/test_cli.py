import json
import subprocess
import sys

import pytest

from xcodon_runtime import cli
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
    assert "xcodon:" in capsys.readouterr().err


def test_inspect_missing_image_prints_empty_array(home, capsys):
    assert cli.main(["inspect", "nobody/nothing:latest"]) == 1
    assert json.loads(capsys.readouterr().out) == []


def test_info_prints_json(home, engine_name, capsys):
    assert cli.main(["--engine", engine_name, "info"]) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == engine_name


def test_full_cli_flow(home, busybox_image, engine_name, capsys, tmp_path):
    e = ["--engine", engine_name]
    assert cli.main([*e, "images"]) == 0
    assert "xcodon-test/busybox:latest" in capsys.readouterr().out
    assert cli.main([*e, "inspect", "xcodon-test/busybox"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["Config"]["Cmd"] == ["/bin/sh"]

    assert cli.main([*e, "create", "--name", "c1", "xcodon-test/busybox", "/bin/sh"]) == 0
    cid = capsys.readouterr().out.strip()
    assert len(cid) == 64
    assert cli.main([*e, "start", "c1"]) == 0
    assert cli.main([*e, "exec", "-w", "/tmp", "--env=Q=1", "c1", "/bin/sh", "-c", "pwd; echo $Q; exit 3"]) == 3
    assert capsys.readouterr().out == "/tmp\n1\n"
    assert cli.main([*e, "ps"]) == 0
    assert "c1" in capsys.readouterr().out
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
    assert len(capsys.readouterr().out.splitlines()) == 1, "run --rm must leave no container"
    assert cli.main([*e, "prune"]) == 0


def test_console_script_entry_point():
    r = subprocess.run([sys.executable, "-m", "xcodon_runtime.cli", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "pull" in r.stdout
