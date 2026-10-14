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


def test_cwltool_runs_a_tool_through_xcrunner(home, busybox_image, engine_name, tmp_path):
    (tmp_path / "echo.cwl").write_text(TOOL)
    # Prefer the interpreter's own bin directory: shutil.which("xcrunner") can resolve to an
    # unrelated program of the same name earlier on PATH (seen on a host that also has an
    # unrelated program installed system-wide). Only fall back to PATH search if
    # this venv has no xcrunner script of its own (e.g. running against a non-editable install
    # laid out differently).
    xcrunner = os.path.join(os.path.dirname(sys.executable), "xcrunner")
    if not os.path.exists(xcrunner):
        xcrunner = shutil.which("xcrunner")
    assert xcrunner and os.path.exists(xcrunner), "install the package so the xcrunner script exists"
    env = {**os.environ, "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(
        ["cwltool", "--user-space-docker-cmd", xcrunner, "--outdir", str(tmp_path / "out"),
         str(tmp_path / "echo.cwl"), "--message", "hello from cwl"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert open(result["out"]["path"]).read().strip() == "hello from cwl"


STAGE_TOOL = textwrap.dedent(
    """\
    cwlVersion: v1.2
    class: CommandLineTool
    requirements:
      DockerRequirement:
        dockerPull: xcodon-test/busybox:latest
      InitialWorkDirRequirement:
        listing:
          - '$(inputs.fastq_files)'
    inputs:
      fastq_files:
        type: File[]
    baseCommand: [sh, -c, 'cat *_R1_001.fastq']
    stdout: out.txt
    outputs:
      out:
        type: File
        outputBinding: {glob: out.txt}
    """
)


def test_staged_inputs_reached_through_a_symlinked_folder_are_in_the_workdir(
        home, busybox_image, engine_name, tmp_path):
    """The Cell Ranger ARC wrapper stages its FASTQs into the working directory. They sat
    under `inputs/data`, a link to a shared dataset: cwltool passed the path through the link,
    the bind was skipped inside the container, and cellranger-arc found no FASTQs."""
    (tmp_path / "shared" / "raw").mkdir(parents=True)
    (tmp_path / "shared" / "raw" / "SRR1_1.fastq").write_text("@read1\n")
    (tmp_path / "project" / "inputs").mkdir(parents=True)
    (tmp_path / "project" / "inputs" / "data").symlink_to(tmp_path / "shared")
    (tmp_path / "project" / "workspace").mkdir()
    fastq = tmp_path / "project" / "workspace" / "S_S1_L001_R1_001.fastq"
    fastq.symlink_to(tmp_path / "project" / "inputs" / "data" / "raw" / "SRR1_1.fastq")
    (tmp_path / "stage.cwl").write_text(STAGE_TOOL)
    job = {"fastq_files": [{"class": "File", "path": str(fastq)}]}
    (tmp_path / "job.json").write_text(json.dumps(job))
    xcrunner = os.path.join(os.path.dirname(sys.executable), "xcrunner")
    if not os.path.exists(xcrunner):
        xcrunner = shutil.which("xcrunner")
    env = {**os.environ, "XCODON_RUNTIME_HOME": str(home.path), "XCODON_ENGINE": engine_name}
    r = subprocess.run(
        ["cwltool", "--user-space-docker-cmd", xcrunner, "--outdir", str(tmp_path / "out"),
         str(tmp_path / "stage.cwl"), str(tmp_path / "job.json")],
        capture_output=True, text=True, env=env, timeout=600,
    )
    assert r.returncode == 0, r.stderr
    assert open(json.loads(r.stdout)["out"]["path"]).read() == "@read1\n"
