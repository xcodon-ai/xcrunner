import io
import json
from pathlib import Path

import pytest

from tests.conftest import pack_rootfs_as_image
from tests.fake_micromamba import make_fake_micromamba
from xcodon_runtime import cli, envrecord
from xcodon_runtime.api import Runtime
from xcodon_runtime.condashim import conda_main

SP = "usr/lib/python3/site-packages"


@pytest.fixture
def pyimage(home, busybox_rootfs):
    meta = busybox_rootfs / SP / "old-1.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text("Name: old\nVersion: 1.0\n")
    return pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/py:latest")


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".xrunner-env").mkdir(parents=True)
    (proj / ".xrunner-env").chmod(0o755)
    return proj


def test_container_stop_records_real_layer_changes(home, pyimage, engine_name, project):
    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    c = rt.create("xcodon-test/py:latest", env_dir=env_dir)
    try:
        rt.start(c)
        script = (f"mkdir -p /{SP}/new-2.0.dist-info && echo 'Name: new' > /{SP}/new-2.0.dist-info/METADATA && "
                  f"echo 'Version: 2.0' >> /{SP}/new-2.0.dist-info/METADATA && rm -rf /{SP}/old-1.0.dist-info")
        assert rt.exec(c, ["/bin/sh", "-c", script]).code == 0
        rt.stop(c)
    finally:
        rt.remove(c, force=True)
    doc = envrecord.load(env_dir)
    assert doc["images"]["xcodon-test/py:latest"]["id"] == "sha256:" + pyimage.id
    pkgs = {(p["name"], p["change"]) for p in doc["layers"][pyimage.id]["packages"]}
    assert pkgs == {("new", "added"), ("old", "removed")}


def test_run_rm_records_too(home, pyimage, engine_name, project):
    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    script = (f"mkdir -p /{SP}/x-1.dist-info && echo 'Name: x' > /{SP}/x-1.dist-info/METADATA && "
              f"echo 'Version: 1' >> /{SP}/x-1.dist-info/METADATA")  # the busybox fixture has no printf applet
    assert rt.run("xcodon-test/py:latest", command=["/bin/sh", "-c", script], rm=True, env_dir=env_dir) == 0
    assert ("x", "added") in {(p["name"], p["change"]) for p in envrecord.load(env_dir)["layers"][pyimage.id]["packages"]}


def test_record_failures_never_break_a_run(home, pyimage, engine_name, project, monkeypatch, caplog):
    env_dir = project / ".xrunner-env"
    monkeypatch.setattr(envrecord, "record_layer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rt = Runtime(home.path, engine=engine_name)
    assert rt.run("xcodon-test/py:latest", command=["/bin/true"], rm=True, env_dir=env_dir) == 0
    assert "could not update the environment record" in caplog.text


def test_conda_changes_update_the_record(home, project, tmp_path):
    mm = make_fake_micromamba(tmp_path / "fakebin")
    environ = {"PATH": "/usr/bin:/bin", "XRUNNER_MICROMAMBA": str(mm), "HOME": str(tmp_path / "u")}
    assert conda_main(["create", "-n", "tools", "x"], home, cwd=project, environ=environ, err=io.StringIO()) == 0
    doc = envrecord.load(project / ".xrunner-env")
    assert doc["conda"]["name:tools"]["explicit"] == ".xrunner-env/conda/envs/tools/conda-explicit.txt"
    assert doc["conda"]["name:tools"]["packages"] == 1


def test_cli_env_record_and_show(home, pyimage, project, monkeypatch, capfd):
    env_dir = project / ".xrunner-env"
    (env_dir / pyimage.id / "upper").mkdir(parents=True)
    (env_dir / pyimage.id / "image.json").write_text(json.dumps(
        {"image_ref": "xcodon-test/py:latest", "image_id": pyimage.id, "engine": "ns", "first_used": "t"}))
    monkeypatch.chdir(project / ".xrunner-env")
    assert cli.main(["env", "record"]) == 0
    assert capfd.readouterr().out.strip() == str(env_dir / "environment.json")
    assert cli.main(["env", "show"]) == 0
    assert "xcodon-test/py:latest" in capfd.readouterr().out
    assert cli.main(["env", "show", "--json", "--env-dir", str(env_dir)]) == 0
    assert json.loads(capfd.readouterr().out)["version"] == 1
    monkeypatch.chdir(Path("/"))
    monkeypatch.delenv("XRUNNER_ENV_DIR", raising=False)
    assert cli.main(["env", "show"]) == 125
    assert "--env-dir" in capfd.readouterr().err
