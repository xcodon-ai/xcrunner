"""coala drives cwltool, and cwltool drives us through --user-space-docker-cmd."""

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.cwltool

TOOL = textwrap.dedent(
    """\
    cwlVersion: v1.2
    class: CommandLineTool
    requirements:
      DockerRequirement:
        dockerPull: xcodon-test/busybox:latest
    inputs:
      message:
        type: string
        inputBinding: {position: 1}
    baseCommand: [echo]
    stdout: out.txt
    outputs:
      out:
        type: File
        outputBinding: {glob: out.txt}
    """
)


def test_cwltool_runs_a_tool_through_xrunner(home, busybox_image, engine_name, tmp_path):
    (tmp_path / "echo.cwl").write_text(TOOL)
    # Prefer the interpreter's own bin directory: shutil.which("xrunner") can resolve to an
    # unrelated program of the same name earlier on PATH (seen on a host that also has an
    # unrelated program installed system-wide). Only fall back to PATH search if
    # this venv has no xrunner script of its own (e.g. running against a non-editable install
    # laid out differently).
    xrunner = os.path.join(os.path.dirname(sys.executable), "xrunner")
    if not os.path.exists(xrunner):
        xrunner = shutil.which("xrunner")
    assert xrunner and os.path.exists(xrunner), "install the package so the xrunner script exists"
    env = {**os.environ, "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(
        ["cwltool", "--user-space-docker-cmd", xrunner, "--outdir", str(tmp_path / "out"),
         str(tmp_path / "echo.cwl"), "--message", "hello from cwl"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert open(result["out"]["path"]).read().strip() == "hello from cwl"
