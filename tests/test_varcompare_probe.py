"""Drive scripts/runbook_varcompare_probe.py with fakes.

The LIVE run + committed var_compare_evidence.json pin the network
path (create/fire/cleanup against the trial); these tests pin the
probe's own mechanics - build_leg criterion mutation, the three-leg
loop, cleanup, and the verdict classifier - without touching Halo.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "varcmp_probe", REPO / "scripts" / "runbook_varcompare_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        if method == "POST" and path == "/Webhook":
            n = sum(1 for m, _ in self.calls if m == "POST")
            return [{"id": f"00000000-0000-4000-8000-00000000000{n}"}]
        return {}


class _FakeHalo:
    requester = _FakeRequester()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeRequester:
        type(self).requester = _FakeRequester()
        return type(self).requester

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def test_probe_leg_loop_verdict_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(probe, "REPO", tmp_path)
    monkeypatch.setattr(
        probe,
        "load_profile",
        lambda _n: SimpleNamespace(tenant_url="https://clidev.trial.usehalo.com"),
    )
    monkeypatch.setattr(probe, "HaloClient", _FakeHalo)

    async def fake_fire(client: Any, wid: str, inputs: dict[str, str]) -> dict[str, Any]:
        # the REAL outcome shape: control met (3 steps), mutated legs notmet (1)
        steps = 3 if inputs.get("a") == "needle" else 1
        return {
            "runlog": {"id": int(wid[-1]), "status": 2, "steps_executed": steps},
            "evolution": [],
            "trigger_fire": {"ok": True, "formCollection": []},
        }

    async def fake_gone(client: Any, label: str, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "var_compare_evidence.json").read_text(encoding="utf-8"))
    assert ev["tenant"] == "clidev.trial.usehalo.com"
    assert set(ev["legs"]) == {"control", "met", "notmet"}
    assert ev["legs"]["control"]["runlog"]["steps_executed"] == 3
    assert ev["legs"]["met"]["runlog"]["steps_executed"] == 1
    assert "DOES NOT RESOLVE" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * 3

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, _ in calls if m == "POST") == 3  # one create per leg
    assert sum(1 for m, _ in calls if m == "DELETE") == 3  # self-cleaning
    out = capsys.readouterr().out
    assert "verdict:" in out and "cleanup:" in out


def test_build_leg_mutates_criterion_and_inputs() -> None:
    doc, crit = probe.build_leg("<<b>>", "x-leg", "x", "y")
    assert doc["name"].endswith("x-leg")
    assert doc["active"] is True and doc["runbook_start_type"] == 1
    assert "_chains" not in doc and "_triggers" not in doc  # sidecars stripped
    cond = next(s for s in doc["steps"] if s.get("step_conditions"))
    assert cond["step_conditions"][0]["value_string"] == "<<b>>"
    assert crit[0]["value_string"] == "<<b>>"
    assert {v["key"]: v["value"] for v in doc["input_variables"]} == {"a": "x", "b": "y"}


def _ev(control: int, met: int, notmet: int) -> dict[str, Any]:
    def leg(steps: int) -> dict[str, Any]:
        return {"runlog": {"steps_executed": steps}}

    return {"legs": {"control": leg(control), "met": leg(met), "notmet": leg(notmet)}}


def test_verdict_classifier() -> None:
    assert "DOES NOT RESOLVE" in probe.verdict(_ev(3, 1, 1))
    assert "RESOLVES" in probe.verdict(_ev(3, 3, 1)) and "DOES NOT" not in probe.verdict(
        _ev(3, 3, 1)
    )
    assert "BROKEN PROBE" in probe.verdict(_ev(1, 1, 1))
    assert "UNEXPECTED" in probe.verdict(_ev(3, 2, 2))
