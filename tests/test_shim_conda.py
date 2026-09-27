# tests/test_shim_conda.py
import os
import subprocess

import pytest

from tests.fake_micromamba import make_fake_micromamba
from xcodon_runtime import cli
from xcodon_runtime.shim import CONDA_MARKER, is_xcrunner_shim, resolve_real


@pytest.fixture
def fake_mm(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin")


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    d = tmp_path / "empty-path"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


def test_install_writes_three_forwarding_shims(home, tmp_path, fake_mm, empty_path, capfd):
    shim_dir = tmp_path / "shim"
    assert cli.main(["shim", "install", "conda", "--dir", str(shim_dir), "--micromamba", str(fake_mm)]) == 0
    out = capfd.readouterr().out
    for name in ("conda", "mamba", "micromamba"):
        p = shim_dir / name
        text = p.read_text()
        assert text.startswith("#!/bin/sh\n") and CONDA_MARKER in text
        assert text.rstrip().endswith('conda "$@"') and "xcrunner" in text
        assert os.access(p, os.X_OK) and is_xcrunner_shim(p)
        assert f"installed {p}" in out
    assert (home.path / "bin" / "micromamba-2.9.0" / "micromamba").read_bytes() == fake_mm.read_bytes()
    assert "add it to PATH" in out
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "XCODON_RUNTIME_HOME": str(home.path), "HOME": str(tmp_path)}
    r = subprocess.run(["mamba", "--version"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "conda 2.9.0 (micromamba via xcrunner)\n"


def test_install_refuses_a_real_conda_on_path(home, tmp_path, fake_mm, monkeypatch):
    real = tmp_path / "realbin"
    real.mkdir()
    (real / "mamba").write_text("#!/bin/sh\necho real\n")
    (real / "mamba").chmod(0o755)
    monkeypatch.setenv("PATH", str(real))
    assert resolve_real("mamba") == str(real / "mamba")
    args = ["shim", "install", "conda", "--dir", str(tmp_path / "shim"), "--micromamba", str(fake_mm)]
    assert cli.main(args) == 125
    assert not (tmp_path / "shim" / "conda").exists()
    assert cli.main(args + ["--force"]) == 0


def test_install_never_writes_through_a_link_or_over_a_user_file(home, tmp_path, fake_mm, empty_path):
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    target = tmp_path / "user-conda"
    target.write_text("#!/bin/sh\necho mine\n")
    (shim_dir / "conda").symlink_to(target)
    args = ["shim", "install", "conda", "--dir", str(shim_dir), "--micromamba", str(fake_mm)]
    assert cli.main(args) == 125
    assert cli.main(args + ["--force"]) == 0
    assert target.read_text() == "#!/bin/sh\necho mine\n"
    assert not (shim_dir / "conda").is_symlink() and is_xcrunner_shim(shim_dir / "conda")
    assert cli.main(args) == 0, "replacing its own shims needs no --force"


def test_a_conda_shim_on_path_is_not_a_real_conda(home, tmp_path, fake_mm, empty_path, monkeypatch):
    first = tmp_path / "first"
    assert cli.main(["shim", "install", "conda", "--dir", str(first), "--micromamba", str(fake_mm)]) == 0
    monkeypatch.setenv("PATH", f"{first}:{empty_path}")
    assert resolve_real("conda") is None
    assert cli.main(["shim", "install", "conda", "--dir", str(tmp_path / "second"),
                     "--micromamba", str(fake_mm)]) == 0


def test_docker_is_still_the_default_kind(home, tmp_path, empty_path):
    assert cli.main(["shim", "install", "--dir", str(tmp_path / "d")]) == 0
    assert (tmp_path / "d" / "docker").exists() and not (tmp_path / "d" / "conda").exists()
    assert cli.main(["shim", "install", "docker", "--dir", str(tmp_path / "d"), "--micromamba", "x"]) == 125


# -- fix round 1 -------------------------------------------------------------------------


def test_write_shim_refuses_cleanly_on_a_read_only_dir(home, tmp_path, fake_mm, empty_path, capfd):
    """An OSError while writing (a read-only DIR, here) must become a clean ShimRefused
    -- exit 125 with a plain message -- not a raw traceback with exit 1."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim_dir.chmod(0o555)
    try:
        args = ["shim", "install", "conda", "--dir", str(shim_dir), "--micromamba", str(fake_mm)]
        assert cli.main(args) == 125
        err = capfd.readouterr().err
        assert "cannot write" in err and "Traceback" not in err
    finally:
        shim_dir.chmod(0o755)
