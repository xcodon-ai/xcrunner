import pytest

from xcodon_runtime import engine as eng
from xcodon_runtime.errors import EngineUnavailable


def all_ok():
    return {n: {"ok": True, "error": ""} for n in eng.PROBE_NAMES}


def test_ns_when_all_probes_pass(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    monkeypatch.setattr(eng, "run_probes", lambda *a: all_ok())
    assert eng.select_engine(home).name == "ns"


def test_proot_when_a_probe_fails(home, monkeypatch):
    monkeypatch.delenv("XCODON_ENGINE", raising=False)
    probes = all_ok()
    probes["overlay"] = {"ok": False, "error": "overlay: Operation not permitted"}
    monkeypatch.setattr(eng, "run_probes", lambda *a: probes)
    choice = eng.select_engine(home)
    assert choice.name == "proot"
    assert "overlay" in choice.reason


def test_override_env_and_arg(home, monkeypatch):
    monkeypatch.setattr(eng, "run_probes", lambda *a: pytest.fail("probes must not run when overridden"))
    monkeypatch.setenv("XCODON_ENGINE", "proot")
    assert eng.select_engine(home).name == "proot"
    assert eng.select_engine(home, override="ns").name == "ns"
    monkeypatch.setenv("XCODON_ENGINE", "bogus")
    with pytest.raises(EngineUnavailable, match="bogus"):
        eng.select_engine(home)


def test_bind_round_trip():
    b = eng.Bind("/h", "/c", True)
    assert eng.Bind.from_dict(b.to_dict()) == b


@pytest.mark.ns
def test_real_probes_pass_on_ns_capable_host(home):
    from xcodon_runtime.probe import run_probes

    results = run_probes(home.path)
    assert all(r["ok"] for r in results.values()), results
