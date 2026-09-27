import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_micromamba import make_fake_micromamba, read_log
from xcodon_runtime import cli
from xcodon_runtime import condaroot
from xcodon_runtime.condaroot import ensure_root, lookup_root, opt_value, resolve_root, split_option
from xcodon_runtime.condashim import _split_before_run, conda_main, parse_args
from xcodon_runtime.micromamba import MicromambaMissing


@pytest.fixture
def fake(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin"), tmp_path / "mm.log"


def _env(fake, **extra):
    mm, log = fake
    return {"PATH": "/usr/bin:/bin", "HOME": str(Path("/nonexistent-user-home")),
            "XCRUNNER_MICROMAMBA": str(mm), "FAKE_MM_LOG": str(log), **extra}


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".xcrunner-env").mkdir(parents=True)
    # resolve_root skips a group- or world-writable .xcrunner-env; do not depend on the umask.
    (proj / ".xcrunner-env").chmod(0o755)
    (proj / "workspace").mkdir()
    return proj


def test_resolve_root_order(tmp_path, home, project):
    sub = project / "workspace"
    assert resolve_root(sub, {"XCRUNNER_ENV_DIR": str(tmp_path / "e")}, home).path == tmp_path / "e" / "conda"
    r = resolve_root(sub, {}, home)
    assert (r.path, r.source) == (project / ".xcrunner-env" / "conda", "project")
    r = resolve_root(tmp_path / "elsewhere", {}, home)
    assert (r.path, r.source) == (home.path / "conda", "home")
    r = resolve_root(sub, {"XCRUNNER_ENV_DIR": "/e"}, home, explicit="r2")
    assert (r.path, r.source) == (sub / "r2", "flag")


def test_lookup_root_falls_back_to_the_home_root(home, project):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    root = resolve_root(project, {}, home)
    found = lookup_root(root, home, "old")
    assert (found.path, found.source) == (home.path / "conda", "lookup")
    assert lookup_root(root, home, "new").path == root.path


def test_parse_args():
    p = parse_args(["--json", "create", "-n", "a", "-c", "bioconda", "--channel=conda-forge", "bwa", "-y"])
    assert (p.verb, p.name, p.channels, p.yes) == ("create", "a", ["bioconda", "conda-forge"], True)
    assert p.tokens == ["--json", "-n", "a", "-c", "bioconda", "--channel=conda-forge", "bwa", "-y"]
    p = parse_args(["env", "export", "-p", "x", "-r", "/root2", "--explicit"])
    assert (p.verb, p.sub, p.prefix, p.root_flag, p.key) == ("env", "export", "x", "/root2", "env export")
    assert "-r" not in p.tokens and "/root2" not in p.tokens
    assert parse_args(["--version"]).version and parse_args(["-h"]).help


def test_split_before_run_finds_run_after_leading_options():
    assert _split_before_run(["run", "-n", "x", "tool"]) == ([], ["-n", "x", "tool"])
    assert _split_before_run(["-r", "/root", "run", "-n", "x", "tool"]) == (
        ["-r", "/root"], ["-n", "x", "tool"])
    assert _split_before_run(["-q", "run", "tool"]) == (["-q"], ["tool"])
    assert _split_before_run(["-rroot", "run", "tool"]) == (["-rroot"], ["tool"])
    assert _split_before_run(["--root-prefix=/root", "run", "tool"]) == (
        ["--root-prefix=/root"], ["tool"])
    assert _split_before_run(["create", "-n", "x"]) is None
    assert _split_before_run(["-y", "create", "-n", "x"]) is None
    assert _split_before_run([]) is None


def test_create_runs_micromamba_with_isolated_settings(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["create", "-n", "bwa_env", "-c", "bioconda", "bwa"], home, cwd=project,
                      environ=_env(fake), err=err) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"] == ["create", "-n", "bwa_env", "-c", "bioconda", "bwa",
                                "--no-rc", "-r", str(root), "-y", "-c", "conda-forge"]
    assert calls[0]["cwd"] == str(project)
    assert calls[0]["env"] == {
        "HOME": str(root / ".home"), "MAMBA_ROOT_PREFIX": str(root),
        "CONDA_PKGS_DIRS": str(home.path / "conda-pkgs"),
        "XDG_CACHE_HOME": str(root / ".home" / ".cache"),
        "XDG_CONFIG_HOME": str(root / ".home" / ".config"),
        "CONDARC": None, "CONDA_PREFIX": None,
    }
    prefix = root / "envs" / "bwa_env"
    assert calls[1]["argv"] == ["env", "export", "--no-rc", "-r", str(root), "-p", str(prefix), "--explicit"]
    assert (prefix / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert err.getvalue() == ""


def test_user_conda_variables_are_dropped(home, project, fake):
    env = _env(fake, CONDARC="/home/u/.condarc", CONDA_PREFIX="/home/u/miniconda3", MAMBA_ROOT_PREFIX="/x")
    assert conda_main(["list"], home, cwd=project, environ=env, err=io.StringIO()) == 0
    call = read_log(fake[1])[0]
    assert call["env"]["CONDARC"] is None and call["env"]["CONDA_PREFIX"] is None
    assert call["env"]["MAMBA_ROOT_PREFIX"] == str(project / ".xcrunner-env" / "conda")


def test_read_only_verbs_get_no_yes_and_no_channels(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["--json", "list", "-n", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["list", "--json", "-n", "x", "--no-rc", "-r", str(root)]


def test_yes_and_channels_are_not_duplicated(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    argv = ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools",
                                            "--no-rc", "-r", str(root)]


def test_override_channels_adds_no_defaults(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    argv = ["create", "-p", "workspace/env", "--override-channels", "-c", "bioconda", "bwa"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    sent = ["create", "-p", str(project / "workspace" / "env"), "--override-channels", "-c", "bioconda", "bwa"]
    assert read_log(fake[1])[0]["argv"] == sent + ["--no-rc", "-r", str(root), "-y"]
    assert (project / "workspace" / "env" / "conda-explicit.txt").exists()


def test_env_subcommands_and_clean_confirmation(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["env", "remove", "-n", "a"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert conda_main(["clean", "-a"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"] == ["env", "remove", "-n", "a", "--no-rc", "-r", str(root), "-y"]
    assert calls[1]["argv"] == ["clean", "-a", "--no-rc", "-y"], "micromamba's clean rejects -r"
    assert calls[1]["env"]["MAMBA_ROOT_PREFIX"] == str(root)


def test_root_flag_replaces_the_resolved_root(home, project, fake, tmp_path):
    custom = tmp_path / "custom"
    assert conda_main(["create", "-r", str(custom), "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake), err=io.StringIO()) == 0
    argv = read_log(fake[1])[0]["argv"]
    assert argv.count("-r") == 1 and argv[argv.index("-r") + 1] == str(custom)
    assert (custom / ".home").is_dir()


def test_failed_command_keeps_its_exit_code_and_writes_no_record(home, project, fake):
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project, environ=_env(fake, FAKE_MM_EXIT="3"),
                      err=io.StringIO()) == 3
    assert len(read_log(fake[1])) == 1
    assert not (project / ".xcrunner-env" / "conda" / "envs" / "a" / "conda-explicit.txt").exists()


def test_a_failed_export_only_warns(home, project, fake):
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake, FAKE_MM_EXPORT_EXIT="1"), err=err) == 0
    assert "could not record the packages" in err.getvalue()


def test_base_installs_record_at_the_root(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["install", "-n", "base", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert (root / "conda-explicit.txt").exists()


def test_home_fallback_warns_once(home, tmp_path, fake):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=lonely, environ=_env(fake), err=err) == 0
    assert err.getvalue() == (f"xcrunner: no project env folder found; using {home.path / 'conda'} "
                              "(set XCRUNNER_ENV_DIR or create .xcrunner-env in the project)\n")
    assert (home.path / "conda" / "envs" / "a" / "conda-meta").is_dir()


def test_lookups_find_an_env_in_the_home_root(home, project, fake):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    err = io.StringIO()
    assert conda_main(["list", "-n", "old"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert conda_main(["create", "-n", "old", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"][calls[0]["argv"].index("-r") + 1] == str(home.path / "conda")
    assert "no project env folder" not in err.getvalue()
    assert calls[1]["argv"][calls[1]["argv"].index("-r") + 1] == str(project / ".xcrunner-env" / "conda")


def test_refused_and_unsupported_verbs(home, project, fake):
    err = io.StringIO()
    assert conda_main(["activate", "bwa_env"], home, cwd=project, environ=_env(fake), err=err) == 1
    assert "conda run -n NAME" in err.getvalue()
    err = io.StringIO()
    assert conda_main(["build", "recipe/"], home, cwd=project, environ=_env(fake), err=err) == 2
    assert "not supported" in err.getvalue()
    err = io.StringIO()
    assert conda_main(["env", "update", "-f", "x.yml"], home, cwd=project, environ=_env(fake), err=err) == 2
    assert read_log(fake[1]) == []


def test_version_and_usage(home, project, fake, capsys):
    assert conda_main(["--version"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert capsys.readouterr().out == "conda 2.9.0 (micromamba via xcrunner)\n"
    assert conda_main(["--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert "conda run -n NAME" in capsys.readouterr().out
    err = io.StringIO()
    assert conda_main([], home, cwd=project, environ=_env(fake), err=err) == 2


def test_missing_micromamba_raises(home, project):
    with pytest.raises(MicromambaMissing, match="run: xcrunner shim install conda"):
        conda_main(["list"], home, cwd=project, environ={"PATH": "/usr/bin:/bin"}, err=io.StringIO())


def test_cli_conda_verb_passes_arguments_through(home, fake, monkeypatch, capfd):
    monkeypatch.setenv("XCRUNNER_MICROMAMBA", str(fake[0]))
    assert cli.main(["-v", "conda", "--version"]) == 0
    assert capfd.readouterr().out == "conda 2.9.0 (micromamba via xcrunner)\n"
    monkeypatch.delenv("XCRUNNER_MICROMAMBA")
    assert cli.main(["conda", "list"]) == 125
    assert "xcrunner shim install conda" in capfd.readouterr().err


# -- fix round 1 -----------------------------------------------------------------------


def test_ensure_root_creates_a_base_env(tmp_path):
    """A fresh root is a base env too, as in real conda, or a bare `conda list` on it
    (which targets the root itself) finds no environment there."""
    root = tmp_path / "r"
    ensure_root(root)
    assert (root / "conda-meta").is_dir()
    assert (root / "envs").is_dir()
    assert (root / ".home").is_dir()


def test_list_on_a_fresh_root_hands_micromamba_a_root_with_conda_meta(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["list"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert (root / "conda-meta").is_dir()


def test_opt_value_short_option_attached_value():
    """Following argparse: a short option's attached value is everything after the
    option letter (`-nfoo` -> `foo`, `-n=foo` -> `=foo`, the `=` is not special)."""
    assert opt_value(["-nfoo"], 0) == ("foo", 1)
    assert opt_value(["-n=foo"], 0) == ("=foo", 1)
    assert opt_value(["-n", "foo"], 0) == ("foo", 2)
    assert opt_value(["--channel=conda-forge"], 0) == ("conda-forge", 1)


def test_parse_args_short_options_with_attached_values():
    p = parse_args(["list", "-nfoo"])
    assert p.name == "foo"
    p = parse_args(["list", "-cbioconda"])
    assert p.channels == ["bioconda"]
    p = parse_args(["list", "-r=/x", "-n", "a"])
    assert p.root_flag == "=/x" and p.name == "a"


def test_parse_args_combined_short_flags_set_yes():
    p = parse_args(["create", "-yq"])
    assert p.yes is True
    assert p.tokens == ["-yq"]


def test_combined_short_flags_set_yes_without_duplicating_it(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["create", "-yq", "-n", "a", "x"], home, cwd=project, environ=_env(fake),
                      err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["create", "-yq", "-n", "a", "x", "--no-rc", "-r", str(root),
                                            "-c", "conda-forge", "-c", "bioconda"]


def test_env_remove_looks_up_an_env_that_lives_only_in_the_home_root(home, project, fake):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    err = io.StringIO()
    assert conda_main(["env", "remove", "-n", "old"], home, cwd=project, environ=_env(fake), err=err) == 0
    call = read_log(fake[1])[0]
    assert call["argv"][call["argv"].index("-r") + 1] == str(home.path / "conda")
    assert "no project env folder" not in err.getvalue()


def test_env_help_passes_through_to_micromamba(home, project, fake):
    assert conda_main(["env", "--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["env", "--help"]
    assert conda_main(["env", "-h"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[1]["argv"] == ["env", "-h"]


def test_env_help_returns_micromambas_exit_code(home, project, fake):
    assert conda_main(["env", "--help"], home, cwd=project, environ=_env(fake, FAKE_MM_EXIT="5"),
                      err=io.StringIO()) == 5


def test_env_help_uses_the_isolated_micromamba_environment(home, project, fake):
    """`env --help` still needs a resolved, ensured root and the same isolated HOME/
    MAMBA_ROOT_PREFIX as every other micromamba call, not the caller's raw environ."""
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["env", "--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    call = read_log(fake[1])[0]
    assert call["env"]["MAMBA_ROOT_PREFIX"] == str(root)
    assert call["env"]["HOME"] == str(root / ".home")


def test_config_list_passes_through(home, project, fake):
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["config", "list"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["config", "list", "--no-rc", "-r", str(root)]


def test_config_changes_are_refused(home, project, fake):
    err = io.StringIO()
    assert conda_main(["config", "--add", "channels", "x"], home, cwd=project, environ=_env(fake), err=err) == 2
    assert "config list" in err.getvalue()
    assert read_log(fake[1]) == []


def test_record_warns_when_the_target_prefix_has_no_environment(home, project, fake):
    """A RECORD verb that exits 0 but whose target prefix was never actually created
    (here: removing packages from an env name that does not exist) warns instead of
    silently writing nothing."""
    root = project / ".xcrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["remove", "-n", "ghost", "x"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert err.getvalue() == (f"xcrunner: warning: could not record the packages of {root / 'envs' / 'ghost'}: "
                              "no environment there\n")


# -- fix round 1 -------------------------------------------------------------------------


def test_config_list_works_with_a_preceding_option(home, project, fake):
    """`conda -q config list` used to exit 2: the config check read p.tokens[0], which
    is the pre-verb `-q`, not `list`."""
    root = project / ".xcrunner-env" / "conda"
    assert conda_main(["-q", "config", "list"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["config", "-q", "list", "--no-rc", "-r", str(root)]


def test_config_change_with_a_preceding_option_is_still_refused(home, project, fake):
    err = io.StringIO()
    assert conda_main(["-q", "config", "--add", "channels", "x"], home, cwd=project,
                      environ=_env(fake), err=err) == 2
    assert "config list" in err.getvalue()
    assert read_log(fake[1]) == []


def test_dry_run_writes_no_record_and_prints_no_warning(home, project, fake):
    """--dry-run creates nothing, so xcrunner must neither write conda-explicit.txt (the
    fake micromamba here still creates the env dirs, ignoring the flag, which is why
    this fails without the fix: _write_record would find them and write anyway) nor
    warn about a missing environment."""
    root = project / ".xcrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["create", "--dry-run", "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake), err=err) == 0
    argv = read_log(fake[1])[0]["argv"]
    assert "--dry-run" in argv and "-d" not in argv
    assert not (root / "envs" / "a" / "conda-explicit.txt").exists()
    assert err.getvalue() == ""


def test_dry_run_short_flag_is_translated_to_dry_run_and_skips_the_record(home, project, fake):
    """conda's `-d` means `--dry-run`, but micromamba 2.9.0 has no `-d` at all and
    rejects it outright (the fake now mirrors this, see fake_micromamba.py): xcrunner
    must translate `-d` to `--dry-run` before it ever reaches micromamba, not just
    pass it through. Without the translation this fails with exit code 2, "not
    expected: -d", from the fake."""
    root = project / ".xcrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["create", "-d", "-n", "b", "x"], home, cwd=project, environ=_env(fake), err=err) == 0
    argv = read_log(fake[1])[0]["argv"]
    assert "--dry-run" in argv and "-d" not in argv
    assert not (root / "envs" / "b" / "conda-explicit.txt").exists()
    assert err.getvalue() == ""


def test_dash_d_is_left_alone_outside_the_dry_run_verbs(home, project, fake):
    """conda does not define -d/--dry-run for `list`; xcrunner must not translate a
    bare `-d` there (it is simply an unrecognized flag, same as real conda/micromamba
    would see, and RECORD does not apply to `list` anyway). The fake rejects any
    bare `-d` it receives, so this also confirms it reached micromamba untouched."""
    assert conda_main(["list", "-d"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 2
    argv = read_log(fake[1])[0]["argv"]
    assert "-d" in argv and "--dry-run" not in argv


def test_env_create_from_file_uses_the_files_name(home, project, fake, tmp_path):
    root = project / ".xcrunner-env" / "conda"
    envfile = tmp_path / "env.yml"
    envfile.write_text("name: fromyml\ndependencies: []\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-f", str(envfile)], home, cwd=project, environ=_env(fake), err=err) == 0
    assert (root / "envs" / "fromyml" / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert err.getvalue() == ""


def test_env_create_from_file_with_name_and_prefix_uses_the_name(home, project, fake, tmp_path):
    """`conda env export` writes both `name:` and `prefix:` by default; micromamba
    2.9.0's `env create -f FILE` (and so the shim) only honors `name:`."""
    root = project / ".xcrunner-env" / "conda"
    ignored_prefix = tmp_path / "ignored-prefix"
    envfile = tmp_path / "env2.yml"
    envfile.write_text(f"name: fromyml\nprefix: {ignored_prefix}\ndependencies: []\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-f", str(envfile)], home, cwd=project, environ=_env(fake), err=err) == 0
    assert (root / "envs" / "fromyml" / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert not ignored_prefix.exists()
    assert err.getvalue() == ""


def test_env_create_from_file_with_only_prefix_fails(home, project, fake, tmp_path):
    """Real micromamba 2.9.0 never reads a file's `prefix:`; with no name anywhere
    (file, -n, or -p) it exits 1, "No target prefix specified", and nothing is recorded."""
    target = tmp_path / "onlyprefix"
    envfile = tmp_path / "env3.yml"
    envfile.write_text(f"prefix: {target}\ndependencies: []\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-f", str(envfile)], home, cwd=project, environ=_env(fake), err=err) == 1
    assert not target.exists()
    assert err.getvalue() == ""


def test_env_create_dash_n_overrides_the_file(home, project, fake, tmp_path):
    root = project / ".xcrunner-env" / "conda"
    envfile = tmp_path / "env3.yml"
    envfile.write_text("name: fromyml\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-f", str(envfile), "-n", "override"], home, cwd=project,
                      environ=_env(fake), err=err) == 0
    assert (root / "envs" / "override" / "conda-explicit.txt").exists()
    assert not (root / "envs" / "fromyml").exists()
    assert err.getvalue() == ""


def test_cli_conda_dash_v_reaches_micromamba_untouched(home, fake, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XCRUNNER_MICROMAMBA", str(fake[0]))
    monkeypatch.setenv("FAKE_MM_LOG", str(fake[1]))
    assert cli.main(["conda", "-v", "list"]) == 0
    assert "-v" in read_log(fake[1])[0]["argv"]


def test_cli_conda_home_flag_is_not_swallowed_by_xcrunners_own_home_flag(home, fake, monkeypatch, tmp_path):
    """`--home Z` sits after `conda` in argv, so it must reach conda_main as part of its
    own args, not be consumed by xcrunner's global --home (which would otherwise eat `Z`
    and leave `list` looking like a bare, successful command)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XCRUNNER_MICROMAMBA", str(fake[0]))
    monkeypatch.setenv("FAKE_MM_LOG", str(fake[1]))
    assert cli.main(["conda", "--home", "Z", "list"]) == 2
    assert read_log(fake[1]) == []


# -- final review ----------------------------------------------------------------------


@pytest.mark.parametrize("form", [["-p", "myenv"], ["-pmyenv"], ["--prefix=myenv"], ["--prefix", "myenv"]])
def test_relative_prefix_without_a_slash_is_a_folder_in_cwd(home, project, fake, form):
    """micromamba 2.9.0 reads a -p value with no `/` as an env name, `<root>/envs/NAME`
    (the fake now does too); xcrunner hands it the absolute `<cwd>/NAME` instead, so the
    env, the record and `conda run -p NAME` all agree on one folder."""
    root = project / ".xcrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["create", *form, "x"], home, cwd=project, environ=_env(fake), err=err) == 0
    target = project / "myenv"
    argv = read_log(fake[1])[0]["argv"]
    assert str(target) in "".join(argv) and "myenv" not in [a for a in argv if not a.startswith(("-", "/"))]
    assert (target / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert not (root / "envs" / "myenv").exists()
    assert err.getvalue() == ""


def test_prefix_is_made_absolute_in_every_form(home, project, fake):
    target = str(project / "myenv")
    for form, sent in ((["-p", "myenv"], ["-p", target]), (["-pmyenv"], ["-p" + target]),
                       (["--prefix=myenv"], ["--prefix=" + target]), (["--prefix", "myenv"], ["--prefix", target]),
                       (["-p", "/abs/e"], ["-p", "/abs/e"])):
        p = parse_args(["list", *form], cwd=project)
        assert p.tokens == sent and p.prefix == sent[-1].split("=")[-1].removeprefix("-p")


def test_env_create_prefix_without_a_slash_is_a_folder_in_cwd(home, project, fake, tmp_path):
    envfile = tmp_path / "env.yml"
    envfile.write_text("name: fromyml\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-p", "myenv", "-f", str(envfile)], home, cwd=project,
                      environ=_env(fake), err=err) == 0
    argv = read_log(fake[1])[0]["argv"]
    assert argv[argv.index("-p") + 1] == str(project / "myenv")
    assert (project / "myenv" / "conda-explicit.txt").exists()
    assert err.getvalue() == ""


def test_create_then_run_with_a_relative_prefix_without_a_slash(home, project, fake):
    env = {**_env(fake), "XCODON_RUNTIME_HOME": str(home.path)}
    xr = [sys.executable, "-m", "xcodon_runtime.cli", "conda"]
    r = subprocess.run(xr + ["create", "-p", "myenv", "x"], cwd=project, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "warning" not in r.stderr, r.stderr
    r = subprocess.run(xr + ["run", "-p", "myenv", "sh", "-c", 'echo "$CONDA_PREFIX"'], cwd=project, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{project / 'myenv'}\n"


def _mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_upward_search_skips_a_world_writable_env_folder(home, tmp_path):
    shared = tmp_path / "shared"
    (shared / ".xcrunner-env").mkdir(parents=True)
    (shared / ".xcrunner-env").chmod(0o777)
    work = shared / "work"
    work.mkdir()
    r = resolve_root(work, {}, home)
    assert (r.path, r.source) == (home.path / "conda", "home")
    (shared / ".xcrunner-env").chmod(0o775)
    assert resolve_root(work, {}, home).source == "home", "group-writable is skipped too"


def test_upward_search_keeps_looking_past_an_untrusted_folder(home, tmp_path):
    outer = tmp_path / "outer"
    (outer / ".xcrunner-env").mkdir(parents=True)
    (outer / ".xcrunner-env").chmod(0o755)
    inner = outer / "inner"
    (inner / ".xcrunner-env").mkdir(parents=True)
    (inner / ".xcrunner-env").chmod(0o777)
    r = resolve_root(inner, {}, home)
    assert (r.path, r.source) == (outer / ".xcrunner-env" / "conda", "project")


def test_upward_search_accepts_a_link_to_a_folder_you_own(home, tmp_path):
    real = tmp_path / "real-env"
    real.mkdir()
    real.chmod(0o755)
    proj = tmp_path / "linked"
    proj.mkdir()
    (proj / ".xcrunner-env").symlink_to(real)
    r = resolve_root(proj, {}, home)
    assert (r.path, r.source) == (proj / ".xcrunner-env" / "conda", "project")


def test_upward_search_skips_a_folder_another_user_owns(home, project, monkeypatch):
    other_uid = os.getuid() + 1
    monkeypatch.setattr(condaroot.os, "getuid", lambda: other_uid)
    assert resolve_root(project, {}, home).source == "home"


def test_env_dir_and_root_flag_stay_trusted(home, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    assert resolve_root(tmp_path, {"XCRUNNER_ENV_DIR": str(loose)}, home).path == loose / "conda"
    assert resolve_root(tmp_path, {}, home, explicit=str(loose)).path == loose


def test_ensure_root_dirs_are_not_group_or_world_writable(tmp_path):
    old = os.umask(0o022)
    try:
        env_folder = tmp_path / "p" / ".xcrunner-env"
        ensure_root(env_folder / "conda")
    finally:
        os.umask(old)
    for d in (env_folder, env_folder / "conda", env_folder / "conda" / ".home",
              env_folder / "conda" / "envs", env_folder / "conda" / "conda-meta"):
        assert not _mode(d) & 0o022, (d, oct(_mode(d)))
    home_like = type("H", (), {"path": tmp_path / "h"})()
    assert resolve_root(tmp_path / "p", {}, home_like).source == "project"


def test_ensure_root_wraps_every_mkdir_error(tmp_path):
    from xcodon_runtime.errors import XcodonError

    root = tmp_path / "r"
    (root / ".home").mkdir(parents=True)
    (root / "envs").write_text("a file, not a folder")
    with pytest.raises(XcodonError, match="cannot create the conda root prefix"):
        ensure_root(root)


def test_record_temp_file_name_is_unique(home, project, fake):
    """The record used to go through a fixed `conda-explicit.txt.tmp`, so two installs
    into one env could collide; a leftover of that name (here, a folder) broke it."""
    prefix = project / ".xcrunner-env" / "conda" / "envs" / "a"
    (prefix / "conda-explicit.txt.tmp").mkdir(parents=True)
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert (prefix / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert err.getvalue() == ""
    assert not [p for p in prefix.iterdir() if p.name.startswith(".conda-explicit-")]


@pytest.mark.parametrize("argv", [["remove", "-n", "b", "--all"], ["remove", "-n", "b", "-a"],
                                  ["uninstall", "-n", "b", "--all"], ["remove", "-ya", "-n", "b"],
                                  ["env", "remove", "-n", "b"]])
def test_removing_a_whole_env_drops_its_record(home, project, fake, argv):
    prefix = project / ".xcrunner-env" / "conda" / "envs" / "b"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "conda-explicit.txt").write_text("@EXPLICIT\nold\n")
    err = io.StringIO()
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=err) == 0
    assert not (prefix / "conda-explicit.txt").exists()
    assert len(read_log(fake[1])) == 1, "no export after removing everything"
    assert err.getvalue() == ""


def test_removing_one_package_still_records(home, project, fake):
    prefix = project / ".xcrunner-env" / "conda" / "envs" / "b"
    (prefix / "conda-meta").mkdir(parents=True)
    assert conda_main(["remove", "-n", "b", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert "seqtk" in (prefix / "conda-explicit.txt").read_text()


def test_opt_value_does_not_take_an_option_as_a_value():
    assert opt_value(["-r", "-n", "c"], 0) == (None, 1)
    assert opt_value(["--name", "--json"], 0) == (None, 1)
    assert opt_value(["-n", "-"], 0) == ("-", 2), "a lone `-` is a value"
    assert opt_value(["-n-x"], 0) == ("-x", 1), "an attached value is always the value"


@pytest.mark.parametrize("argv,opt", [(["list", "-r", "-n", "c"], "-r"), (["create", "-n"], "-n"),
                                      (["create", "-p", "--json", "x"], "-p"), (["list", "--name"], "--name")])
def test_a_missing_value_exits_2(home, project, fake, argv, opt):
    err = io.StringIO()
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=err) == 2
    assert err.getvalue() == f"conda: {opt} needs a value\n"
    assert read_log(fake[1]) == []
    assert not (project / "-n").exists()


def test_rc_file_is_not_a_pre_verb_option():
    assert _split_before_run(["--rc-file", "x", "run", "tool"]) is None


def test_split_option():
    assert split_option("--name=foo", "n") == ("--name", "foo")
    assert split_option("--name=", "n") == ("--name", "")
    assert split_option("--name", "n") == ("--name", None)
    assert split_option("-nfoo", "n") == ("-n", "foo")
    assert split_option("-n=foo", "n") == ("-n", "=foo")
    assert split_option("-n", "n") == ("-n", None)
    assert split_option("-yq", "n") == ("-yq", None)
    assert split_option("-nfoo", "p") == ("-nfoo", None)
    assert split_option("tool", "n") == ("tool", None)
    assert split_option("-", "n") == ("-", None)
