"""x86_64 images on ARM hosts through binfmt emulation. See spec section 16.7."""

import logging
import platform as std_platform

import pytest

from tests.fake_registry import FakeRegistry
from tests.test_registry import config_for, layer_bytes
from xcodon_runtime import reference
from xcodon_runtime.errors import PullError
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.reference import Platform, emulated_platforms, host_can_run
from xcodon_runtime.registry import RegistryClient, select_platform

HANDLER_OK = "enabled\ninterpreter /mnt/lima-rosetta/rosetta\nflags: OCF\noffset 0\nmagic 7f454c46\n"


@pytest.fixture
def binfmt(tmp_path, monkeypatch):
    d = tmp_path / "binfmt_misc"
    d.mkdir()
    monkeypatch.setattr(reference, "BINFMT_DIR", d)
    return d


@pytest.fixture
def arm_host(monkeypatch):
    monkeypatch.setattr(std_platform, "machine", lambda: "aarch64")


@pytest.mark.parametrize("name", ["rosetta", "qemu-x86_64"])
def test_arm_host_with_an_f_flag_handler_emulates_amd64(binfmt, arm_host, name):
    (binfmt / name).write_text(HANDLER_OK)
    assert emulated_platforms() == (Platform("linux", "amd64"),)
    assert host_can_run(Platform("linux", "amd64"))
    assert host_can_run(Platform("linux", "arm64"))


@pytest.mark.parametrize("text", [
    HANDLER_OK.replace("enabled", "disabled"),
    HANDLER_OK.replace("flags: OCF", "flags: OC"),
    "enabled\ninterpreter /x\n",
    "",
])
def test_unusable_handlers_do_not_count(binfmt, arm_host, text):
    (binfmt / "rosetta").write_text(text)
    assert emulated_platforms() == ()
    assert not host_can_run(Platform("linux", "amd64"))


def test_no_handler_file_means_no_emulation(binfmt, arm_host):
    assert emulated_platforms() == ()


def test_x86_host_emulates_nothing(binfmt, monkeypatch):
    monkeypatch.setattr(std_platform, "machine", lambda: "x86_64")
    (binfmt / "rosetta").write_text(HANDLER_OK)
    assert emulated_platforms() == ()
    assert host_can_run(Platform("linux", "amd64"))
    assert not host_can_run(Platform("linux", "arm64"))
    assert not host_can_run(Platform("windows", "amd64"))


def test_select_platform_falls_back_and_says_so(caplog):
    manifests = [{"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]
    with pytest.raises(PullError, match="no manifest for linux/arm64"):
        select_platform(manifests, Platform("linux", "arm64"))
    with caplog.at_level(logging.WARNING):
        got = select_platform(manifests, Platform("linux", "arm64"), fallbacks=(Platform("linux", "amd64"),))
    assert got == "sha256:" + "b" * 64
    assert "linux/amd64" in caplog.text and "emulation" in caplog.text


def test_the_host_platform_wins_over_a_fallback():
    manifests = [{"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
                 {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "arm64"}}]
    got = select_platform(manifests, Platform("linux", "arm64"), fallbacks=(Platform("linux", "amd64"),))
    assert got == "sha256:" + "a" * 64


@pytest.fixture
def reg():
    with FakeRegistry() as r:
        yield r


def _store(home):
    return ImageStore(home, sources=[RegistryClient(home, scheme="http")])


def test_pull_falls_back_to_amd64_in_an_index(home, reg, binfmt, arm_host):
    (binfmt / "rosetta").write_text(HANDLER_OK)
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    img = _store(home).pull(f"{reg.host}/lib/multi:v1")
    assert img.config["architecture"] == "amd64"


def test_pull_without_emulation_finds_no_arm_manifest(home, reg, binfmt, arm_host):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/multi", "v1", config_for(layers), layers, multi_arch=True)
    with pytest.raises(PullError, match="no manifest for linux/arm64"):
        _store(home).pull(f"{reg.host}/lib/multi:v1")


def test_a_single_platform_image_that_cannot_run_is_refused(home, reg, binfmt, arm_host):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/x86only", "v1", config_for(layers), layers)
    with pytest.raises(PullError, match="linux/amd64 only; this host runs linux/arm64"):
        _store(home).pull(f"{reg.host}/lib/x86only:v1")


def test_a_single_platform_image_runs_through_emulation(home, reg, binfmt, arm_host):
    (binfmt / "rosetta").write_text(HANDLER_OK)
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/x86only", "v1", config_for(layers), layers)
    assert _store(home).pull(f"{reg.host}/lib/x86only:v1").config["architecture"] == "amd64"


def test_an_explicit_platform_skips_the_fallback_and_the_check(home, reg, binfmt, arm_host):
    layers = [layer_bytes("a", b"A")]
    reg.add_image("lib/x86only", "v1", config_for(layers), layers)
    img = _store(home).pull(f"{reg.host}/lib/x86only:v1", Platform("linux", "amd64"))
    assert img.config["architecture"] == "amd64"
