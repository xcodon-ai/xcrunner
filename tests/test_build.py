import os
import shutil
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


# -- RUN/CMD/ENTRYPOINT are verbatim; the shell (not xcrunner) resolves $VARS --

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


# -- round 2: directory merges must not crash on overlapping names (A) ------

def test_copy_merges_directories_with_overlapping_names(rt, tmp_path):
    ctx = tmp_path / "ctx-mergedirs"
    ctx.mkdir()
    (ctx / "src" / "data").mkdir(parents=True)
    (ctx / "src" / "data" / "a.txt").write_text("A")
    (ctx / "src" / "data" / "common.txt").write_text("first")
    (ctx / "tests" / "data").mkdir(parents=True)
    (ctx / "tests" / "data" / "b.txt").write_text("B")
    (ctx / "tests" / "data" / "common.txt").write_text("second")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY src/ tests/ /app/\n")
    built = rt.build(ctx)
    assert (built.rootfs / "app/data/a.txt").read_text() == "A"
    assert (built.rootfs / "app/data/b.txt").read_text() == "B"
    # a later source's file replaces an earlier one of the same name
    assert (built.rootfs / "app/data/common.txt").read_text() == "second"


# -- round 2: guest destinations are normalized and clamped at / (B) --------

def test_copy_dest_dotdot_is_clamped_to_root(rt, tmp_path, home):
    ctx = tmp_path / "ctx-dotdot-dest"
    ctx.mkdir()
    (ctx / "f").write_text("x")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nWORKDIR /workspace\nCOPY f ../../../x\n")
    built = rt.build(ctx)
    assert (built.rootfs / "x").read_text() == "x"
    # nothing escaped onto the host: the runtime home itself must be untouched
    assert not (home.path / "x").exists()


# -- round 2: destinations that are symlinks in the image (C) --------------

def test_copy_into_symlinked_bin_preserves_the_link(rt, tmp_path, home):
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / "mergedusr")
    (root / "usr").mkdir()
    shutil.move(str(root / "bin"), str(root / "usr" / "bin"))
    (root / "bin").symlink_to("usr/bin")
    pack_rootfs_as_image(home, root, "xcodon-test/mergedusr:latest")

    ctx = tmp_path / "ctx-mergedusr"
    ctx.mkdir()
    (ctx / "tool").write_text("hi")
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/mergedusr\nCOPY tool /bin/\nRUN echo via-symlink > /out\n"
    )
    built = rt.build(ctx)
    assert (built.rootfs / "usr" / "bin" / "tool").read_text() == "hi"
    assert built.rootfs.joinpath("bin").is_symlink()
    assert os.readlink(built.rootfs / "bin") == "usr/bin"
    assert (built.rootfs / "out").read_text() == "via-symlink\n"


def test_copy_dest_through_absolute_symlink_stays_inside_rootfs(rt, tmp_path, home):
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / "abssym")
    (root / "run").mkdir()
    (root / "var").mkdir()
    (root / "var" / "run").symlink_to("/run")  # absolute target
    pack_rootfs_as_image(home, root, "xcodon-test/abssym:latest")

    ctx = tmp_path / "ctx-abssym"
    ctx.mkdir()
    (ctx / "f").write_text("hi")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/abssym\nCOPY f /var/run/f\n")
    built = rt.build(ctx)
    # An absolute symlink target is re-rooted at the image's own rootfs, so
    # this must land at <rootfs>/run/f, never the host's real /run.
    assert (built.rootfs / "run" / "f").read_text() == "hi"


# -- round 2: ENV/LABEL/COPY split into words before expanding (E) ----------

def test_env_word_split_before_expand_keeps_multiword_value_together(rt, tmp_path):
    ctx = tmp_path / "ctx-envsplit"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text('FROM xcodon-test/busybox\nARG P="a b"\nENV P=$P\n')
    built = rt.build(ctx)
    assert "P=a b" in built.config["config"]["Env"]


def test_env_value_with_apostrophe_in_double_quotes_does_not_raise(rt, tmp_path):
    ctx = tmp_path / "ctx-apostrophe"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nENV GREETING=\"it's fine\"\n")
    built = rt.build(ctx)
    assert "GREETING=it's fine" in built.config["config"]["Env"]


def test_copy_dest_expands_arg_after_split(rt, tmp_path):
    ctx = tmp_path / "ctx-copyexpand"
    ctx.mkdir()
    (ctx / "f").write_text("hi")
    (ctx / "Dockerfile").write_text('FROM xcodon-test/busybox\nARG D=/dest\nCOPY f $D/f\n')
    built = rt.build(ctx)
    assert (built.rootfs / "dest" / "f").read_text() == "hi"


# -- round 2: source directory mode set after contents are placed (F) ------

def test_copy_dir_source_with_read_only_mode_and_file(rt, tmp_path):
    ctx = tmp_path / "ctx-ro-dir"
    ctx.mkdir()
    src = ctx / "ro"
    src.mkdir()
    (src / "f").write_text("data")
    os.chmod(src, 0o555)
    try:
        (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY ro /dest/\n")
        # The regression: chmod'ing the layer's "dest" directory to 0o555
        # *before* copying "f" into it made the copy itself fail with
        # PermissionError. Deferring the chmod until after the content is in
        # place is the fix; this must complete without raising.
        built = rt.build(ctx)
        assert (built.rootfs / "dest" / "f").read_text() == "data"
        # build_rootfs's own layer-apply step ORs in 0o700 on every directory
        # it applies (flatten.py's _apply_dir, unrelated to this fix, and
        # never restored back down), so 0o555 survives as 0o555 | 0o700 =
        # 0o755 in the finished image -- not bit-for-bit 0o555.
        assert stat.S_IMODE(os.lstat(built.rootfs / "dest").st_mode) == 0o755
    finally:
        os.chmod(src, 0o755)


# -- round 2: single quotes and ${X:+word} (H) ------------------------------

def test_arg_single_quotes_keep_dollar_literal(rt, tmp_path):
    ctx = tmp_path / "ctx-singlequote"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nARG X='$HOME'\nLABEL literal=$X\n")
    built = rt.build(ctx)
    assert built.config["config"]["Labels"]["literal"] == "$HOME"


def test_colon_plus_expansion(rt, tmp_path):
    ctx = tmp_path / "ctx-colonplus"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/busybox\nARG X=set\nLABEL present=${X:+yes}\nLABEL absent=${Y:+yes}\n"
    )
    built = rt.build(ctx)
    labels = built.config["config"]["Labels"]
    assert labels["present"] == "yes"
    assert labels["absent"] == ""


# -- round 3, item 1: a stale mode fixup must never chmod a symlink's host target --

def test_copy_stale_fixup_never_chmods_a_symlinks_host_target(rt, tmp_path):
    ctx = tmp_path / "ctx-hostwrite"
    ctx.mkdir()
    hostdir = tmp_path / "hostdir"
    hostdir.mkdir()
    os.chmod(hostdir, 0o700)
    (ctx / "a3" / "q").mkdir(parents=True)
    os.chmod(ctx / "a3" / "q", 0o777)
    (ctx / "b3").mkdir()
    (ctx / "b3" / "q").symlink_to(hostdir)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY a3/ b3/ /x/\n")
    built = rt.build(ctx)
    assert (built.rootfs / "x" / "q").is_symlink()
    # a3/q (0o777) was queued for a chmod fixup before b3/q's symlink replaced
    # it at the same path; that fixup must have been dropped, not applied to
    # whatever the symlink points at on the host.
    assert stat.S_IMODE(os.stat(hostdir).st_mode) == 0o700


# -- round 3, item 2: a directory source must replace a symlink left by an earlier one --

def test_copy_directory_replaces_symlink_left_by_earlier_source(rt, tmp_path):
    ctx = tmp_path / "ctx-symlinkmerge"
    ctx.mkdir()
    hostdir = tmp_path / "hostdir2"
    hostdir.mkdir()
    (ctx / "b3").mkdir()
    (ctx / "b3" / "q").symlink_to(hostdir)
    (ctx / "a3" / "q").mkdir(parents=True)
    (ctx / "a3" / "q" / "file").write_text("content")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY b3/ a3/ /x/\n")
    built = rt.build(ctx)
    assert not (built.rootfs / "x" / "q").is_symlink()
    assert (built.rootfs / "x" / "q" / "file").read_text() == "content"
    assert list(hostdir.iterdir()) == [], "nothing must ever be written into the link's target"


# -- round 3, item 3: a later file/link must replace an earlier directory, even nested --

def test_copy_file_replaces_earlier_directory_with_nested_contents(rt, tmp_path):
    ctx = tmp_path / "ctx-crashA1"
    ctx.mkdir()
    (ctx / "a" / "y" / "nested").mkdir(parents=True)
    (ctx / "a" / "y" / "nested" / "z").write_text("z")
    (ctx / "y").write_text("file-y")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY a/ y /x/\n")
    built = rt.build(ctx)
    assert (built.rootfs / "x" / "y").read_text() == "file-y"


def test_copy_file_replaces_earlier_directory_with_nested_contents_second_case(rt, tmp_path):
    ctx = tmp_path / "ctx-crashA2"
    ctx.mkdir()
    (ctx / "a2" / "q" / "nested").mkdir(parents=True)
    (ctx / "a2" / "q" / "nested" / "z").write_text("z")
    (ctx / "b2").mkdir()
    (ctx / "b2" / "q").write_text("file-q")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY a2/ b2/ /x/\n")
    built = rt.build(ctx)
    assert (built.rootfs / "x" / "q").read_text() == "file-q"


# -- round 3, item 4: a single-pass word lexer for ARG/ENV/LABEL/COPY/WORKDIR/USER/FROM --

def test_lexer_single_quote_and_var_in_same_env(rt, tmp_path):
    ctx = tmp_path / "ctx-lex1"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nARG y=Y_VALUE\nENV A='$x' B=$y\n")
    built = rt.build(ctx)
    env = built.config["config"]["Env"]
    assert "A=$x" in env
    assert "B=Y_VALUE" in env


def test_lexer_backslash_dollar_escape(rt, tmp_path):
    ctx = tmp_path / "ctx-lex2"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nENV A=\\$x\n")
    built = rt.build(ctx)
    assert "A=$x" in built.config["config"]["Env"]


def test_lexer_double_quote_expands_and_keeps_apostrophe(rt, tmp_path):
    ctx = tmp_path / "ctx-lex3"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        'FROM xcodon-test/busybox\nARG HOME=/home/test\nENV MSG="it\'s $HOME"\n'
    )
    built = rt.build(ctx)
    assert "MSG=it's /home/test" in built.config["config"]["Env"]


def test_lexer_arg_double_quote_expands_var(rt, tmp_path):
    ctx = tmp_path / "ctx-lex4"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(
        'FROM xcodon-test/busybox\nARG X=world\nARG M="it\'s $X"\nLABEL m=$M\n'
    )
    built = rt.build(ctx)
    assert built.config["config"]["Labels"]["m"] == "it's world"


def test_lexer_label_apostrophe_in_double_quotes(rt, tmp_path):
    ctx = tmp_path / "ctx-lex5"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nLABEL d=\"don't\"\n")
    built = rt.build(ctx)
    assert built.config["config"]["Labels"]["d"] == "don't"


def test_lexer_unclosed_quote_raises(rt, tmp_path):
    ctx = tmp_path / "ctx-lex6"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text('FROM xcodon-test/busybox\nENV X="abc\n')
    with pytest.raises(XcodonError):
        rt.build(ctx)


# -- round 3, item 5: nested COPY entries must also resolve through rootfs symlinks --

def test_copy_dir_source_nested_entries_resolve_through_rootfs_symlinks(rt, tmp_path, home):
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / "mergedusr2")
    (root / "usr").mkdir()
    shutil.move(str(root / "bin"), str(root / "usr" / "bin"))
    (root / "bin").symlink_to("usr/bin")
    pack_rootfs_as_image(home, root, "xcodon-test/mergedusr2:latest")

    ctx = tmp_path / "ctx-mergedusr2"
    ctx.mkdir()
    (ctx / "rootfs" / "bin").mkdir(parents=True)
    (ctx / "rootfs" / "bin" / "tool").write_text("hi")
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/mergedusr2\nCOPY rootfs/ /\nRUN echo via-nested-symlink > /out\n"
    )
    built = rt.build(ctx)
    assert (built.rootfs / "usr" / "bin" / "tool").read_text() == "hi"
    assert built.rootfs.joinpath("bin").is_symlink()
    assert (built.rootfs / "out").read_text() == "via-nested-symlink\n"


# -- round 3, item 6: a non-directory path component raises a clear error ----

def test_copy_dest_through_non_directory_component_raises(rt, tmp_path):
    ctx = tmp_path / "ctx-notadir"
    ctx.mkdir()
    (ctx / "f").write_text("x")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY f /etc/passwd/x\n")
    with pytest.raises(XcodonError, match="directory"):
        rt.build(ctx)


# -- round 4, item 1: a non-directory entry keeps its own name --------------
#
# A directory entry of a COPY source resolves through the image's symlinks
# (so merged-/usr ``COPY rootfs/ /`` still works). A file or symlink entry
# resolves only its parent; whatever is at its own name is replaced, the way
# docker and overlay layering do it.

def _merged_usr_image(home, tmp_path, name):
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / name)
    (root / "usr").mkdir()
    shutil.move(str(root / "bin"), str(root / "usr" / "bin"))
    (root / "bin").symlink_to("usr/bin")
    pack_rootfs_as_image(home, root, f"xcodon-test/{name}:latest")
    return f"xcodon-test/{name}"


def test_copy_symlink_entry_replaces_image_link_not_its_target_dir(rt, tmp_path, home):
    # N9: /lib64 -> lib in both the image and the context.
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / "libimg")
    (root / "lib").mkdir()
    (root / "lib" / "libc.so").write_text("libc")
    (root / "lib64").symlink_to("lib")
    pack_rootfs_as_image(home, root, "xcodon-test/libimg:latest")

    ctx = tmp_path / "ctx-n9"
    (ctx / "rootfs" / "lib").mkdir(parents=True)
    (ctx / "rootfs" / "lib" / "libx.so").write_text("libx")
    (ctx / "rootfs" / "lib64").symlink_to("lib")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/libimg\nCOPY rootfs/ /\n")
    built = rt.build(ctx)
    lib = built.rootfs / "lib"
    assert not lib.is_symlink() and lib.is_dir()
    assert (lib / "libc.so").read_text() == "libc"
    assert (lib / "libx.so").read_text() == "libx"
    assert os.readlink(built.rootfs / "lib64") == "lib"


def test_copy_symlink_entry_on_merged_usr_keeps_usr_bin(rt, tmp_path, home):
    # N10: a context holding only rootfs/bin -> usr/bin must not turn
    # /usr/bin into a link to itself.
    ref = _merged_usr_image(home, tmp_path, "mergedusr4")
    ctx = tmp_path / "ctx-n10"
    (ctx / "rootfs").mkdir(parents=True)
    (ctx / "rootfs" / "bin").symlink_to("usr/bin")
    (ctx / "Dockerfile").write_text(f"FROM {ref}\nCOPY rootfs/ /\nRUN echo still-runs > /out\n")
    built = rt.build(ctx)
    assert os.readlink(built.rootfs / "bin") == "usr/bin"
    usr_bin = built.rootfs / "usr" / "bin"
    assert not usr_bin.is_symlink() and usr_bin.is_dir()
    assert (usr_bin / "busybox").is_file()
    assert (built.rootfs / "out").read_text() == "still-runs\n"


def _link_image(home, tmp_path, name):
    from tests.conftest import build_busybox_rootfs, pack_rootfs_as_image

    root = build_busybox_rootfs(tmp_path / name)
    (root / "usr" / "share" / "zoneinfo").mkdir(parents=True)
    (root / "usr" / "share" / "zoneinfo" / "UTC").write_text("utc-data")
    (root / "etc" / "localtime").symlink_to("../usr/share/zoneinfo/UTC")
    (root / "opt").mkdir()
    (root / "opt" / "app").symlink_to("/nonexist")
    pack_rootfs_as_image(home, root, f"xcodon-test/{name}:latest")
    return f"xcodon-test/{name}"


def test_copy_file_entry_replaces_image_link_to_a_file(rt, tmp_path, home):
    # N4, first case: the zoneinfo file must stay untouched.
    ref = _link_image(home, tmp_path, "linkimg1")
    ctx = tmp_path / "ctx-n4a"
    (ctx / "rootfs" / "etc").mkdir(parents=True)
    (ctx / "rootfs" / "etc" / "localtime").write_text("new-tz")
    (ctx / "Dockerfile").write_text(f"FROM {ref}\nCOPY rootfs/ /\n")
    built = rt.build(ctx)
    localtime = built.rootfs / "etc" / "localtime"
    assert not localtime.is_symlink()
    assert localtime.read_text() == "new-tz"
    assert (built.rootfs / "usr" / "share" / "zoneinfo" / "UTC").read_text() == "utc-data"


def test_copy_file_entry_replaces_dangling_image_link(rt, tmp_path, home):
    # N4, second case: a dangling absolute link must not create its target.
    ref = _link_image(home, tmp_path, "linkimg2")
    ctx = tmp_path / "ctx-n4b"
    (ctx / "rootfs" / "opt").mkdir(parents=True)
    (ctx / "rootfs" / "opt" / "app").write_text("app")
    (ctx / "Dockerfile").write_text(f"FROM {ref}\nCOPY rootfs/ /\n")
    built = rt.build(ctx)
    app = built.rootfs / "opt" / "app"
    assert not app.is_symlink()
    assert app.read_text() == "app"
    assert not os.path.lexists(built.rootfs / "nonexist")


def test_copy_single_file_dest_replaces_image_link(rt, tmp_path, home):
    # The top-level ``COPY f /dest`` form follows the same rule.
    ref = _link_image(home, tmp_path, "linkimg3")
    ctx = tmp_path / "ctx-n4c"
    ctx.mkdir()
    (ctx / "tz").write_text("new-tz")
    (ctx / "app").write_text("app")
    (ctx / "Dockerfile").write_text(f"FROM {ref}\nCOPY tz /etc/localtime\nCOPY app /opt/app\n")
    built = rt.build(ctx)
    localtime = built.rootfs / "etc" / "localtime"
    assert not localtime.is_symlink() and localtime.read_text() == "new-tz"
    assert (built.rootfs / "usr" / "share" / "zoneinfo" / "UTC").read_text() == "utc-data"
    app = built.rootfs / "opt" / "app"
    assert not app.is_symlink() and app.read_text() == "app"
    assert not os.path.lexists(built.rootfs / "nonexist")


def test_copy_single_file_dest_parent_resolves_through_image_link(rt, tmp_path, home):
    # Only the parent resolves: /bin/newtool on merged-/usr lands in /usr/bin.
    # A dest that is a link to a directory is still a directory destination.
    ref = _merged_usr_image(home, tmp_path, "mergedusr5")
    ctx = tmp_path / "ctx-parent"
    ctx.mkdir()
    (ctx / "newtool").write_text("new")
    (ctx / "other").write_text("other")
    (ctx / "Dockerfile").write_text(f"FROM {ref}\nCOPY newtool /bin/newtool\nCOPY other /bin\n")
    built = rt.build(ctx)
    assert os.readlink(built.rootfs / "bin") == "usr/bin"
    assert (built.rootfs / "usr" / "bin" / "newtool").read_text() == "new"
    assert (built.rootfs / "usr" / "bin" / "other").read_text() == "other"


def test_copy_file_replaces_earlier_source_symlink_pointing_outside(rt, tmp_path):
    # An earlier source's symlink at the same name is removed, not written
    # through, even when its target lies outside the layer.
    ctx = tmp_path / "ctx-linkthenfile"
    (ctx / "a").mkdir(parents=True)
    (ctx / "a" / "x").symlink_to("/nonexist-outside")
    (ctx / "b").mkdir()
    (ctx / "b" / "x").write_text("file-x")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY a/ b/ /y/\n")
    built = rt.build(ctx)
    x = built.rootfs / "y" / "x"
    assert not x.is_symlink() and x.read_text() == "file-x"


# -- round 4, item 3: a bare key mixed with KEY=value pairs is an error -----

def test_env_bare_key_mixed_with_pairs_raises(rt, tmp_path):
    ctx = tmp_path / "ctx-envmixed"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nENV A=1 B\n")
    with pytest.raises(XcodonError, match="'B'"):
        rt.build(ctx)


# -- final review, item 3: scratch dirs go away even with a read-only context dir --

def test_build_leaves_no_scratch_dir_for_a_readonly_context_dir(rt, tmp_path, home):
    ctx = tmp_path / "ctx-ro"
    (ctx / "ro").mkdir(parents=True)
    (ctx / "ro" / "f").write_text("x")
    os.chmod(ctx / "ro", 0o555)
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY . /opt/c/\nWORKDIR /new/dir\n")
    try:
        built = rt.build(ctx)
    finally:
        os.chmod(ctx / "ro", 0o755)
    assert (built.rootfs / "opt/c/ro/f").read_text() == "x"
    left = [p.name for p in home.path.iterdir() if p.name.startswith(("copy-", "workdir-", "commit-"))]
    assert left == []


# -- final review, item 5: the build holds store shared; RUN never pulls ---------

def _store_is_free(home) -> bool:
    import fcntl

    fd = os.open(home.locks / "store.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_build_holds_store_shared_from_from_to_the_end(rt, tmp_path, home):
    ctx = tmp_path / "ctx-lock"
    ctx.mkdir()
    (ctx / "f").write_text("f")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nRUN echo one > /one\nCOPY f /f\nENV A=1\n")
    free_at_step: dict[str, bool] = {}

    def out(line: str) -> None:
        if line.startswith("Step "):
            free_at_step[line.split()[1]] = _store_is_free(home)

    rt.build(ctx, tags=["xcodon-test/locked:1"], out=out)
    assert free_at_step["1/4"] is True, "FROM runs before the lock (it may pull)"
    assert free_at_step["2/4"] is False and free_at_step["3/4"] is False and free_at_step["4/4"] is False, \
        "prune (store exclusive) must wait while later steps build on intermediate images"
    assert _store_is_free(home), "the build releases the lock at the end"


def test_run_step_never_pulls_a_missing_step_image(rt, home, monkeypatch):
    from xcodon_runtime.build import Builder
    from xcodon_runtime.errors import ImageNotFound
    from xcodon_runtime.imagestore import ImageStore

    base = rt.images.require("xcodon-test/busybox")
    step = rt.images.commit(base, None, changes={"Env": ["STEP=1"]}, created_by="test")
    shutil.rmtree(step.dir)  # what a concurrent `prune --all` did before the build held store

    def no_pull(self, ref, platform=None):
        raise AssertionError(f"a RUN step must never pull; asked for {ref}")

    monkeypatch.setattr(ImageStore, "pull", no_pull)
    with pytest.raises(ImageNotFound):
        Builder(rt, home.path)._run_step(step, ["/bin/true"], {}, "true")


# -- final review, item 6: a symlink source keeps its own name --------------------

def test_copy_symlink_source_keeps_its_name_and_copies_the_target(rt, tmp_path):
    ctx = tmp_path / "ctx-srclink"
    ctx.mkdir()
    (ctx / "real.txt").write_text("R")
    (ctx / "link.txt").symlink_to("real.txt")
    (ctx / "sub").mkdir()
    (ctx / "sub" / "s").write_text("S")
    (ctx / "linkdir").symlink_to("sub")
    (ctx / "Dockerfile").write_text(
        "FROM xcodon-test/busybox\nCOPY link.txt /d/\nCOPY link.txt /f\nCOPY linkdir /e/\n")
    built = rt.build(ctx)
    d = built.rootfs / "d"
    # Docker (BuildKit, checked against docker 29.8.1) follows a top-level
    # source link: the entry is named after the link and holds the target's content.
    assert sorted(os.listdir(d)) == ["link.txt"]
    assert not (d / "link.txt").is_symlink() and (d / "link.txt").read_text() == "R"
    assert (built.rootfs / "f").read_text() == "R"
    assert sorted(os.listdir(built.rootfs / "e")) == ["s"]


def test_copy_symlink_source_pointing_outside_the_context_is_an_error(rt, tmp_path):
    ctx = tmp_path / "ctx-srclink-out"
    ctx.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (ctx / "leak.txt").symlink_to(tmp_path / "secret.txt")
    (ctx / "Dockerfile").write_text("FROM xcodon-test/busybox\nCOPY leak.txt /d/\n")
    with pytest.raises(XcodonError, match="outside the build context"):
        rt.build(ctx)
