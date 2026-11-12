"""Host paths that are absolute symlinks still mount on the ns engine.

The keeper mounts after pivot_root, where an absolute symlink on the host would
resolve inside the container. /etc/resolv.conf and /etc/hosts are resolved in the
keeper before the pivot (seen with a VPN that links /etc/resolv.conf into /opt);
user bind sources are resolved when the container is created.
"""

import os

import pytest

from xcodon_runtime import keeper
from xcodon_runtime.api import Runtime
from xcodon_runtime.engine import Bind


def _binds(steps):
    return {s[2]: s[1] for s in steps if s[0] == "bind"}


def test_host_files_behind_an_absolute_link_are_resolved(tmp_path, monkeypatch):
    real = tmp_path / "vpn" / "resolv.conf"
    real.parent.mkdir()
    real.write_text("nameserver 10.0.0.1\n")
    link = tmp_path / "etc-resolv.conf"
    link.symlink_to(real)  # absolute target
    monkeypatch.setattr(keeper, "HOST_FILES", (str(link),))
    steps = keeper.build_mount_steps({"merged": "/m", "binds": [], "workdir": "/"}, "oldroot")
    assert _binds(steps)[str(link)] == f"/oldroot{real}"


@pytest.mark.ns
def test_a_bind_through_an_absolute_link_works_in_a_container(home, busybox_image, tmp_path):
    (tmp_path / "gpfs" / "home").mkdir(parents=True)
    (tmp_path / "gpfs" / "home" / "file.txt").write_text("through the link\n")
    (tmp_path / "home").symlink_to(tmp_path / "gpfs" / "home")
    rt = Runtime(home.path, engine="ns")
    out = tmp_path / "out"
    with open(out, "wb") as f:
        code = rt.run("xcodon-test/busybox", ["/bin/cat", "/data/file.txt"], rm=True,
                      binds=[Bind(str(tmp_path / "home"), "/data")], stdout=f)
    assert code == 0 and out.read_text() == "through the link\n"


@pytest.mark.ns
def test_resolv_conf_reaches_the_container(home, busybox_image, tmp_path):
    rt = Runtime(home.path, engine="ns")
    out = tmp_path / "out"
    with open(out, "wb") as f:
        code = rt.run("xcodon-test/busybox", ["/bin/cat", "/etc/resolv.conf"], rm=True, stdout=f)
    host = open(os.path.realpath("/etc/resolv.conf")).read()
    assert code == 0 and out.read_text() == host
