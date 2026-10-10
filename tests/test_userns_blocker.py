"""When the user namespace probe fails, its error names the setting that blocks it."""

import errno
import os
import sys

import pytest

from xcodon_runtime import probe


@pytest.fixture
def proc_sys(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "PROC_SYS", tmp_path)
    (tmp_path / "kernel").mkdir()
    (tmp_path / "user").mkdir()
    return tmp_path


def _deny_id_maps(monkeypatch):
    monkeypatch.setattr(probe.sc, "unshare", lambda flags: None)

    def deny(*a):
        raise PermissionError(errno.EPERM, "Operation not permitted", "/proc/self/uid_map")

    monkeypatch.setattr(probe.sc, "write_id_maps", deny)


def test_apparmor_restriction_is_named(proc_sys, monkeypatch):
    (proc_sys / "kernel" / "apparmor_restrict_unprivileged_userns").write_text("1\n")
    _deny_id_maps(monkeypatch)
    with pytest.raises(OSError) as e:
        probe._enter_userns()
    msg = str(e.value)
    assert "Operation not permitted" in msg
    assert "kernel.apparmor_restrict_unprivileged_userns=1" in msg and "AppArmor" in msg
    assert os.path.realpath(sys.executable) in msg, "names the interpreter a profile must cover"


@pytest.mark.parametrize("path,value,expect", [
    ("user/max_user_namespaces", "0", "user.max_user_namespaces is 0"),
    ("kernel/unprivileged_userns_clone", "0", "kernel.unprivileged_userns_clone is 0"),
])
def test_other_switches_are_named(proc_sys, monkeypatch, path, value, expect):
    (proc_sys / path).write_text(value + "\n")
    monkeypatch.setattr(probe.sc, "unshare",
                        lambda flags: (_ for _ in ()).throw(OSError(errno.EPERM, "unshare: Operation not permitted")))
    with pytest.raises(OSError, match=expect):
        probe._enter_userns()


def test_no_known_switch_keeps_the_plain_error(proc_sys, monkeypatch):
    (proc_sys / "kernel" / "apparmor_restrict_unprivileged_userns").write_text("0\n")
    (proc_sys / "user" / "max_user_namespaces").write_text("63000\n")
    _deny_id_maps(monkeypatch)
    with pytest.raises(OSError) as e:
        probe._enter_userns()
    assert "AppArmor" not in str(e.value) and "max_user_namespaces" not in str(e.value)
