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


# -- Final review ----------------------------------------------------------------


def test_a_fifo_made_in_the_container_never_hangs_the_record(home, pyimage, engine_name, project):
    from tests.test_pkgscan import _bounded, _release

    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    script = (f"mkdir -p /{SP}/f-1.dist-info /opt/conda/conda-meta && mkfifo /{SP}/f-1.dist-info/METADATA && "
              f"mkfifo /opt/conda/conda-meta/x-1.json; exit 7")
    layer = env_dir / pyimage.id / ("upper" if engine_name == "ns" else "rootfs")
    fifos = [layer / SP / "f-1.dist-info" / "METADATA", layer / "opt/conda/conda-meta/x-1.json"]
    try:
        code = _bounded(lambda: rt.run("xcodon-test/py:latest", command=["/bin/sh", "-c", script], rm=True,
                                       env_dir=env_dir), timeout=60)
        assert code == 7
        assert all(f.exists() for f in fifos), "the container made real FIFOs in its layer"
        _bounded(lambda: envrecord.record_all(env_dir, rt.images), timeout=30)
    finally:
        _release(fifos)
    assert envrecord.load(env_dir)["layers"][pyimage.id]["packages"] == []


def test_a_built_image_lists_its_built_in_packages(home, pyimage, engine_name, project, tmp_path):
    env_dir = project / ".xrunner-env"
    rt = Runtime(home.path, engine=engine_name)
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    d1, d2 = f"/{SP}/built-1.0.dist-info", f"/{SP}/two-2.0.dist-info"
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/py:latest\n"
        f"RUN mkdir -p {d1} && echo 'Name: built' > {d1}/METADATA && echo 'Version: 1.0' >> {d1}/METADATA\n"
        f"RUN mkdir -p {d2} && echo 'Name: two' > {d2}/METADATA && echo 'Version: 2.0' >> {d2}/METADATA\n")
    built = rt.build(ctx, tags=["xcodon-test/built:1"])
    c = rt.create("xcodon-test/built:1", env_dir=env_dir)  # create only; never started
    rt.remove(c, force=True)
    e = envrecord.load(env_dir)["images"]["xcodon-test/built:1"]
    assert e["id"] == "sha256:" + built.id and e["source"] == "build"
    assert e["base"] == "sha256:" + pyimage.id and e["parent"] != e["base"]
    assert {(p["name"], p["change"], p["version"]) for p in e["packages"]} == {
        ("built", "added", "1.0"), ("two", "added", "2.0")}
    assert e["platform"] == "linux/amd64" and e["package_counts"] == {"pip": 3}
    listed = json.loads((project / e["packages_file"]).read_text())["packages"]
    assert sorted(p["name"] for p in listed) == ["built", "old", "two"]
    assert cli.main(["env", "record", "--env-dir", str(env_dir)]) == 0
    assert envrecord.load(env_dir)["images"]["xcodon-test/built:1"]["id"] == "sha256:" + built.id
    text = envrecord.show(env_dir)
    assert "built-in package changes:" in text and "built  added 1.0" in text and "Dockerfile on record" in text
    assert f"built from {pyimage.id[:12]}" in text and "platform linux/amd64; packages: pip 3" in text


def test_cli_env_errors_exit_125_without_a_traceback(home, project, tmp_path, capfd):
    missing = tmp_path / "no-such" / ".xrunner-env"
    assert cli.main(["env", "record", "--env-dir", str(missing)]) == 125
    err = capfd.readouterr().err
    assert "Traceback" not in err and str(missing) in err
    env_dir = project / ".xrunner-env"
    (env_dir / "environment.json").mkdir()  # a directory where the file should be
    assert cli.main(["env", "show", "--json", "--env-dir", str(env_dir)]) == 125
    assert "Traceback" not in capfd.readouterr().err


def test_cli_env_show_json_prints_the_file_itself(home, project, capfd):
    env_dir = project / ".xrunner-env"
    assert cli.main(["env", "show", "--json", "--env-dir", str(env_dir)]) == 0
    assert json.loads(capfd.readouterr().out) == envrecord.empty()
    raw = '{"version": 1, "images": {}, "layers": {"ab": 1}, "conda": {}, "extra": [1,2]}\n'
    (env_dir / "environment.json").write_text(raw)
    assert cli.main(["env", "show", "--json", "--env-dir", str(env_dir)]) == 0
    assert capfd.readouterr().out == raw
    assert cli.main(["env", "show", "--env-dir", str(env_dir)]) == 0  # non-dict layer entry: no crash
