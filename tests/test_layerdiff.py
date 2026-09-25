import errno
import os
import stat
from pathlib import Path

import pytest

from xcodon_runtime.layerdiff import OPAQUE, hash_layer_dir, link_tree, snapshot_diff, snapshot_upper


def mk(root: Path, files: dict[str, str | None]) -> Path:
    for rel, content in files.items():
        p = root / rel
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
        elif content.startswith("LINK:"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(content[5:])
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return root


def test_snapshot_upper_translates_opaque_dirs_and_copies_files(tmp_path):
    upper = mk(tmp_path / "upper", {"bin/added": "m", "newdir": None, "replaced/new": "n", "lnk": "LINK:/etc/hosts"})
    try:
        os.setxattr(upper / "replaced", "user.overlay.opaque", b"y")
        os.setxattr(upper / "bin", "user.overlay.origin", b"\x00")
    except OSError:
        pytest.skip("user xattrs unsupported on this filesystem")
    dest = tmp_path / "layer"
    n = snapshot_upper(upper, dest)
    assert (dest / "bin/added").read_text() == "m"
    assert os.stat(dest / "bin/added").st_ino == os.stat(upper / "bin/added").st_ino
    assert (dest / "replaced" / OPAQUE).exists()
    assert (dest / "replaced/new").read_text() == "n"
    assert os.readlink(dest / "lnk") == "/etc/hosts"
    assert (dest / "newdir").is_dir()
    assert not os.listxattr(dest / "bin"), "overlay attributes are not carried over"
    assert n >= 5


@pytest.mark.ns
def test_snapshot_upper_translates_whiteout_devices(home, busybox_image):
    """Real overlay whiteouts need a user namespace to create; use a container."""
    from xcodon_runtime.api import Runtime
    from xcodon_runtime.engine_ns import NsEngine

    rt = Runtime(home.path, engine="ns")
    c = rt.create("xcodon-test/busybox")
    rt.start(c)
    assert rt.exec(c, ["/bin/sh", "-c", "rm /bin/ls; rm -rf /home/user && mkdir /home/user && echo n > /home/user/x"]).code == 0
    rt.stop(c)
    upper, _ = NsEngine().layer_paths(c)
    dest = home.path / "snap"
    snapshot_upper(upper, dest)
    assert (dest / "bin" / ".wh.ls").is_file()
    assert (dest / "home/user" / OPAQUE).exists()
    assert (dest / "home/user/x").read_text() == "n\n"
    assert not (dest / "bin" / "ls").exists()


def test_snapshot_diff_finds_added_changed_and_deleted(tmp_path):
    base = mk(tmp_path / "base", {"keep": "k", "changed": "old", "gone": "g", "d/inner": "i", "gonedir/f": "f"})
    cur = mk(tmp_path / "cur", {"keep": "k", "changed": "new!", "d/inner": "i", "d/added": "a", "newlink": "LINK:keep"})
    # same mtime for unchanged files so only content/size decides
    for rel in ("keep", "d/inner"):
        st = os.stat(base / rel)
        os.utime(cur / rel, ns=(st.st_atime_ns, st.st_mtime_ns))
    dest = tmp_path / "layer"
    snapshot_diff(cur, base, dest)
    assert (dest / "changed").read_text() == "new!"
    assert (dest / "d/added").read_text() == "a"
    assert (dest / ".wh.gone").is_file()
    assert (dest / ".wh.gonedir").is_file()
    assert os.readlink(dest / "newlink") == "keep"
    assert not (dest / "keep").exists()
    assert not (dest / "d/inner").exists()


def test_hash_layer_dir_is_deterministic_and_content_sensitive(tmp_path):
    a = mk(tmp_path / "a", {"x/y": "1", "z": "2"})
    b = mk(tmp_path / "b", {"x/y": "1", "z": "2"})
    os.utime(b / "z", ns=(1, 1))
    assert hash_layer_dir(a) == hash_layer_dir(b)
    assert hash_layer_dir(a).startswith("sha256:")
    (b / "z").write_text("3")
    assert hash_layer_dir(a) != hash_layer_dir(b)
    assert hash_layer_dir(mk(tmp_path / "empty", {})) == hash_layer_dir(mk(tmp_path / "empty2", {}))


def test_link_tree(tmp_path):
    src = mk(tmp_path / "src", {"a/b": "x", "l": "LINK:a/b"})
    os.chmod(src / "a", 0o750)
    link_tree(src, tmp_path / "dst")
    assert os.stat(tmp_path / "dst/a/b").st_ino == os.stat(src / "a/b").st_ino
    assert os.readlink(tmp_path / "dst/l") == "a/b"
    assert stat.S_IMODE(os.stat(tmp_path / "dst/a").st_mode) == 0o750


def test_link_tree_preserves_restrictive_dir_mode(tmp_path):
    """A 0o500 source dir must not end up widened to 0o700 in the copy."""
    src = mk(tmp_path / "src2", {"d/f": "x"})
    os.chmod(src / "d", 0o500)
    dst_root = tmp_path / "dst2"
    try:
        link_tree(src, dst_root)
        assert stat.S_IMODE(os.stat(dst_root / "d").st_mode) == 0o500
        assert (dst_root / "d" / "f").read_text() == "x"
    finally:
        os.chmod(src / "d", 0o750)
        os.chmod(dst_root / "d", 0o750)


def test_snapshot_upper_preserves_restrictive_dir_mode(tmp_path):
    """Same guarantee for snapshot_upper: the layer dir keeps the upper dir's exact mode."""
    upper = mk(tmp_path / "upper2", {"d/f": "x"})
    os.chmod(upper / "d", 0o500)
    dest = tmp_path / "layer2"
    try:
        snapshot_upper(upper, dest)
        assert stat.S_IMODE(os.stat(dest / "d").st_mode) == 0o500
        assert (dest / "d" / "f").read_text() == "x"
    finally:
        os.chmod(upper / "d", 0o750)
        os.chmod(dest / "d", 0o750)


def test_place_file_copy_fallback_strips_overlay_xattrs(tmp_path, monkeypatch):
    """When os.link fails (e.g. EXDEV) and copy2 falls back, overlay bookkeeping must not leak."""
    from xcodon_runtime import layerdiff

    src = mk(tmp_path / "src3", {"f": "x"})
    target = src / "f"
    try:
        os.setxattr(target, "user.overlay.origin", b"\x00")
    except OSError:
        pytest.skip("user xattrs unsupported on this filesystem")

    def fake_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(layerdiff.os, "link", fake_link)
    dst = tmp_path / "dst3" / "f"
    dst.parent.mkdir(parents=True, exist_ok=True)
    layerdiff._place_file(target, dst)
    assert dst.read_text() == "x"
    assert not os.listxattr(dst), "overlay xattrs must not leak through the copy fallback"
