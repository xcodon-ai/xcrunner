import json
import os
import threading
import time
from pathlib import Path

import pytest

from xcodon_runtime.home import RuntimeHome


def test_creates_layout(tmp_path):
    h = RuntimeHome(tmp_path / "h")
    for d in (h.blobs, h.layers, h.images, h.containers, h.locks):
        assert d.is_dir()
    assert h.blobs == tmp_path / "h" / "blobs" / "sha256"


def test_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XCODON_RUNTIME_HOME", str(tmp_path / "fromenv"))
    assert RuntimeHome().path == (tmp_path / "fromenv").resolve()


def test_home_default_is_under_dot_xcodon(monkeypatch, tmp_path):
    monkeypatch.delenv("XCODON_RUNTIME_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert RuntimeHome().path == (tmp_path / ".xcodon" / "runtime").resolve()


def test_atomic_dir_renames_on_success(home):
    final = home.images / "abc"
    with home.atomic_dir(final) as tmp:
        assert tmp.name == "abc.tmp"
        (tmp / "f").write_text("x")
    assert (final / "f").read_text() == "x"
    assert not tmp.exists()


def test_atomic_dir_cleans_up_on_error(home):
    final = home.images / "abc"
    with pytest.raises(RuntimeError):
        with home.atomic_dir(final) as tmp:
            (tmp / "f").write_text("x")
            raise RuntimeError("boom")
    assert not final.exists()
    assert not tmp.exists()


def test_lock_is_exclusive(home):
    order = []

    def worker(name, hold):
        with home.lock("shared"):
            order.append(f"{name}-in")
            time.sleep(hold)
            order.append(f"{name}-out")

    t1 = threading.Thread(target=worker, args=("a", 0.2))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=("b", 0))
    t2.start()
    t1.join()
    t2.join()
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_refs_round_trip(home):
    assert home.read_refs() == {}
    home.write_refs({"docker.io/library/a:latest": "1" * 64})
    assert home.read_refs() == {"docker.io/library/a:latest": "1" * 64}
    assert json.loads(home.refs_file.read_text())


def test_prune_leftovers(home):
    (home.layers / "x.tmp").mkdir()
    (home.images / "y.tmp").mkdir()
    (home.blobs / "z.part").write_bytes(b"")
    (home.layers / "keep").mkdir()
    removed = home.prune_leftovers()
    assert {p.name for p in removed} == {"x.tmp", "y.tmp", "z.part"}
    assert (home.layers / "keep").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="test requires non-root permissions")
def test_prune_leftovers_does_not_report_stuck_dirs(home):
    """Verify that prune_leftovers does not report paths it failed to remove."""
    stuck = home.layers / "stuck.tmp"
    stuck.mkdir()
    (stuck / "file").write_text("content")
    # Make directory unremovable: remove write permission so children cannot be unlinked
    os.chmod(stuck, 0o500)
    try:
        removed = home.prune_leftovers()
        # The stuck directory should still exist and should NOT be in the removed list
        assert stuck.exists(), "stuck directory should still exist"
        assert stuck not in removed, "stuck directory should not be reported as removed"
    finally:
        # Restore permissions so pytest can clean up
        os.chmod(stuck, 0o700)
