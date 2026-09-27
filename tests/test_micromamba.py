import hashlib
import io
import os
import tarfile

import pytest

from xcodon_runtime import micromamba as mm
from xcodon_runtime.errors import XcodonError

FAKE_BINARY = b"#!/bin/sh\necho 2.9.0\n"


def _archive(binary: bytes = FAKE_BINARY) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        info = tarfile.TarInfo("bin/micromamba")
        info.size = len(binary)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(binary))
        other = tarfile.TarInfo("info/index.json")
        other.size = 2
        tar.addfile(other, io.BytesIO(b"{}"))
    return buf.getvalue()


@pytest.fixture
def pinned_fake(monkeypatch):
    data = _archive()
    monkeypatch.setattr(mm, "MICROMAMBA_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(mm, "MICROMAMBA_BINARY_SHA256", hashlib.sha256(FAKE_BINARY).hexdigest())
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        return io.BytesIO(data)

    return opener, calls


def test_pinned_values_are_the_conda_forge_2_9_0_package():
    assert mm.MICROMAMBA_VERSION == "2.9.0"
    assert mm.MICROMAMBA_URL == "https://conda.anaconda.org/conda-forge/linux-64/micromamba-2.9.0-0.tar.bz2"
    assert mm.MICROMAMBA_ARCHIVE_SHA256 == "8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd"
    assert mm.MICROMAMBA_BINARY_SHA256 == "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"


def test_install_downloads_verifies_and_extracts(home, pinned_fake):
    opener, calls = pinned_fake
    path = mm.install_micromamba(home, opener=opener)
    assert path == home.path / "bin" / "micromamba-2.9.0" / "micromamba" == mm.pinned_path(home)
    assert path.read_bytes() == FAKE_BINARY
    assert os.access(path, os.X_OK)
    assert calls == [mm.MICROMAMBA_URL]
    assert mm.install_micromamba(home, opener=opener) == path
    assert calls == [mm.MICROMAMBA_URL], "a verified binary is not downloaded again"
    assert [p.name for p in path.parent.iterdir()] == ["micromamba"], "no temp files left"
    assert [p.name for p in path.parent.parent.iterdir()] == ["micromamba-2.9.0"], "no temp files left"


def test_install_rejects_a_wrong_archive_checksum(home, pinned_fake, monkeypatch):
    opener, _ = pinned_fake
    monkeypatch.setattr(mm, "MICROMAMBA_ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(XcodonError, match="checksum"):
        mm.install_micromamba(home, opener=opener)
    assert not mm.pinned_path(home).exists()


def test_install_rejects_a_wrong_binary_checksum(home, pinned_fake, monkeypatch):
    opener, _ = pinned_fake
    monkeypatch.setattr(mm, "MICROMAMBA_BINARY_SHA256", "0" * 64)
    with pytest.raises(XcodonError, match="checksum"):
        mm.install_micromamba(home, opener=opener)
    assert not mm.pinned_path(home).exists()


def test_install_reports_download_errors(home):
    def broken(url, timeout=None):
        raise OSError("network down")

    with pytest.raises(XcodonError, match="--micromamba"):
        mm.install_micromamba(home, opener=broken)


def test_install_copies_a_given_binary(home, tmp_path):
    src = tmp_path / "my-micromamba"
    src.write_bytes(b"#!/bin/sh\necho mine\n")
    path = mm.install_micromamba(home, source=src)
    assert path.read_bytes() == src.read_bytes() and os.access(path, os.X_OK)
    with pytest.raises(XcodonError, match="not a file"):
        mm.install_micromamba(home, source=tmp_path / "missing")


def test_install_refuses_other_platforms(home, monkeypatch, pinned_fake):
    monkeypatch.setattr(mm.platform, "machine", lambda: "aarch64")
    with pytest.raises(XcodonError, match="linux-64"):
        mm.install_micromamba(home, opener=pinned_fake[0])


def test_find_prefers_the_environment_variable(home, tmp_path):
    exe = tmp_path / "mm"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert mm.find_micromamba(home, {"XCRUNNER_MICROMAMBA": str(exe)}) == exe
    with pytest.raises(mm.MicromambaMissing, match="XCRUNNER_MICROMAMBA"):
        mm.find_micromamba(home, {"XCRUNNER_MICROMAMBA": str(tmp_path / "nope")})


def test_find_uses_the_pinned_binary_or_explains(home, pinned_fake):
    with pytest.raises(mm.MicromambaMissing, match="run: xcrunner shim install conda"):
        mm.find_micromamba(home, {})
    path = mm.install_micromamba(home, opener=pinned_fake[0])
    assert mm.find_micromamba(home, {}) == path


# -- final review --------------------------------------------------------------------


def test_pinned_binary_is_named_micromamba(home):
    """micromamba names itself after its file in the hints and errors it prints
    ("micromamba-2.9 run -n ..." for a file named micromamba-2.9.0), so the pinned
    file must be called `micromamba`, the name the shim serves."""
    assert mm.pinned_path(home).name == "micromamba"
    assert mm.pinned_path(home).parent == home.path / "bin" / "micromamba-2.9.0"


def test_install_replaces_a_binary_stored_in_the_earlier_layout(home, pinned_fake):
    old = home.path / "bin" / "micromamba-2.9.0"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old layout")
    path = mm.install_micromamba(home, opener=pinned_fake[0])
    assert path == old / "micromamba" and path.read_bytes() == FAKE_BINARY
