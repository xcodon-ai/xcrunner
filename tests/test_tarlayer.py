import gzip
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from xcodon_runtime.errors import UnsupportedLayer
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream


def make_tar(entries) -> bytes:
    """entries: list of (TarInfo, data bytes or None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            else:
                t.addfile(info)
    return buf.getvalue()


def ti(name, type=tarfile.REGTYPE, mode=0o644, linkname="", uid=0, gid=0):
    i = tarfile.TarInfo(name)
    i.type = type
    i.mode = mode
    i.linkname = linkname
    i.uid, i.gid = uid, gid
    return i


def test_extract_regular_files_symlinks_and_dirs(tmp_path):
    data = make_tar(
        [
            (ti("etc", tarfile.DIRTYPE, 0o755), None),
            (ti("etc/passwd", uid=0, gid=0), b"root:x:0:0::/root:/bin/sh\n"),
            (ti("bin", tarfile.SYMTYPE, linkname="usr/bin"), None),
            (ti("etc/alt", tarfile.SYMTYPE, linkname="/usr/bin/sudo"), None),
            (ti("usr/bin/sudo", mode=0o4755), b"x"),
            (ti("tmp", tarfile.DIRTYPE, 0o1777), None),
            (ti("ro", tarfile.DIRTYPE, 0o555), None),
            (ti("ro/inside"), b"y"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), tmp_path)
    assert skipped == 0
    assert (tmp_path / "etc/passwd").read_bytes().startswith(b"root")
    assert os.readlink(tmp_path / "bin") == "usr/bin"
    assert os.readlink(tmp_path / "etc/alt") == "/usr/bin/sudo"
    mode = stat.S_IMODE(os.lstat(tmp_path / "usr/bin/sudo").st_mode)
    assert not mode & stat.S_ISUID
    assert stat.S_IMODE(os.lstat(tmp_path / "tmp").st_mode) & 0o700 == 0o700
    assert (tmp_path / "ro/inside").read_bytes() == b"y", "files inside 0555 dirs must extract"
    assert os.lstat(tmp_path / "etc/passwd").st_uid == os.getuid()


def test_skips_devices_and_escapes(tmp_path, caplog):
    data = make_tar(
        [
            (ti("dev/null", tarfile.CHRTYPE), None),
            (ti("escape", tarfile.SYMTYPE, linkname="../../outside"), None),
            (ti("escape/evil"), b"pwned"),
            (ti("ok"), b"fine"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), tmp_path)
    assert skipped == 2
    assert (tmp_path / "ok").exists()
    assert not (tmp_path.parent / "outside").exists()
    assert not (tmp_path / "dev/null").exists()


def test_hardlinks_preserved(tmp_path):
    data = make_tar(
        [
            (ti("a"), b"same"),
            (ti("b", tarfile.LNKTYPE, linkname="a"), None),
        ]
    )
    extract_layer(io.BytesIO(data), tmp_path)
    assert os.stat(tmp_path / "a").st_ino == os.stat(tmp_path / "b").st_ino


def test_open_layer_stream_detects_gzip_and_plain(tmp_path):
    raw = make_tar([(ti("f"), b"1")])
    (tmp_path / "plain").write_bytes(raw)
    (tmp_path / "gz").write_bytes(gzip.compress(raw))
    for name in ("plain", "gz"):
        with open_layer_stream(tmp_path / name) as s:
            dest = tmp_path / f"out-{name}"
            dest.mkdir()
            extract_layer(s, dest)
            assert (dest / "f").read_bytes() == b"1"


def test_open_layer_stream_zstd_without_module(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zstandard":
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    (tmp_path / "z").write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 16)
    with pytest.raises(UnsupportedLayer, match="zstd"):
        open_layer_stream(tmp_path / "z")
