"""XCRUNNER_CONTAINER_DIR: container folders on a separate (node-local) disk. See spec section 15."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from xcodon_runtime import engine as eng
from xcodon_runtime import probe
from xcodon_runtime.api import Runtime
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.home import CONTAINER_DIR_ENV, RuntimeHome


def test_the_setting_is_named_xcrunner_container_dir():
    assert CONTAINER_DIR_ENV == "XCRUNNER_CONTAINER_DIR"


def test_default_container_dir_is_inside_the_home(tmp_path, monkeypatch):
    monkeypatch.delenv(CONTAINER_DIR_ENV, raising=False)
    h = RuntimeHome(tmp_path / "h")
    assert h.containers == (tmp_path / "h").resolve() / "containers"


def test_empty_setting_means_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(CONTAINER_DIR_ENV, "")
    assert RuntimeHome(tmp_path / "h").containers == (tmp_path / "h").resolve() / "containers"


def test_setting_moves_the_container_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(CONTAINER_DIR_ENV, str(tmp_path / "local" / "c"))
    h = RuntimeHome(tmp_path / "h")
    assert h.containers == (tmp_path / "local" / "c").resolve()
    assert h.containers.is_dir()
    assert not (h.path / "containers").exists()
    # The image store stays in the home.
    assert h.images == h.path / "images"


def test_argument_beats_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv(CONTAINER_DIR_ENV, str(tmp_path / "fromenv"))
    h = RuntimeHome(tmp_path / "h", containers=tmp_path / "fromarg")
    assert h.containers == (tmp_path / "fromarg").resolve()


@pytest.mark.parametrize("bad", ["a,b", "a:b"])
def test_overlay_separators_are_refused(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(CONTAINER_DIR_ENV, str(tmp_path / bad))
    with pytest.raises(XcodonError, match=CONTAINER_DIR_ENV):
        RuntimeHome(tmp_path / "h")


def test_select_engine_probes_with_the_container_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    h = RuntimeHome(tmp_path / "h", containers=tmp_path / "local")
    seen = []

    def fake(home_path, containers_path=None):
        seen.append((home_path, containers_path))
        return {n: {"ok": True, "error": ""} for n in eng.PROBE_NAMES}

    monkeypatch.setattr(eng, "run_probes", fake)
    eng.select_engine(h)
    assert seen == [(h.path, h.containers)]


def test_run_probes_passes_the_container_dir_to_each_child(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return Done()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    local = tmp_path / "local"
    probe.run_probes(tmp_path / "h", local)
    assert calls and all(argv[-1] == str(local.resolve()) for argv in calls)


def test_overlay_probe_puts_the_writable_side_in_the_container_dir(tmp_path, monkeypatch):
    mounts = []

    def fake_mount(source, target, fstype, flags=0, data=None):
        opts = dict(kv.split("=", 1) for kv in data.split(","))
        mounts.append(opts)
        shutil.copy(Path(opts["lowerdir"]) / "f", Path(target) / "f")

    monkeypatch.setattr(probe, "_enter_userns", lambda: None)
    monkeypatch.setattr(probe.sc, "mount", fake_mount)
    monkeypatch.setattr(probe.sc, "umount2", lambda *a: None)
    home, local = tmp_path / "home", tmp_path / "local"
    home.mkdir()
    local.mkdir()
    probe._probe_overlay(home, local)
    (opts,) = mounts
    assert Path(opts["lowerdir"]).is_relative_to(home)
    assert Path(opts["upperdir"]).is_relative_to(local)
    assert Path(opts["workdir"]).is_relative_to(local)
    assert list(home.iterdir()) == [] and list(local.iterdir()) == []


@pytest.fixture
def local_dir(monkeypatch):
    """A container dir on another filesystem when /dev/shm exists, like a node-local disk under a shared home."""
    parent = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    d = Path(tempfile.mkdtemp(prefix="xcrunner-local-", dir=parent)).resolve()
    monkeypatch.setenv(CONTAINER_DIR_ENV, str(d))
    yield d
    from xcodon_runtime.containers import _rmtree_tolerant

    _rmtree_tolerant(d)


def test_containers_run_commit_and_remove_in_the_container_dir(home, busybox_image, engine_name, local_dir):
    rt = Runtime(home.path, engine=engine_name)
    assert rt.home.containers == local_dir
    c = rt.create("xcodon-test/busybox", command=["/bin/sh"], name="one")
    assert c.dir.parent == local_dir
    rt.start(c)
    assert rt.exec(c, "echo x > /f && cat /f").stdout == b"x\n"
    rt.stop(c)
    writable = c.dir / ("upper" if engine_name == "ns" else "rootfs")
    assert (writable / "f").read_text() == "x\n"
    rt.start(c)
    assert rt.exec(c, ["/bin/cat", "/f"]).stdout == b"x\n"
    rt.stop(c)
    assert [x.id for x in rt.containers(all=True)] == [c.id]
    assert rt.get_container("one").id == c.id
    # Nothing container-related lands in the (shared) home.
    assert not any((home.path / "containers").iterdir())
    assert not list(home.locks.glob("container*"))
    assert list((local_dir / ".locks").glob("container-*"))
    img = rt.commit(c, tag="xcodon-test/committed")
    assert img.rootfs.is_relative_to(home.path)
    rt.remove(c)
    assert not c.dir.exists()
    assert rt.run("xcodon-test/committed", ["/bin/cat", "/f"], rm=True, stdout=open(os.devnull, "w")) == 0
    assert rt.containers(all=True) == []
    assert rt.info()["containers"] == str(local_dir)


def test_an_overlay_only_failure_points_at_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    probes = {n: {"ok": True, "error": ""} for n in eng.PROBE_NAMES}
    probes["overlay"] = {"ok": False, "error": "overlay: Invalid argument"}
    monkeypatch.setattr(eng, "run_probes", lambda *a: probes)
    choice = eng.select_engine(RuntimeHome(tmp_path / "h"))
    assert choice.name == "proot"
    assert CONTAINER_DIR_ENV in choice.reason


@pytest.mark.ns
def test_real_probes_pass_with_a_separate_container_dir(home, local_dir):
    results = probe.run_probes(home.path, local_dir)
    assert all(r["ok"] for r in results.values()), results
    assert list(local_dir.iterdir()) == []
