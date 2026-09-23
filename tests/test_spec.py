from pathlib import Path

import pytest

from xcodon_runtime.errors import XcodonError
from xcodon_runtime.spec import DEFAULT_PATH, build_spec, resolve_user


@pytest.fixture
def rootfs(tmp_path: Path) -> Path:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\nappuser:x:1001:2001:App:/srv/app:/bin/sh\n"
    )
    (tmp_path / "etc/group").write_text("root:x:0:\nappgrp:x:2001:\nstaff:x:50:\n")
    return tmp_path


CID = "abcdef1234567890" * 4


def test_argv_from_entrypoint_and_cmd(rootfs):
    cfg = {"Entrypoint": ["/bin/tool"], "Cmd": ["--help"]}
    assert build_spec({"config": cfg}, rootfs, CID).argv == ["/bin/tool", "--help"]
    assert build_spec({"config": cfg}, rootfs, CID, command=["run"]).argv == ["/bin/tool", "run"]
    assert build_spec({"config": cfg}, rootfs, CID, entrypoint=["/bin/sh"], command=["-c", "x"]).argv == ["/bin/sh", "-c", "x"]
    assert build_spec({"config": {"Cmd": ["sh"]}}, rootfs, CID, entrypoint=[]).argv == ["sh"]


def test_empty_argv_is_error(rootfs):
    with pytest.raises(XcodonError, match="no command"):
        build_spec({"config": {}}, rootfs, CID)


def test_env_layers(rootfs):
    cfg = {"Env": ["PATH=/usr/bin", "A=1"]}
    s = build_spec({"config": cfg}, rootfs, CID, command=["x"], env={"A": "2", "B": "3"})
    assert s.env["PATH"] == "/usr/bin"
    assert s.env["A"] == "2"
    assert s.env["B"] == "3"
    assert s.env["HOSTNAME"] == CID[:12]
    assert s.env["HOME"] == "/root"


def test_default_path_when_missing(rootfs):
    s = build_spec({"config": {}}, rootfs, CID, command=["x"])
    assert s.env["PATH"] == DEFAULT_PATH


def test_home_from_passwd_for_user(rootfs):
    s = build_spec({"config": {"User": "appuser"}}, rootfs, CID, command=["x"])
    assert (s.uid, s.gid) == (1001, 2001)
    assert s.env["HOME"] == "/srv/app"


def test_home_override_wins(rootfs):
    s = build_spec({"config": {"Env": ["HOME=/opt"]}}, rootfs, CID, command=["x"])
    assert s.env["HOME"] == "/opt"


def test_workdir_rules(rootfs):
    assert build_spec({"config": {}}, rootfs, CID, command=["x"]).workdir == "/"
    assert build_spec({"config": {"WorkingDir": "/w"}}, rootfs, CID, command=["x"]).workdir == "/w"
    assert build_spec({"config": {"WorkingDir": "/w"}}, rootfs, CID, command=["x"], workdir="/o").workdir == "/o"


@pytest.mark.parametrize(
    "user,expected",
    [
        (None, (0, 0)),
        ("", (0, 0)),
        ("root", (0, 0)),
        ("appuser", (1001, 2001)),
        ("appuser:staff", (1001, 50)),
        ("1001", (1001, 2001)),
        ("1001:50", (1001, 50)),
        ("4242", (4242, 4242)),
        ("4242:4343", (4242, 4343)),
    ],
)
def test_resolve_user(rootfs, user, expected):
    assert resolve_user(user, rootfs) == expected


def test_unknown_user_name_is_error(rootfs):
    with pytest.raises(XcodonError, match="unknown user"):
        resolve_user("nobody-here", rootfs)


def test_user_option_overrides_image(rootfs):
    s = build_spec({"config": {"User": "appuser"}}, rootfs, CID, command=["x"], user="0")
    assert (s.uid, s.gid) == (0, 0)
