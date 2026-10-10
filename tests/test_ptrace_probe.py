"""Hosts that block ptrace: PRoot cannot run there, so say so instead of failing inside PRoot."""

import os
import shutil
import subprocess
import sys

import pytest

from xcodon_runtime import engine as eng
from xcodon_runtime.api import Runtime
from xcodon_runtime.errors import EngineUnavailable
from xcodon_runtime.probe import PTRACE_PROBE, run_probes


def _probes(ns_ok: bool, ptrace_ok: bool, ptrace_error: str = "ptrace: blocked") -> dict:
    p = {n: {"ok": ns_ok, "error": "" if ns_ok else f"{n}: denied"} for n in eng.PROBE_NAMES}
    p[PTRACE_PROBE] = {"ok": ptrace_ok, "error": "" if ptrace_ok else ptrace_error}
    return p


def test_the_ptrace_probe_runs_with_the_others(home):
    assert PTRACE_PROBE == "ptrace" and PTRACE_PROBE not in eng.PROBE_NAMES
    assert PTRACE_PROBE in run_probes(home.path)


def _ptrace_allowed_here() -> bool:
    try:
        scope = int(open("/proc/sys/kernel/yama/ptrace_scope").read())
    except OSError:
        scope = 0
    status = open("/proc/self/status").read()
    return scope <= 1 and "Seccomp:\t2" not in status


@pytest.mark.skipif(not _ptrace_allowed_here(), reason="this host blocks ptrace")
def test_the_ptrace_probe_passes_where_ptrace_works(home):
    assert run_probes(home.path)[PTRACE_PROBE] == {"ok": True, "error": ""}


def _systemd_run_works() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    r = subprocess.run(["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--", "true"],
                       capture_output=True, timeout=60)
    return r.returncode == 0


@pytest.mark.skipif(not _systemd_run_works(), reason="needs systemd-run --user to apply a seccomp filter")
def test_the_ptrace_probe_fails_and_names_seccomp_when_a_filter_blocks_ptrace(home):
    r = subprocess.run(
        ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "-p", "SystemCallFilter=~ptrace",
         "-p", "SystemCallErrorNumber=EPERM", "--", sys.executable, "-m", "xcodon_runtime.probe",
         PTRACE_PROBE, str(home.path), str(home.containers)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert r.returncode != 0
    assert "Operation not permitted" in r.stderr and "seccomp" in r.stderr


def test_ns_failing_with_ptrace_working_still_picks_proot(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: _probes(ns_ok=False, ptrace_ok=True))
    assert eng.select_engine(home).name == "proot"


def test_no_engine_when_both_are_blocked(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: _probes(False, False, "ptrace: seccomp filter"))
    choice = eng.select_engine(home)
    assert choice.name == eng.NO_ENGINE == "none"
    assert "user namespaces" in choice.reason and "seccomp filter" in choice.reason


def test_the_same_error_from_every_ns_probe_appears_once(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    same = {n: {"ok": False, "error": f"{n}: unshare: Operation not permitted"} for n in eng.PROBE_NAMES}
    same[PTRACE_PROBE] = {"ok": False, "error": "ptrace: ptrace(PTRACE_TRACEME) failed: Operation not permitted"}
    monkeypatch.setattr(eng, "run_probes", lambda *a: same)
    reason = eng.select_engine(home).reason
    assert reason.count("unshare: Operation not permitted") == 1
    assert "userns: " not in reason and "ptrace: ptrace: " not in reason


def test_ns_working_ignores_a_ptrace_failure(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: _probes(ns_ok=True, ptrace_ok=False))
    assert eng.select_engine(home).name == "ns"


def test_create_fails_clearly_and_info_still_reports(home, busybox_image, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: _probes(False, False, "ptrace: seccomp filter"))
    monkeypatch.setattr("xcodon_runtime.api.select_engine", eng.select_engine)
    rt = Runtime(home.path)
    with pytest.raises(EngineUnavailable, match="no engine can run on this host"):
        rt.create("xcodon-test/busybox", command=["/bin/true"])
    info = rt.info()
    assert info["engine"] == "none" and "seccomp filter" in info["engine_reason"]
    assert info["probes"][PTRACE_PROBE]["ok"] is False


@pytest.mark.proot
def test_proot_settings_from_the_host_reach_proot(home, busybox_image, monkeypatch, tmp_path):
    """PRoot passes its environment on to the guest, so the guest seeing the value shows PRoot got it."""
    from xcodon_runtime.engine_proot import find_proot

    if find_proot() is None:
        pytest.skip("no proot binary")
    monkeypatch.setenv("PROOT_TMP_DIR", str(tmp_path))
    rt = Runtime(home.path, engine="proot")
    out = tmp_path / "stdout.txt"
    with open(out, "wb") as f:
        assert rt.run("xcodon-test/busybox", ["/bin/sh", "-c", "echo $PROOT_TMP_DIR"], rm=True, stdout=f) == 0
    assert out.read_text().strip() == str(tmp_path)
