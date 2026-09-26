import logging
import os
import subprocess
import sys

import pytest

from tests.fake_micromamba import make_fake_micromamba
from xcodon_runtime import cli, sandbox
from xcodon_runtime.shim import CONDA_MARKER, DOCKER_MARKER, is_xrunner_shim


@pytest.fixture
def env_dir(tmp_path):
    return tmp_path / "proj" / ".xrunner-env"


@pytest.fixture
def fake_mm(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin")


def test_activate_writes_shims_and_returns_the_settings(home, env_dir, fake_mm):
    environ = {"PATH": "/usr/bin:/bin", "XRUNNER_MICROMAMBA": str(fake_mm)}
    settings = sandbox.activate(env_dir, home=home, environ=environ)
    bin_dir = env_dir.resolve() / "bin"
    assert settings == {"PATH": f"{bin_dir}:/usr/bin:/bin", "XRUNNER_ENV_DIR": str(env_dir.resolve())}
    for name, marker in (("docker", DOCKER_MARKER), ("conda", CONDA_MARKER), ("mamba", CONDA_MARKER),
                         ("micromamba", CONDA_MARKER)):
        shim = bin_dir / name
        assert is_xrunner_shim(shim) and marker in shim.read_text() and os.access(shim, os.X_OK)
    assert (bin_dir / "docker").read_text().rstrip().endswith('docker "$@"')
    assert (bin_dir / "conda").read_text().rstrip().endswith('conda "$@"')


def test_activate_is_idempotent_and_never_grows_path(home, env_dir, fake_mm):
    environ = {"PATH": "/usr/bin:/bin", "XRUNNER_MICROMAMBA": str(fake_mm)}
    first = sandbox.activate(env_dir, home=home, environ=environ)
    again = sandbox.activate(env_dir, home=home, environ={**environ, "PATH": first["PATH"]})
    assert again == first


def test_activate_puts_the_shims_first_even_when_already_on_path(home, env_dir, fake_mm):
    bin_dir = env_dir.resolve() / "bin"
    environ = {"PATH": f"/usr/bin:{bin_dir}:/bin", "XRUNNER_MICROMAMBA": str(fake_mm)}
    assert sandbox.activate(env_dir, home=home, environ=environ)["PATH"] == f"{bin_dir}:/usr/bin:/bin"


def test_activate_installs_micromamba_once_when_missing(home, env_dir, monkeypatch, tmp_path):
    calls = []
    fake = make_fake_micromamba(tmp_path / "dl")

    def install(h, source=None):
        calls.append(h.path)
        target = h.path / "bin" / "micromamba-2.9.0" / "micromamba"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fake.read_bytes())
        target.chmod(0o755)
        return target

    monkeypatch.setattr(sandbox, "install_micromamba", install)
    monkeypatch.setattr(sandbox, "find_micromamba",
                        lambda h, environ=None: (_ for _ in ()).throw(sandbox.MicromambaMissing("no")))
    sandbox.activate(env_dir, home=home, environ={"PATH": "/usr/bin:/bin"})
    assert calls == [home.path]


def test_a_failed_micromamba_download_still_activates(home, env_dir, monkeypatch, caplog):
    monkeypatch.setattr(sandbox, "find_micromamba",
                        lambda h, environ=None: (_ for _ in ()).throw(sandbox.MicromambaMissing("no")))
    monkeypatch.setattr(sandbox, "install_micromamba",
                        lambda h, source=None: (_ for _ in ()).throw(sandbox.XcodonError("offline")))
    with caplog.at_level(logging.WARNING):
        settings = sandbox.activate(env_dir, home=home, environ={"PATH": "/usr/bin:/bin"})
    assert (env_dir / "bin" / "docker").exists() and (env_dir / "bin" / "conda").exists()
    assert settings["XRUNNER_ENV_DIR"] == str(env_dir.resolve())
    assert "micromamba" in caplog.text and "offline" in caplog.text


def test_activate_replaces_a_non_shim_file_in_its_own_bin(home, env_dir, fake_mm):
    bin_dir = env_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "conda").write_text("#!/bin/sh\necho stale\n")
    sandbox.activate(env_dir, home=home, environ={"PATH": "/bin", "XRUNNER_MICROMAMBA": str(fake_mm)})
    assert is_xrunner_shim(bin_dir / "conda"), "the folder is xrunner's own, so its contents are replaced"


def test_activate_refuses_a_symlinked_bin(home, env_dir, fake_mm, tmp_path):
    env_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (env_dir / "bin").symlink_to(elsewhere)
    with pytest.raises(sandbox.XcodonError, match="not a directory"):
        sandbox.activate(env_dir, home=home, environ={"PATH": "/bin", "XRUNNER_MICROMAMBA": str(fake_mm)})
    assert list(elsewhere.iterdir()) == []


def test_the_activated_shims_reach_xrunner(home, env_dir, fake_mm, tmp_path):
    settings = sandbox.activate(env_dir, home=home, environ={"PATH": "/usr/bin:/bin", "XRUNNER_MICROMAMBA": str(fake_mm)})
    env = {"PATH": settings["PATH"], "XRUNNER_ENV_DIR": settings["XRUNNER_ENV_DIR"], "HOME": str(tmp_path),
           "XCODON_RUNTIME_HOME": str(home.path), "XRUNNER_MICROMAMBA": str(fake_mm)}
    r = subprocess.run(["conda", "--version"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout == "conda 2.9.0 (micromamba via xrunner)\n", r.stderr
    r = subprocess.run(["docker", "image", "inspect", "nope/none:1"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "No such image" in r.stderr


def test_cli_prints_export_lines(home, env_dir, fake_mm, monkeypatch, capfd):
    monkeypatch.setenv("XRUNNER_MICROMAMBA", str(fake_mm))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli.main(["sandbox", "activate", str(env_dir)]) == 0
    out = capfd.readouterr().out.splitlines()
    bin_dir = env_dir.resolve() / "bin"
    assert out == [f"export PATH='{bin_dir}:/usr/bin:/bin'", f"export XRUNNER_ENV_DIR='{env_dir.resolve()}'"]


def test_cli_rejects_a_relative_env_dir(home, capfd):
    assert cli.main(["sandbox", "activate", "relative/.xrunner-env"]) == 125
    assert "absolute" in capfd.readouterr().err


def test_python_api_is_importable_without_the_cli():
    r = subprocess.run([sys.executable, "-c", "from xcodon_runtime.sandbox import activate; print(activate.__doc__ is not None)"],
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "True", r.stderr
