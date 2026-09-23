import gc
import gzip
import io
import os
import stat
import tarfile
import warnings
from pathlib import Path

import pytest

from xcodon_runtime.errors import UnsupportedLayer
from xcodon_runtime.tarlayer import extract_layer, open_layer_stream, _layer_filter


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


def test_open_layer_stream_gzip_no_resource_leak(tmp_path):
    raw = make_tar([(ti("f"), b"1")])
    gz_path = tmp_path / "gz"
    gz_path.write_bytes(gzip.compress(raw))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        s = open_layer_stream(gz_path)
        s.close()
        del s
        gc.collect()
        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) == 0, f"ResourceWarning: {resource_warnings}"


def test_layer_filter_clears_ownership(tmp_path):
    member = ti("file", uid=12345, gid=12345)
    member.uname = "nobody"
    member.gname = "nogroup"
    filtered = _layer_filter(member, str(tmp_path))
    assert filtered.uid is None
    assert filtered.gid is None
    assert filtered.uname is None
    assert filtered.gname is None

    # Also test that extracted files have the current user's uid
    data = make_tar([(ti("file", uid=12345, gid=12345), b"content")])
    extract_layer(io.BytesIO(data), tmp_path)
    assert os.lstat(tmp_path / "file").st_uid == os.getuid()


def test_hardlink_escaping_dest_is_skipped(tmp_path):
    """A hardlink whose linkname points outside dest must not link the host file in."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.chmod(outside, 0o600)
    os.utime(outside, (1000000, 1000000))
    before = os.lstat(outside)
    dest = tmp_path / "dest"
    dest.mkdir()
    data = make_tar(
        [
            (ti("stolen", tarfile.LNKTYPE, mode=0o777, linkname="../outside.txt"), None),
            (ti("ok"), b"fine"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), dest)
    assert skipped == 1
    assert not (dest / "stolen").exists()
    assert (dest / "ok").read_bytes() == b"fine"
    after = os.lstat(outside)
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime == before.st_mtime
    assert outside.read_text() == "secret"


def test_hardlink_with_absolute_linkname_is_skipped(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("host file")
    os.chmod(victim, 0o600)
    dest = tmp_path / "dest"
    dest.mkdir()
    data = make_tar([(ti("grab", tarfile.LNKTYPE, mode=0o777, linkname=str(victim)), None)])
    skipped = extract_layer(io.BytesIO(data), dest)
    assert skipped == 1
    assert not (dest / "grab").exists()
    assert stat.S_IMODE(os.lstat(victim).st_mode) == 0o600


def test_dangling_hardlink_is_skipped(tmp_path):
    """A hardlink whose target was never extracted is skipped, and the rest still lands.

    Left to tarfile, such a member makes it scan the whole archive for the
    target and raise KeyError, which on a stream eats the members after it.
    """
    data = make_tar(
        [
            (ti("dangling", tarfile.LNKTYPE, linkname="never-there"), None),
            (ti("after"), b"still extracted"),
        ]
    )
    skipped = extract_layer(io.BytesIO(data), tmp_path)
    assert skipped == 1
    assert (tmp_path / "after").read_bytes() == b"still extracted"
