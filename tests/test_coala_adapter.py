import asyncio

import pytest

from xcodon_runtime.coala_adapter import XcodonContainerManager


@pytest.fixture
def manager(home, busybox_image, engine_name):
    return XcodonContainerManager(home.path, engine=engine_name)


def test_matches_coala_runtime_interface():
    for method in ("ensure_image", "create_container", "start_container", "exec_command",
                   "get_logs", "remove_container", "cleanup_all"):
        assert asyncio.iscoroutinefunction(getattr(XcodonContainerManager, method))
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
        b = await manager.create_container("xcodon-test/busybox:latest")
        await manager.start_container(a)
        await manager.cleanup_all()
        assert manager.containers == {}
        assert manager.runtime.containers(all=True) == []

    asyncio.run(flow())
