
import pytest

from xcodon_runtime.api import Runtime

pytestmark = pytest.mark.network


def test_pull_busybox_from_docker_hub_and_run(home, engine_name, tmp_path):
    rt = Runtime(home.path, engine=engine_name)
    img = rt.pull("busybox:latest")
    assert (img.rootfs / "bin" / "busybox").exists()
    assert rt.inspect("busybox") is not None
    out = tmp_path / "o"
    with open(out, "wb") as f:
        code = rt.run("busybox:latest", command=["sh", "-c", "echo from-hub; id -u"], rm=True, stdout=f)
    assert code == 0
    assert out.read_bytes() == b"from-hub\n0\n"


def test_pull_multi_arch_biocontainer_manifest(home):
    """quay.io serves a plain manifest with anonymous token auth; docker.io serves an index."""
    rt = Runtime(home.path)
    img = rt.pull("quay.io/biocontainers/samtools:1.20--h50ea8bc_0")
    assert (img.rootfs / "usr" / "local" / "bin" / "samtools").exists()
