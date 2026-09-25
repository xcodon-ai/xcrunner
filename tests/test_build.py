import os
import stat

import pytest

from xcodon_runtime.api import Runtime
from xcodon_runtime.build import expand_args, parse_command, parse_dockerfile, parse_env
from xcodon_runtime.errors import XcodonError
from xcodon_runtime.spec import DEFAULT_PATH


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


def test_parse_dockerfile_skips_comment_inside_continuation():
    text = "RUN echo a \\\n    # a comment, not part of the command\n    && echo b\n"
    ins = parse_dockerfile(text)
    assert len(ins) == 1
    assert ins[0].name == "RUN"
    assert "comment" not in ins[0].args
    assert ins[0].args in ("echo a     && echo b", "echo a && echo b")


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
    (tmp_path / "outside").write_text("nope")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY ../outside /x\n")
    with pytest.raises(XcodonError, match="context"):
        rt.build(ctx)
    assert rt.containers(all=True) == [], "no build containers left behind"


def test_add_from_url_is_rejected(rt, tmp_path):
    ctx = tmp_path / "ctx-url"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nADD http://example.com/x /x\n")
    with pytest.raises(XcodonError, match="URL"):
        rt.build(ctx)


# -- RUN/CMD/ENTRYPOINT are verbatim; the shell (not xrunner) resolves $VARS --

def test_run_is_not_expanded_shell_handles_its_own_vars(rt, tmp_path):
    ctx = tmp_path / "ctx-run-verbatim"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN f=x; echo $f > /out\n")
    built = rt.build(ctx)
    assert (built.rootfs / "out").read_text() == "x\n"


def test_run_echo_home_uses_container_env_not_dockerfile_substitution(rt, tmp_path):
    ctx = tmp_path / "ctx-run-home"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo $HOME > /out\n")
    built = rt.build(ctx)
    # Premature (pre-fix) substitution treated $HOME as an unknown ARG and
    # replaced it with "", so the file would be empty. It must not be.
    assert (built.rootfs / "out").read_text().strip() != ""


def test_cmd_shell_form_is_stored_verbatim(rt, tmp_path):
    ctx = tmp_path / "ctx-cmd-verbatim"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCMD echo $HOME\n")
    built = rt.build(ctx)
    assert built.config["config"]["Cmd"] == ["/bin/sh", "-c", "echo $HOME"]


def test_run_cache_key_depends_on_arg_values_not_run_text(rt, tmp_path):
    ctx = tmp_path / "ctx-run-cache"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nARG V=1\nRUN echo $V > /out\n")
    rt.build(ctx, build_args={"V": "a"})
    lines2 = []
    rt.build(ctx, build_args={"V": "b"}, out=lines2.append)
    assert not any("CACHED" in l for l in lines2)


# -- COPY destination rules -------------------------------------------------

def test_copy_dot_dot_merges_context_into_workdir(rt, tmp_path):
    ctx = tmp_path / "ctx-dotdot"
    ctx.mkdir()
    (ctx / "a.txt").write_text("A")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nWORKDIR /app\nCOPY . .\n")
    built = rt.build(ctx)
    assert (built.rootfs / "app" / "a.txt").read_text() == "A"
    assert (built.rootfs / "app" / "Dockerfile").exists()


def test_copy_dir_source_merges_contents_into_dest(rt, tmp_path):
    ctx = tmp_path / "ctx-dirmerge"
    ctx.mkdir()
    (ctx / "data").mkdir()
    (ctx / "data" / "a.txt").write_text("A")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nWORKDIR /app\nCOPY data /app/\n")
    built = rt.build(ctx)
    assert (built.rootfs / "app" / "a.txt").read_text() == "A"
    assert not (built.rootfs / "app" / "data").exists()


def test_copy_file_into_existing_dir_without_trailing_slash(rt, tmp_path):
    ctx = tmp_path / "ctx-nodirslash"
    ctx.mkdir()
    (ctx / "foo.sh").write_text("#!/bin/sh\necho hi\n")
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/busybox\nWORKDIR /usr/local/bin\nCOPY foo.sh /usr/local/bin\n"
    )
    built = rt.build(ctx)
    assert (built.rootfs / "usr/local/bin/foo.sh").read_text() == "#!/bin/sh\necho hi\n"
    assert (built.rootfs / "usr/local/bin").is_dir()


def test_copy_multiple_sources_requires_directory_dest(rt, tmp_path):
    ctx = tmp_path / "ctx-multisrc"
    ctx.mkdir()
    (ctx / "a").write_text("A")
    (ctx / "b").write_text("B")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY a b /single\n")
    with pytest.raises(XcodonError, match="directory"):
        rt.build(ctx)


def test_copy_preserves_existing_directory_mode(rt, tmp_path, home, busybox_rootfs):
    from tests.conftest import pack_rootfs_as_image

    # 0o750 (no group/other write, no setuid/setgid/sticky) survives the
    # image-import tar filter unchanged, unlike 1777 -- see extract_layer's
    # docstring: "Setuid, setgid, sticky, and group/other write bits are
    # dropped by the filter." That filter is a deliberate, unrelated safety
    # measure in the pull/import path, not something this fix touches; 0o750
    # still differs from the 0o755 a freshly created directory would default
    # to, so it is a real before/after check on COPY's own behavior.
    os.chmod(busybox_rootfs / "tmp", 0o750)
    pack_rootfs_as_image(home, busybox_rootfs, "xcodon-test/stickytmp:latest")
    ctx = tmp_path / "ctx-stickytmp"
    ctx.mkdir()
    (ctx / "f").write_text("hi")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/stickytmp\nCOPY f /tmp/f\n")
    built = rt.build(ctx)
    mode = stat.S_IMODE(os.lstat(built.rootfs / "tmp").st_mode)
    assert mode == 0o750
    assert (built.rootfs / "tmp" / "f").read_text() == "hi"


# -- variable scope: ARG/ENV precedence and pre/post-FROM scoping ----------

def test_env_expansion_sees_current_image_env(rt, tmp_path):
    ctx = tmp_path / "ctx-env-scope"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nENV PATH=/opt/x/bin:$PATH\n")
    built = rt.build(ctx)
    assert f"PATH=/opt/x/bin:{DEFAULT_PATH}" in built.config["config"]["Env"]


def test_env_overrides_arg_of_same_name_in_run(rt, tmp_path):
    ctx = tmp_path / "ctx-env-wins"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/busybox\nARG X=1\nENV X=2\nRUN echo $X > /out\n"
    )
    built = rt.build(ctx)
    assert (built.rootfs / "out").read_text() == "2\n"


def test_arg_without_default_is_unset_not_empty(rt, tmp_path):
    ctx = tmp_path / "ctx-arg-unset"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/busybox\nARG UNSET_VAR\nARG SET_VAR=hello\nRUN env > /out\n"
    )
    built = rt.build(ctx)
    out = (built.rootfs / "out").read_text()
    assert "UNSET_VAR" not in out
    assert "SET_VAR=hello" in out


def test_arg_scoping_across_from(rt, tmp_path):
    ctx = tmp_path / "ctx-arg-scope"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "ARG PRE=42\n"
        "FROM xcodon-test/busybox\n"
        "RUN env > /before\n"
        "ARG PRE\n"
        "RUN env > /after\n"
    )
    built = rt.build(ctx)
    assert "PRE" not in (built.rootfs / "before").read_text()
    assert "PRE=42" in (built.rootfs / "after").read_text()


def test_arg_default_strips_quotes(rt, tmp_path):
    ctx = tmp_path / "ctx-arg-quotes"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        'FROM xcodon-test/busybox\nARG GREETING="a b"\nLABEL greeting="$GREETING"\n'
    )
    built = rt.build(ctx)
    assert built.config["config"]["Labels"]["greeting"] == "a b"


# -- malformed input: XcodonError, never a raw exception --------------------

def test_copy_bad_json_raises_xcodon_error(rt, tmp_path):
    ctx = tmp_path / "ctx-badjson"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text('FROM xcodon-test/busybox\nCOPY ["a"\n')
    with pytest.raises(XcodonError):
        rt.build(ctx)


def test_copy_single_token_raises_xcodon_error(rt, tmp_path):
    ctx = tmp_path / "ctx-onetoken"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY onlyonepath\n")
    with pytest.raises(XcodonError):
        rt.build(ctx)


# -- COPY/ADD flags ----------------------------------------------------------

def test_copy_from_flag_is_multi_stage_error(rt, tmp_path):
    ctx = tmp_path / "ctx-copyfrom"
    ctx.mkdir()
    (ctx / "f").write_text("x")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY --from=builder f /f\n")
    with pytest.raises(XcodonError, match="multi-stage"):
        rt.build(ctx)


def test_copy_chown_chmod_flags_are_ignored_with_warning(rt, tmp_path):
    ctx = tmp_path / "ctx-chownchmod"
    ctx.mkdir()
    (ctx / "f").write_text("x")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY --chown=1000:1000 --chmod=755 f /f\n")
    lines = []
    built = rt.build(ctx, out=lines.append)
    assert (built.rootfs / "f").read_text() == "x"
    assert any("ignored" in l and ("chown" in l or "chmod" in l) for l in lines)


# -- SHELL persistence --------------------------------------------------------

def test_shell_instruction_is_recorded_and_busts_cache(rt, tmp_path):
    ctx = tmp_path / "ctx-shell"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text('FROM xcodon-test/busybox\nSHELL ["/bin/sh", "-c"]\n')
    built = rt.build(ctx)
    assert built.config["config"]["Shell"] == ["/bin/sh", "-c"]
    assert built.id != rt.resolve_image("xcodon-test/busybox").id


# -- FROM rules: no multi-stage, --platform accepted -------------------------

def test_second_from_is_multi_stage_error(rt, tmp_path):
    ctx = tmp_path / "ctx-secondfrom"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nFROM xcodon-test/busybox\n")
    with pytest.raises(XcodonError, match="multi-stage"):
        rt.build(ctx)


def test_from_platform_flag_is_accepted_and_ignored(rt, tmp_path):
    ctx = tmp_path / "ctx-platform"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM --platform=linux/amd64 xcodon-test/busybox\n")
    lines = []
    built = rt.build(ctx, out=lines.append)
    assert built.id == rt.resolve_image("xcodon-test/busybox").id
    assert any("platform" in l and "ignored" in l for l in lines)


# -- instruction validation --------------------------------------------------

def test_unknown_instruction_fails_before_any_step_runs(rt, tmp_path):
    ctx = tmp_path / "ctx-unknown"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nFROBNICATE y\nRUN echo should-not-run\n")
    with pytest.raises(XcodonError, match="line 2"):
        rt.build(ctx)
    assert rt.containers(all=True) == []
