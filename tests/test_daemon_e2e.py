"""End to end: import an image from the local docker daemon and run it."""

import subprocess

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.daemon import DaemonSource
from xcodon_runtime.imagestore import ImageStore

pytestmark = pytest.mark.docker

REF = "busybox:latest"


def _ensure_image() -> None:
    if subprocess.run(["docker", "image", "inspect", REF], capture_output=True).returncode == 0:
        return
    if subprocess.run(["docker", "pull", REF], capture_output=True).returncode != 0:
        pytest.skip(f"the docker daemon cannot provide {REF}")


def test_pull_from_the_daemon_and_run_it(home, engine_name):
    _ensure_image()
    image = ImageStore(home, sources=[DaemonSource(home)]).pull(REF)
    assert image.refs == ["docker.io/library/busybox:latest"]
    assert (image.rootfs / "bin" / "busybox").exists()

    rt = Runtime(home.path, engine=engine_name)
    c = rt.create(REF, command=["/bin/echo", "from-daemon"], pull="never")
    rt.start(c)
    try:
        result = rt.exec(c)
        assert result.code == 0, result.stderr
        assert result.stdout == b"from-daemon\n"
    finally:
        rt.stop(c)
        rt.remove(c)
