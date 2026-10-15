"""When the default container folder cannot hold overlay layers, a node-local one is chosen.

On the cluster, ~/.xcodon/runtime sits on NFS: the overlay probe failed and xcrunner ran PRoot.
XCRUNNER_CONTAINER_DIR=/tmp/xcrunner-$USER/containers made the same node run ns.
"""

from __future__ import annotations

import os

import pytest

from xcodon_runtime import localdirs
from xcodon_runtime.api import Runtime
from xcodon_runtime.home import CONTAINER_DIR_ENV

ENV_LAYERS = "XCRUNNER_ENV_LAYER_DIR"


@pytest.fixture
def node(home, tmp_path, monkeypatch):
    """A fake node: its local disk is tmp_path/local, the default folder fails overlay."""
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setenv(localdirs.AUTO_ENV, "1")
    monkeypatch.setattr(localdirs, "_candidates", lambda: [local])
    monkeypatch.setattr(localdirs, "_boot_id", lambda: "boot-1")
    monkeypatch.setattr(localdirs, "_free", lambda p: 100 << 30)
    calls = []

    def overlay_ok(home_path, containers):
        calls.append(str(containers))
        return str(containers).startswith(str(local))

    monkeypatch.setattr(localdirs, "_overlay_ok", overlay_ok)
    monkeypatch.delenv(CONTAINER_DIR_ENV, raising=False)
    monkeypatch.delenv(ENV_LAYERS, raising=False)
    return local, calls


def test_a_local_folder_is_chosen_when_the_default_cannot_hold_overlays(node, home):
    local, _ = node
    rt = Runtime(home.path)
    private = local / f"xcrunner-{localdirs._user()}"
    assert rt.home.containers == (private / "containers").resolve()
    assert os.environ[ENV_LAYERS] == str(private / "env-layers")
    assert "auto" in rt.auto_dirs_note and rt.info()["containers_auto"] == rt.auto_dirs_note
    assert oct(os.stat(private).st_mode & 0o777) == "0o700"


def test_the_decision_is_cached_per_boot(node, home, monkeypatch):
    _, calls = node
    Runtime(home.path)
    first = len(calls)
    monkeypatch.delenv(CONTAINER_DIR_ENV)
    Runtime(home.path)
    assert len(calls) == first, "the second process reads the cache"
    monkeypatch.delenv(CONTAINER_DIR_ENV)
    monkeypatch.setattr(localdirs, "_boot_id", lambda: "boot-2")
    Runtime(home.path)
    assert len(calls) > first, "a reboot probes again"


def test_settings_and_working_defaults_are_left_alone(node, home, monkeypatch, tmp_path):
    local, _ = node
    monkeypatch.setenv(CONTAINER_DIR_ENV, str(tmp_path / "mine"))
    assert Runtime(home.path).home.containers == (tmp_path / "mine").resolve()
    monkeypatch.delenv(CONTAINER_DIR_ENV)
    monkeypatch.setenv(localdirs.AUTO_ENV, "0")
    assert Runtime(home.path).home.containers == home.containers
    monkeypatch.setenv(localdirs.AUTO_ENV, "1")
    monkeypatch.setattr(localdirs, "_overlay_ok", lambda h, c: True)
    monkeypatch.setattr(localdirs, "_boot_id", lambda: "boot-ok")
    assert Runtime(home.path).home.containers == home.containers, "the default works: keep it"
    assert Runtime(home.path, engine="proot").auto_dirs_note == ""


def test_a_full_or_foreign_local_folder_is_skipped(node, home, monkeypatch):
    local, _ = node
    monkeypatch.setattr(localdirs, "_free", lambda p: 1 << 30)
    assert Runtime(home.path).home.containers == home.containers, "under 10 GB free"
    monkeypatch.setattr(localdirs, "_free", lambda p: 100 << 30)
    monkeypatch.setattr(localdirs, "_boot_id", lambda: "boot-3")
    private = local / f"xcrunner-{localdirs._user()}"
    os.chmod(private, 0o777)
    assert Runtime(home.path).home.containers == home.containers, "writable by others: not ours"


def test_the_real_overlay_probe_accepts_a_local_folder(home, tmp_path):
    """The probe the choice relies on, for real: a folder on local disk holds overlay layers."""
    from xcodon_runtime.probe import run_probe

    if not run_probe("userns", home.path, home.containers)["ok"]:
        pytest.skip("this host has no unprivileged user namespaces")
    (tmp_path / "c").mkdir()
    assert run_probe("overlay", home.path, tmp_path / "c") == {"ok": True, "error": ""}
