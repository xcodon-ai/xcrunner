"""Direct unit tests of the pure helpers in ``buildpaths``. No busybox or engine needed."""

import os

import pytest

from xcodon_runtime.buildpaths import MAX_SYMLINK_HOPS, expand_args, resolve_in_rootfs, split_words
from xcodon_runtime.build import parse_env
from xcodon_runtime.errors import XcodonError


# -- split_words ---------------------------------------------------------------

def test_split_words_splits_on_unquoted_whitespace():
    assert split_words("a  b\tc", {}) == ["a", "b", "c"]


def test_split_words_unclosed_brace_raises():
    with pytest.raises(XcodonError, match="unclosed"):
        split_words("${X", {"X": "1"})


def test_split_words_junk_after_name_is_bad_substitution():
    with pytest.raises(XcodonError, match="bad substitution"):
        split_words("${X y}", {"X": "1"})


def test_split_words_brace_without_a_name_is_bad_substitution():
    with pytest.raises(XcodonError, match="bad substitution"):
        split_words("${}", {})


def test_expand_args_also_rejects_bad_substitution():
    with pytest.raises(XcodonError, match="bad substitution"):
        expand_args("${X y}", {"X": "1"})


def test_split_words_backslash_in_double_quotes_before_plain_char_stays():
    assert split_words(r'"a\b"', {}) == [r"a\b"]


def test_split_words_backslash_in_double_quotes_escapes_special_chars():
    assert split_words(r'"q\"d\\e\$X"', {"X": "no"}) == ['q"d\\e$X']


def test_split_words_trailing_backslash_is_kept_literal():
    assert split_words("abc\\", {}) == ["abc\\"]


def test_split_words_single_quotes_are_literal():
    assert split_words(r"'$X \n \"' b", {"X": "no"}) == [r'$X \n \"', "b"]


def test_split_words_backslash_dollar_is_literal_dollar():
    assert split_words(r"\$X", {"X": "no"}) == ["$X"]


def test_split_words_backslash_space_joins_words():
    assert split_words(r"a\ b c", {}) == ["a b", "c"]


def test_split_words_colon_minus():
    assert split_words("${X:-w}", {}) == ["w"]
    assert split_words("${X:-w}", {"X": ""}) == ["w"]
    assert split_words("${X:-w}", {"X": "v"}) == ["v"]


def test_split_words_colon_plus():
    assert split_words("${X:+w}", {}) == [""]
    assert split_words("${X:+w}", {"X": ""}) == [""]
    assert split_words("${X:+w}", {"X": "v"}) == ["w"]


def test_split_words_dollar_without_name_is_literal():
    assert split_words("$ $1", {}) == ["$", "$1"]


def test_split_words_expansion_inside_double_quotes_keeps_spaces():
    assert split_words('"$X"', {"X": "a b"}) == ["a b"]


def test_split_words_unclosed_quotes_raise():
    with pytest.raises(XcodonError, match="unclosed"):
        split_words("'abc", {})
    with pytest.raises(XcodonError, match="unclosed"):
        split_words('"abc', {})


# -- parse_env -----------------------------------------------------------------

def test_parse_env_bare_key_mixed_with_pairs_raises():
    with pytest.raises(XcodonError, match="B"):
        parse_env("A=1 B")


def test_parse_env_pairs_and_legacy_form_still_work():
    assert parse_env("A=1 B=") == {"A": "1", "B": ""}
    assert parse_env("KEY some value") == {"KEY": "some value"}


# -- resolve_in_rootfs ---------------------------------------------------------

def test_resolve_plain_and_missing_paths(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_in_rootfs(tmp_path, "/a") == "/a"
    assert resolve_in_rootfs(tmp_path, "a/missing/x") == "/a/missing/x"
    assert resolve_in_rootfs(tmp_path, "/../../a/./") == "/a"
    assert resolve_in_rootfs(tmp_path, "/") == "/"


def test_resolve_symlink_loop_raises(tmp_path):
    (tmp_path / "loop").symlink_to("loop")
    with pytest.raises(XcodonError, match="too many symlink hops"):
        resolve_in_rootfs(tmp_path, "/loop/x")


def test_resolve_two_link_cycle_raises(tmp_path):
    (tmp_path / "a").symlink_to("b")
    (tmp_path / "b").symlink_to("a")
    with pytest.raises(XcodonError, match="too many symlink hops"):
        resolve_in_rootfs(tmp_path, "/a")


def test_resolve_long_chain_under_the_limit_works(tmp_path):
    (tmp_path / "end").mkdir()
    prev = "end"
    for i in range(MAX_SYMLINK_HOPS):
        (tmp_path / f"l{i}").symlink_to(prev)
        prev = f"l{i}"
    assert resolve_in_rootfs(tmp_path, f"/{prev}/f") == "/end/f"


def test_resolve_relative_target_dotdot_is_clamped_at_root(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "up").symlink_to("../../../../../etc")
    assert resolve_in_rootfs(tmp_path, "/a/b/up/passwd") == "/etc/passwd"


def test_resolve_relative_target_is_joined_to_link_dir(tmp_path):
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "bin").symlink_to("usr/bin")
    assert resolve_in_rootfs(tmp_path, "/bin/sh") == "/usr/bin/sh"


def test_resolve_absolute_target_is_rerooted_at_rootfs(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "run").symlink_to("/run")
    (tmp_path / "var" / "etc").symlink_to("/etc")  # the host /etc exists, the rootfs one does not
    assert resolve_in_rootfs(tmp_path, "/var/run/x") == "/run/x"
    assert resolve_in_rootfs(tmp_path, "/var/etc/passwd") == "/etc/passwd"
    assert not os.path.lexists(tmp_path / "etc")


def test_resolve_through_non_directory_raises(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd").write_text("x")
    with pytest.raises(XcodonError, match="not a directory"):
        resolve_in_rootfs(tmp_path, "/etc/passwd/x")


def test_resolve_final_component_link_to_file_is_followed(tmp_path):
    (tmp_path / "f").write_text("x")
    (tmp_path / "l").symlink_to("f")
    assert resolve_in_rootfs(tmp_path, "/l") == "/f"
