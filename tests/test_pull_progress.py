"""Docker-style progress for pulls: xcrunner pull, and pulls that run and create start."""

import io
import pytest

from tests.fake_registry import FakeRegistry, digest_of
from tests.test_registry import config_for, layer_bytes
from xcodon_runtime import cli
from xcodon_runtime.api import Runtime
from xcodon_runtime.imagestore import ImageStore
from xcodon_runtime.progress import PullProgress, human_size
from xcodon_runtime.registry import RegistryClient


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("n,text", [(0, "0B"), (999, "999B"), (1000, "1.00kB"), (12_340_000, "12.34MB"),
                                    (2_500_000_000, "2.50GB")])
def test_human_size(n, text):
    assert human_size(n) == text


def test_log_output_has_one_line_per_step():
    out = io.StringIO()
    p = PullProgress(out)
    p.start("ghcr.io/coala-info/coala-runtime-python:latest")
    d = "sha256:" + "ab" * 32
    p.layer(d, "Pulling fs layer")
    p.downloading(d, 500, 1000)
    p.layer(d, "Download complete")
    p.layer(d, "Extracting")
    p.layer(d, "Pull complete")
    p.finish("sha256:" + "cd" * 32, "Downloaded newer image for ghcr.io/coala-info/coala-runtime-python:latest")
    assert out.getvalue().splitlines() == [
        "latest: Pulling from coala-info/coala-runtime-python",
        "abababababab: Pulling fs layer",
        "abababababab: Download complete",
        "abababababab: Pull complete",
        "Digest: sha256:" + "cd" * 32,
        "Status: Downloaded newer image for ghcr.io/coala-info/coala-runtime-python:latest",
    ]


def test_a_terminal_gets_a_live_progress_bar():
    out = Tty()
    p = PullProgress(out)
    d = "sha256:" + "ab" * 32
    p.layer(d, "Pulling fs layer")
    p.downloading(d, 250_000, 1_000_000)
    p.layer(d, "Download complete")
    text = out.getvalue()
    assert "\r" in text and "[" in text and "250.00kB/1.00MB" in text
    assert text.endswith("abababababab: Download complete\n")


def _image(reg, name="lib/app", tag="v1"):
    layers = [layer_bytes("a", b"A" * 2000), layer_bytes("b", b"B")]
    reg.add_image(name, tag, config_for(layers), layers)
    return layers


def _blob_gets(reg, layers):
    wanted = {digest_of(x) for x in layers}
    return [r for r in reg.requests if r[0] == "api" and any(d in r[1] for d in wanted)]


def test_a_pull_reports_each_layer_and_a_rerun_says_already_exists(home):
    with FakeRegistry() as reg:
        layers = _image(reg)
        store = ImageStore(home, sources=[RegistryClient(home, scheme="http")])
        ref = f"{reg.host}/lib/app:v1"
        out = io.StringIO()
        img = store.pull(ref, progress=PullProgress(out))
        lines = out.getvalue().splitlines()
        assert lines[0] == "v1: Pulling from lib/app"
        for gz in layers:
            short = digest_of(gz)[7:19]
            assert [line for line in lines if line.startswith(short)] == [
                f"{short}: Pulling fs layer", f"{short}: Download complete", f"{short}: Pull complete"]
        assert lines[-2] == "Digest: " + digest_of(reg.manifests[("lib/app", "v1")][0])
        assert img.short_id
        assert lines[-1] == f"Status: Downloaded newer image for {reg.host}/lib/app:v1"
        before = len(_blob_gets(reg, layers))

        out = io.StringIO()
        store.pull(ref, progress=PullProgress(out))
        lines = out.getvalue().splitlines()
        for gz in layers:
            assert f"{digest_of(gz)[7:19]}: Already exists" in lines
        assert lines[-1] == f"Status: Image is up to date for {reg.host}/lib/app:v1"
        assert len(_blob_gets(reg, layers)) == before, "layers already in the store are not downloaded again"


def test_the_api_is_silent_without_progress(home, capsys):
    with FakeRegistry() as reg:
        _image(reg)
        ImageStore(home, sources=[RegistryClient(home, scheme="http")]).pull(f"{reg.host}/lib/app:v1")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.fixture
def http_registry(monkeypatch):
    """The fake registry speaks plain http; the CLI's client defaults to https."""
    real_init = RegistryClient.__init__

    def init(self, home, scheme="https", timeout=60):
        real_init(self, home, scheme="http", timeout=timeout)

    monkeypatch.setattr(RegistryClient, "__init__", init)


def test_cli_pull_shows_progress_on_stderr_and_keeps_stdout(home, http_registry, capsys):
    with FakeRegistry() as reg:
        _image(reg)
        ref = f"{reg.host}/lib/app:v1"
        assert cli.main(["pull", ref]) == 0
        captured = capsys.readouterr()
        assert captured.out.strip().startswith(f"{ref}: ")
        assert "v1: Pulling from lib/app" in captured.err and "Pull complete" in captured.err
        assert "Status: Downloaded newer image" in captured.err
        assert cli.main(["pull", "-q", ref]) == 0
        captured = capsys.readouterr()
        assert captured.err == "" and captured.out.strip().startswith(f"{ref}: ")


def test_run_that_pulls_says_the_image_was_not_found_locally(home, http_registry):
    with FakeRegistry() as reg:
        _image(reg)
        rt = Runtime(home.path)
        out = io.StringIO()
        rt.progress = PullProgress(out)
        rt.resolve_image(f"{reg.host}/lib/app:v1")
        text = out.getvalue()
        assert text.startswith(f"Unable to find image '{reg.host}/lib/app:v1' locally\n")
        assert "Pull complete" in text
