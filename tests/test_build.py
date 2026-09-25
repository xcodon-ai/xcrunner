import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.build import expand_args, parse_command, parse_dockerfile, parse_env
from xcodon_runtime.errors import XcodonError


def test_parse_dockerfile_handles_comments_continuations_and_case():
    text = """# comment
FROM base:1  # not a comment in docker, kept as args
run echo a \\
    && echo b
COPY ["a b", "/dst/"]
ENV X=1 Y="two words"
"""
    ins = parse_dockerfile(text)
    assert [i.name for i in ins] == ["FROM", "RUN", "COPY", "ENV"]
    assert ins[1].args == "echo a     && echo b" or ins[1].args == "echo a && echo b"
    assert ins[2].args.startswith('["a b"')
    assert ins[3].line == 6


def test_helpers():
    assert expand_args("pip install $PKG ${VER:-1.0} ${X}", {"PKG": "numpy", "X": "x"}) == "pip install numpy 1.0 x"
    assert parse_command('["/bin/sh", "-c", "ls"]', ["/bin/sh", "-c"]) == ["/bin/sh", "-c", "ls"]
    assert parse_command("ls -la", ["/bin/sh", "-c"]) == ["/bin/sh", "-c", "ls -la"]
    assert parse_env('A=1 B="two words" C=x=y') == {"A": "1", "B": "two words", "C": "x=y"}
    assert parse_env("KEY some value here") == {"KEY": "some value here"}


@pytest.fixture
def rt(home, busybox_image, engine_name):
    return Runtime(home.path, engine=engine_name)


def test_build_full_subset(rt, tmp_path, home):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "tool.sh").write_text("#!/bin/sh\necho tool-ran $GREETING\n")
    (ctx / "data").mkdir()
    (ctx / "data" / "a.txt").write_text("A")
    (ctx / "Dockerfile").write_text("""
ARG GREETING=hello
FROM xcodon-test/busybox
ARG GREETING
ENV GREETING=$GREETING OTHER="two words"
WORKDIR /app
COPY tool.sh /usr/local/bin/tool
COPY data/*.txt ./
RUN chmod +x /usr/local/bin/tool && echo built > /app/built
LABEL maintainer="test"
EXPOSE 8080
CMD ["tool"]
""")
    lines = []
    img = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, out=lines.append)
    assert img.refs == ["docker.io/xcodon-test/built:latest"]
    cfg = img.config["config"]
    assert "GREETING=hi" in cfg["Env"] and "OTHER=two words" in cfg["Env"]
    assert cfg["WorkingDir"] == "/app" and cfg["Cmd"] == ["tool"] and cfg["Labels"]["maintainer"] == "test"
    assert (img.rootfs / "usr/local/bin/tool").exists() and (img.rootfs / "app/a.txt").read_text() == "A"
    assert any("EXPOSE" in l and "ignored" in l for l in lines)
    out = home.path / "out"
    with open(out, "wb") as f:
        assert rt.run("xcodon-test/built:latest", rm=True, stdout=f) == 0
    assert out.read_bytes() == b"tool-ran hi\n"
    # cache: a second identical build performs no RUN
    lines.clear()
    img2 = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, out=lines.append)
    assert img2.id == img.id
    assert sum("CACHED" in l for l in lines) >= 4
    # a changed RUN invalidates only from that step on
    (ctx / "Dockerfile").write_text((ctx / "Dockerfile").read_text().replace("echo built", "echo rebuilt"))
    img3 = rt.build(ctx, tags=["xcodon-test/built:latest"], build_args={"GREETING": "hi"}, no_cache=False)
    assert img3.id != img.id and (img3.rootfs / "app/built").read_text() == "rebuilt\n"


def test_build_failures(rt, tmp_path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("RUN echo before-from\n")
    with pytest.raises(XcodonError, match="FROM"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN exit 3\n")
    with pytest.raises(XcodonError, match="exit 3"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox AS stage\n")
    with pytest.raises(XcodonError, match="multi-stage"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY missing.txt /x\n")
    with pytest.raises(XcodonError, match="missing.txt"):
        rt.build(ctx)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY ../outside /x\n")
    with pytest.raises(XcodonError, match="context"):
        rt.build(ctx)
    assert rt.containers(all=True) == [], "no build containers left behind"
