"""Per-platform micromamba pins and the macOS ARM hint. See spec section 16.6."""

import hashlib
import io
import platform as std_platform

import pytest

from tests.fake_micromamba import make_fake_micromamba
from tests.test_micromamba import FAKE_BINARY, _archive
from xcodon_runtime import micromamba as mm
from xcodon_runtime.condashim import OSX_ARM_HINT, conda_main
from xcodon_runtime.errors import XcodonError


def _host(monkeypatch, system, machine):
    monkeypatch.setattr(std_platform, "system", lambda: system)
    monkeypatch.setattr(std_platform, "machine", lambda: machine)


@pytest.mark.parametrize("system,machine,subdir", [
    ("Linux", "x86_64", "linux-64"), ("Linux", "amd64", "linux-64"), ("Darwin", "arm64", "osx-arm64"),
    ("Darwin", "x86_64", None), ("Linux", "aarch64", None), ("Windows", "AMD64", None),
])
def test_host_subdir(monkeypatch, system, machine, subdir):
    _host(monkeypatch, system, machine)
    assert mm.host_subdir() == subdir


def test_the_osx_arm64_pin_is_the_conda_forge_2_9_0_package():
    assert mm.MICROMAMBA_OSX_ARM64_URL == \
        "https://conda.anaconda.org/conda-forge/osx-arm64/micromamba-2.9.0-0.tar.bz2"
    assert mm.MICROMAMBA_OSX_ARM64_ARCHIVE_SHA256 == \
        "500f5074feb8d02c4296ef9921c3650ed2874171805a9fbb8fbb53896433646b"
    assert mm.MICROMAMBA_OSX_ARM64_BINARY_SHA256 == \
        "ec2a072f028e1a7cf20f3e2e74d5a8127cf5a5f27636375b5359811565f4e5be"


def test_a_mac_downloads_and_checks_the_osx_arm64_package(home, monkeypatch):
    _host(monkeypatch, "Darwin", "arm64")
    data = _archive()
    monkeypatch.setattr(mm, "MICROMAMBA_OSX_ARM64_ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(mm, "MICROMAMBA_OSX_ARM64_BINARY_SHA256", hashlib.sha256(FAKE_BINARY).hexdigest())
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        return io.BytesIO(data)

    path = mm.install_micromamba(home, opener=opener)
    assert path.read_bytes() == FAKE_BINARY
    assert calls == [mm.MICROMAMBA_OSX_ARM64_URL]
    assert mm.install_micromamba(home, opener=opener) == path
    assert len(calls) == 1, "a verified binary is not downloaded again"


def test_a_mac_rejects_a_linux_archive(home, monkeypatch):
    _host(monkeypatch, "Darwin", "arm64")
    with pytest.raises(XcodonError, match=mm.MICROMAMBA_OSX_ARM64_ARCHIVE_SHA256):
        mm.install_micromamba(home, opener=lambda url, timeout=None: io.BytesIO(_archive()))


def test_an_unsupported_host_names_the_supported_ones(home, monkeypatch):
    _host(monkeypatch, "Linux", "aarch64")
    with pytest.raises(XcodonError, match="linux-64, osx-arm64 only; this host is Linux aarch64"):
        mm.install_micromamba(home, opener=lambda url, timeout=None: io.BytesIO(b""))


@pytest.fixture
def conda_env(tmp_path):
    fake = make_fake_micromamba(tmp_path / "fakebin")
    proj = tmp_path / "proj"
    (proj / ".xrunner-env").mkdir(parents=True)
    return proj, {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-user-home", "XRUNNER_MICROMAMBA": str(fake),
                  "FAKE_MM_LOG": str(tmp_path / "mm.log")}


@pytest.mark.parametrize("verb", ["create", "install", "update"])
def test_a_failed_install_on_a_mac_prints_the_hint(home, conda_env, monkeypatch, verb):
    _host(monkeypatch, "Darwin", "arm64")
    proj, env = conda_env
    err = io.StringIO()
    assert conda_main([verb, "-n", "a", "samtools"], home, cwd=proj, environ={**env, "FAKE_MM_EXIT": "1"},
                      err=err) == 1
    assert OSX_ARM_HINT in err.getvalue()
    assert "--platform osx-64" in OSX_ARM_HINT


def test_no_hint_on_success_or_on_linux(home, conda_env, monkeypatch):
    proj, env = conda_env
    _host(monkeypatch, "Darwin", "arm64")
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=proj, environ=env, err=err) == 0
    assert OSX_ARM_HINT not in err.getvalue()
    _host(monkeypatch, "Linux", "x86_64")
    err = io.StringIO()
    assert conda_main(["install", "-n", "a", "x"], home, cwd=proj, environ={**env, "FAKE_MM_EXIT": "1"},
                      err=err) == 1
    assert OSX_ARM_HINT not in err.getvalue()
