import os
from pathlib import Path

from xcodon_runtime.flatten import build_rootfs


def mk(root: Path, files: dict[str, str | None]):
    """files: path -> content; None means directory; 'LINK:target' means symlink."""
    for rel, content in files.items():
        p = root / rel
        if content is None:
            p.mkdir(parents=True, exist_ok=True)
        elif isinstance(content, str) and content.startswith("LINK:"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(content[5:])
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return root


def test_whiteout_and_opaque(tmp_path):
    a = mk(tmp_path / "a", {"a.txt": "A", "dir/x": "x", "dir/y": "y", "keep": "k"})
    b = mk(tmp_path / "b", {".wh.a.txt": "", "dir/.wh.x": "", "new.txt": "N"})
    c = mk(tmp_path / "c", {"dir/.wh..wh..opq": "", "dir/z": "z"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b, c], out)
    assert not (out / "a.txt").exists()
    assert (out / "new.txt").read_text() == "N"
    assert (out / "keep").read_text() == "k"
    assert sorted(p.name for p in (out / "dir").iterdir()) == ["z"]
    assert not any(p.name.startswith(".wh.") for p in out.rglob("*"))


def test_file_replaces_dir_and_dir_replaces_file(tmp_path):
    a = mk(tmp_path / "a", {"thing/inner": "i", "other": "o"})
    b = mk(tmp_path / "b", {"thing": "now a file", "other/child": "c"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert (out / "thing").read_text() == "now a file"
    assert (out / "other/child").read_text() == "c"


def test_symlink_replaces_dir(tmp_path):
    a = mk(tmp_path / "a", {"lib64/f": "f", "usr/lib64/g": "g"})
    b = mk(tmp_path / "b", {"lib64": "LINK:usr/lib64"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert os.readlink(out / "lib64") == "usr/lib64"


def test_files_are_hardlinks_into_layers(tmp_path):
    a = mk(tmp_path / "a", {"big": "content"})
    out = tmp_path / "rootfs"
    build_rootfs([a], out)
    assert os.stat(out / "big").st_ino == os.stat(a / "big").st_ino


def test_later_layer_overrides_file(tmp_path):
    a = mk(tmp_path / "a", {"f": "old"})
    b = mk(tmp_path / "b", {"f": "new"})
    out = tmp_path / "rootfs"
    build_rootfs([a, b], out)
    assert (out / "f").read_text() == "new"
    assert (a / "f").read_text() == "old", "layers must never be modified"


def test_directory_mode_copied(tmp_path):
    a = mk(tmp_path / "a", {"d": None})
    os.chmod(a / "d", 0o750)
    out = tmp_path / "rootfs"
    build_rootfs([a], out)
    assert oct(os.stat(out / "d").st_mode & 0o777) == oct(0o750)
