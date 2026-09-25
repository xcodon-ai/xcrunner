import io
from pathlib import Path

import pytest

from tests.fake_micromamba import make_fake_micromamba, read_log
from xcodon_runtime import cli
from xcodon_runtime.condaroot import ensure_root, lookup_root, opt_value, resolve_root
from xcodon_runtime.condashim import conda_main, parse_args
from xcodon_runtime.micromamba import MicromambaMissing


@pytest.fixture
def fake(tmp_path):
    return make_fake_micromamba(tmp_path / "fakebin"), tmp_path / "mm.log"


def _env(fake, **extra):
    mm, log = fake
    return {"PATH": "/usr/bin:/bin", "HOME": str(Path("/nonexistent-user-home")),
            "XRUNNER_MICROMAMBA": str(mm), "FAKE_MM_LOG": str(log), **extra}


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".xrunner-env").mkdir(parents=True)
    (proj / "workspace").mkdir()
    return proj


def test_resolve_root_order(tmp_path, home, project):
    sub = project / "workspace"
    assert resolve_root(sub, {"XRUNNER_ENV_DIR": str(tmp_path / "e")}, home).path == tmp_path / "e" / "conda"
    r = resolve_root(sub, {}, home)
    assert (r.path, r.source) == (project / ".xrunner-env" / "conda", "project")
    r = resolve_root(tmp_path / "elsewhere", {}, home)
    assert (r.path, r.source) == (home.path / "conda", "home")
    r = resolve_root(sub, {"XRUNNER_ENV_DIR": "/e"}, home, explicit="r2")
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


def test_create_runs_micromamba_with_isolated_settings(home, project, fake):
    root = project / ".xrunner-env" / "conda"
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
    assert call["env"]["MAMBA_ROOT_PREFIX"] == str(project / ".xrunner-env" / "conda")


def test_read_only_verbs_get_no_yes_and_no_channels(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["--json", "list", "-n", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["list", "--json", "-n", "x", "--no-rc", "-r", str(root)]


def test_yes_and_channels_are_not_duplicated(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    argv = ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == ["install", "-y", "-c", "conda-forge", "-c", "bioconda", "samtools",
                                            "--no-rc", "-r", str(root)]


def test_override_channels_adds_no_defaults(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    argv = ["create", "-p", "workspace/env", "--override-channels", "-c", "bioconda", "bwa"]
    assert conda_main(argv, home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert read_log(fake[1])[0]["argv"] == argv + ["--no-rc", "-r", str(root), "-y"]
    assert (project / "workspace" / "env" / "conda-explicit.txt").exists()


def test_env_subcommands_and_clean_confirmation(home, project, fake):
    root = project / ".xrunner-env" / "conda"
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
    assert not (project / ".xrunner-env" / "conda" / "envs" / "a" / "conda-explicit.txt").exists()


def test_a_failed_export_only_warns(home, project, fake):
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=project,
                      environ=_env(fake, FAKE_MM_EXPORT_EXIT="1"), err=err) == 0
    assert "could not record the packages" in err.getvalue()


def test_base_installs_record_at_the_root(home, project, fake):
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["install", "-n", "base", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert (root / "conda-explicit.txt").exists()


def test_home_fallback_warns_once(home, tmp_path, fake):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    err = io.StringIO()
    assert conda_main(["create", "-n", "a", "x"], home, cwd=lonely, environ=_env(fake), err=err) == 0
    assert err.getvalue() == f"xrunner: no project env folder found; using {home.path / 'conda'}\n"
    assert (home.path / "conda" / "envs" / "a" / "conda-meta").is_dir()


def test_lookups_find_an_env_in_the_home_root(home, project, fake):
    (home.path / "conda" / "envs" / "old" / "conda-meta").mkdir(parents=True)
    err = io.StringIO()
    assert conda_main(["list", "-n", "old"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert conda_main(["create", "-n", "old", "x"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    calls = read_log(fake[1])
    assert calls[0]["argv"][calls[0]["argv"].index("-r") + 1] == str(home.path / "conda")
    assert "no project env folder" not in err.getvalue()
    assert calls[1]["argv"][calls[1]["argv"].index("-r") + 1] == str(project / ".xrunner-env" / "conda")


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
    assert capsys.readouterr().out == "conda 2.9.0 (micromamba via xrunner)\n"
    assert conda_main(["--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    assert "conda run -n NAME" in capsys.readouterr().out
    err = io.StringIO()
    assert conda_main([], home, cwd=project, environ=_env(fake), err=err) == 2


def test_missing_micromamba_raises(home, project):
    with pytest.raises(MicromambaMissing, match="run: xrunner shim install conda"):
        conda_main(["list"], home, cwd=project, environ={"PATH": "/usr/bin:/bin"}, err=io.StringIO())


def test_cli_conda_verb_passes_arguments_through(home, fake, monkeypatch, capfd):
    monkeypatch.setenv("XRUNNER_MICROMAMBA", str(fake[0]))
    assert cli.main(["-v", "conda", "--version"]) == 0
    assert capfd.readouterr().out == "conda 2.9.0 (micromamba via xrunner)\n"
    monkeypatch.delenv("XRUNNER_MICROMAMBA")
    assert cli.main(["conda", "list"]) == 125
    assert "xrunner shim install conda" in capfd.readouterr().err


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
    root = project / ".xrunner-env" / "conda"
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
    root = project / ".xrunner-env" / "conda"
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
    root = project / ".xrunner-env" / "conda"
    assert conda_main(["env", "--help"], home, cwd=project, environ=_env(fake), err=io.StringIO()) == 0
    call = read_log(fake[1])[0]
    assert call["env"]["MAMBA_ROOT_PREFIX"] == str(root)
    assert call["env"]["HOME"] == str(root / ".home")


def test_config_list_passes_through(home, project, fake):
    root = project / ".xrunner-env" / "conda"
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
    root = project / ".xrunner-env" / "conda"
    err = io.StringIO()
    assert conda_main(["remove", "-n", "ghost", "x"], home, cwd=project, environ=_env(fake), err=err) == 0
    assert err.getvalue() == (f"xrunner: warning: could not record the packages of {root / 'envs' / 'ghost'}: "
                              "no environment there\n")


def test_env_create_from_file_uses_the_files_name(home, project, fake, tmp_path):
    root = project / ".xrunner-env" / "conda"
    envfile = tmp_path / "env.yml"
    envfile.write_text("name: fromyml\ndependencies: []\n")
    err = io.StringIO()
    assert conda_main(["env", "create", "-f", str(envfile)], home, cwd=project, environ=_env(fake), err=err) == 0
    assert (root / "envs" / "fromyml" / "conda-explicit.txt").read_text().startswith("@EXPLICIT")
    assert err.getvalue() == ""


def test_env_create_from_file_with_name_and_prefix_uses_the_name(home, project, fake, tmp_path):
    """`conda env export` writes both `name:` and `prefix:` by default; micromamba
    2.9.0's `env create -f FILE` (and so the shim) only honors `name:`."""
    root = project / ".xrunner-env" / "conda"
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
    root = project / ".xrunner-env" / "conda"
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
    monkeypatch.setenv("XRUNNER_MICROMAMBA", str(fake[0]))
    monkeypatch.setenv("FAKE_MM_LOG", str(fake[1]))
    assert cli.main(["conda", "-v", "list"]) == 0
    assert "-v" in read_log(fake[1])[0]["argv"]


def test_cli_conda_home_flag_is_not_swallowed_by_xrunners_own_home_flag(home, fake, monkeypatch, tmp_path):
    """`--home Z` sits after `conda` in argv, so it must reach conda_main as part of its
    own args, not be consumed by xrunner's global --home (which would otherwise eat `Z`
    and leave `list` looking like a bare, successful command)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XRUNNER_MICROMAMBA", str(fake[0]))
    monkeypatch.setenv("FAKE_MM_LOG", str(fake[1]))
    assert cli.main(["conda", "--home", "Z", "list"]) == 2
    assert read_log(fake[1]) == []
