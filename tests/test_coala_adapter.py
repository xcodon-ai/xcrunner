import asyncio
import inspect

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.coala_adapter import XcodonContainerManager
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.keeper import KEEPER_LOG


@pytest.fixture
def manager(home, busybox_image, engine_name):
    return XcodonContainerManager(home.path, engine=engine_name)


def test_matches_coala_runtime_interface():
    for method in ("ensure_image", "create_container", "start_container", "exec_command",
                   "get_logs", "remove_container", "cleanup_all"):
        assert inspect.iscoroutinefunction(getattr(XcodonContainerManager, method))
    assert XcodonContainerManager.system_site_packages_writable is True


def test_lifecycle_like_coala_runtime(manager, tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "data.csv").write_text("a,b\n")
    out = tmp_path / "out"
    out.mkdir()

    async def flow():
        await manager.ensure_image("xcodon-test/busybox:latest")
        c = await manager.create_container(
            "xcodon-test/busybox:latest",
            command="tail -f /dev/null",
            volumes={str(inp): {"bind": "/input", "mode": "ro"}, str(out): {"bind": "/output", "mode": "rw"}},
            working_dir="/workspace",
            environment={"COALA": "1"},
        )
        await manager.start_container(c)
        code, so, se = await manager.exec_command(c, "cat /input/data.csv; echo $COALA; pwd")
        assert (code, so) == (0, b"a,b\n1\n/workspace\n"), se
        code, _, _ = await manager.exec_command(c, ["/bin/sh", "-c", "echo r > /output/result; exit 2"], workdir="/output")
        assert code == 2
        code, so, _ = await manager.exec_command(c, "echo $EXTRA", environment={"EXTRA": "yes"})
        assert so == b"yes\n"
        logs = await manager.get_logs(c)
        assert isinstance(logs, str)
        assert c.id in manager.containers
        await manager.remove_container(c)
        assert c.id not in manager.containers

    asyncio.run(flow())
    assert (out / "result").read_text() == "r\n"


def test_cleanup_all(manager):
    async def flow():
        a = await manager.create_container("xcodon-test/busybox:latest")
        await manager.create_container("xcodon-test/busybox:latest")
        await manager.start_container(a)
        await manager.cleanup_all()
        assert manager.containers == {}
        assert manager.runtime.containers(all=True) == []

    asyncio.run(flow())


def test_create_container_volume_without_bind_raises(manager):
    async def flow():
        with pytest.raises(XcodonError):
            await manager.create_container("xcodon-test/busybox:latest", volumes={"/tmp": {"mode": "ro"}})

    asyncio.run(flow())


def test_get_logs_ns_vs_proot(manager, engine_name):
    """The ns engine keeps a keeper log per container; the proot engine keeps none."""

    async def flow():
        c = await manager.create_container("xcodon-test/busybox:latest")
        await manager.start_container(c)
        log_path = c.dir / KEEPER_LOG
        if engine_name == "ns":
            assert log_path.exists()
            with open(log_path, "a") as f:
                f.write("line one\nline two\npid 4242\n")
            logs = await manager.get_logs(c)
            assert "pid 4242" in logs
            assert logs.splitlines()[-1] == "pid 4242"
            assert await manager.get_logs(c, tail=1) == "pid 4242"
            assert await manager.get_logs(c, tail=0) == ""
        else:
            assert not log_path.exists()
            assert await manager.get_logs(c) == ""
        await manager.remove_container(c)

    asyncio.run(flow())


def test_cleanup_all_continues_after_one_failure(manager, monkeypatch):
    async def flow():
        a = await manager.create_container("xcodon-test/busybox:latest")
        b = await manager.create_container("xcodon-test/busybox:latest")
        original_remove = Runtime.remove

        def flaky_remove(self, container, force=False):
            if container.id == a.id:
                raise XcodonError("boom: simulated removal failure")
            return original_remove(self, container, force=force)

        monkeypatch.setattr(Runtime, "remove", flaky_remove)

        await manager.cleanup_all()  # must not raise, even though removing `a` fails

        assert a.id in manager.containers
        assert b.id not in manager.containers

    asyncio.run(flow())


def test_returned_container_reloads_and_reports_status(home, busybox_image, engine_name):
    """coala-runtime's executor calls reload() and reads status after start."""
    mgr = XcodonContainerManager(home.path, engine=engine_name)

    async def flow():
        c = await mgr.create_container("xcodon-test/busybox:latest", command="sleep 100", working_dir="/workspace")
        assert hasattr(c, "reload") and hasattr(c, "status") and hasattr(c, "id")
        assert c.status == "created"
        await mgr.start_container(c)
        c.reload()  # sync, exactly as coala-runtime calls it
        assert c.status == "running", c.status
        code, out, _ = await mgr.exec_command(c, "echo hi")
        assert (code, out) == (0, b"hi\n")
        await mgr.remove_container(c)
        c2 = await mgr.create_container("xcodon-test/busybox:latest")
        await mgr.remove_container(c2)  # never started; still removable via the view

    asyncio.run(flow())


def test_env_dir_from_environment_persists_installs(home, busybox_image, engine_name, tmp_path, monkeypatch):
    env = tmp_path / "xcrunner-env"
    monkeypatch.setenv("XCRUNNER_ENV_DIR", str(env))
    mgr = XcodonContainerManager(home.path, engine=engine_name)

    async def flow():
        c = await mgr.create_container("xcodon-test/busybox:latest")
        await mgr.start_container(c)
        code, _, _ = await mgr.exec_command(c, "mkdir -p /opt/tool && echo ok > /opt/tool/marker")
        assert code == 0
        await mgr.remove_container(c)
        c2 = await mgr.create_container("xcodon-test/busybox:latest")
        await mgr.start_container(c2)
        code, out, _ = await mgr.exec_command(c2, "cat /opt/tool/marker")
        assert (code, out) == (0, b"ok\n")
        assert c2.container.env_dir == str(env)
        await mgr.remove_container(c2)

    asyncio.run(flow())
    assert (env / busybox_image.id / ("upper" if engine_name == "ns" else "rootfs")).is_dir()


def test_env_dir_is_validated_at_construction(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mgr = XcodonContainerManager(home.path, env_dir="~/envs/a")
    assert mgr.env_dir == str(tmp_path / "envs" / "a")
    with pytest.raises(XcodonError, match="absolute"):
        XcodonContainerManager(home.path, env_dir="relative/env")
    monkeypatch.setenv("XCRUNNER_ENV_DIR", "also/relative")
    with pytest.raises(XcodonError, match="XCRUNNER_ENV_DIR"):
        XcodonContainerManager(home.path)
    monkeypatch.setenv("XCRUNNER_ENV_DIR", "")
    assert XcodonContainerManager(home.path).env_dir is None


class _FakeImages:
    """Records what the adapter asked the store to do."""

    def __init__(self, present=()):
        self.present = set(present)
        self.pulled: list[str] = []
        self.tagged: list[tuple[str, str]] = []
        self.resolved: list[str] = []

    def get(self, ref):
        return object() if ref in self.present else None

    def pull(self, ref):
        self.pulled.append(ref)
        self.present.add(ref)
        return object()

    def tag(self, ref, new_ref):
        self.tagged.append((ref, new_ref))
        self.present.add(new_ref)
        return object()


class _FakeRuntime:
    def __init__(self, images):
        self.images = images

    def resolve_image(self, ref):
        self.images.resolved.append(ref)
        return self.images.pull(ref)


def _manager_with(images, registry_source=None):
    m = XcodonContainerManager.__new__(XcodonContainerManager)
    m.runtime = _FakeRuntime(images)
    m.containers = {}
    m.env_dir = None
    m.registry_source = registry_source
    return m


_SOURCES = {"coala-runtime-python:latest": "hubentu/coala-runtime-python:latest"}


def test_a_local_tag_is_pulled_from_its_published_source_and_tagged():
    """A bare executor tag resolves to docker.io/library/… and serves no image."""
    images = _FakeImages()
    m = _manager_with(images, registry_source=_SOURCES.get)
    asyncio.run(m.ensure_image("coala-runtime-python:latest"))
    assert images.pulled == ["hubentu/coala-runtime-python:latest"]
    assert images.tagged == [("hubentu/coala-runtime-python:latest", "coala-runtime-python:latest")]
    assert images.resolved == [], "the local name must never be fetched verbatim"


def test_an_image_already_in_the_store_is_left_alone():
    images = _FakeImages(present={"coala-runtime-python:latest"})
    m = _manager_with(images, registry_source=_SOURCES.get)
    asyncio.run(m.ensure_image("coala-runtime-python:latest"))
    assert images.pulled == [] and images.tagged == [] and images.resolved == []


def test_an_unmapped_image_keeps_the_plain_resolve():
    images = _FakeImages()
    m = _manager_with(images, registry_source=_SOURCES.get)
    asyncio.run(m.ensure_image("quay.io/biocontainers/samtools:1.19"))
    assert images.resolved == ["quay.io/biocontainers/samtools:1.19"]
    assert images.tagged == []


def test_without_a_mapping_behaviour_is_unchanged():
    images = _FakeImages()
    m = _manager_with(images)  # no registry_source: older callers
    asyncio.run(m.ensure_image("coala-runtime-python:latest"))
    assert images.resolved == ["coala-runtime-python:latest"]
    assert images.tagged == []


def test_a_broken_mapping_does_not_break_the_run():
    def boom(_ref):
        raise RuntimeError("mapping unavailable")

    images = _FakeImages()
    m = _manager_with(images, registry_source=boom)
    asyncio.run(m.ensure_image("coala-runtime-python:latest"))
    assert images.resolved == ["coala-runtime-python:latest"]


def test_the_constructor_accepts_a_registry_source():
    assert "registry_source" in inspect.signature(XcodonContainerManager.__init__).parameters


def test_describe_container_names_the_engine_and_folder(home, busybox_image, engine_name):
    import asyncio

    from xcodon_runtime import __version__
    from xcodon_runtime.coala_adapter import XcodonContainerManager

    mgr = XcodonContainerManager(home=home.path, engine=engine_name)
    c = asyncio.run(mgr.create_container("xcodon-test/busybox", command="true"))
    try:
        line = mgr.describe_container(c)
        assert line.startswith(f"xcrunner {__version__} · engine {engine_name} · container dir ")
        assert str(home.containers) in line
    finally:
        asyncio.run(mgr.remove_container(c))
