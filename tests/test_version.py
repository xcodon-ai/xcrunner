import re
from pathlib import Path

from xcodon_runtime import __version__


def test_package_version_matches_pyproject():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"$', text, re.M)
    assert m and m.group(1) == __version__
