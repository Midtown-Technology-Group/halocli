"""Drive scripts/runbook_membership_probe.py with fakes.

The LIVE16-leg run + committed membership_evidence.json pin the
network path; these tests pin build_leg's criterion mutation, the leg
loop, cleanup, and the verdict classifier offline.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "membr_probe", REPO / "scripts" / "runbook_membership_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# the real trial outcomes (membership_evidence.json) - the fake fire
# replays them so the test pins the exact shipped verdict
REAL_EXEC = {
    "control": 3,
    "two_eq_or": 1,
    "two_eq_neg": 1,
    "in_list_b": 1,
    "t23_single": 3,
    "t23_csv": 3,
    "t9_any": 1,
    "t9_neg": 1,
    "t23_neg_set": 1,
    "t23_neg_single": 1,
    "t24_notin_true": 3,
    "t24_notin_false": 1,
    "t23_overlap": 1,
    "t24_overlap": 3,
    "t23_arr_true": 1,
    "t23_arr_false": 1,
}


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        if method == "POST" and path == "/Webhook":
            n = sum(1 for m, _ in self.calls if m == "POST")
            return [{"id": f"00000000-0000-4000-8000-000000000{n:03d}"}]
        return {}


class _FakeHalo:
    requester: _FakeRequester = _FakeRequester()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeRequester:
        type(self).requester = _FakeRequester()
        return type(self).requester

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def test_probe_full_leg_loop_and_ship_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(probe, "REPO", tmp_path)
    monkeypatch.setattr(
        probe,
        "load_profile",
        lambda _n: type("P", (), {"tenant_url": "https://clidev.trial.usehalo.com"})(),
    )
    monkeypatch.setattr(probe, "HaloClient", _FakeHalo)

    # fires happen in LEGS order (the main loop) - map by sequence
    order = list(probe.LEGS)
    fire_idx = {"n": 0}

    async def fake_fire(client: Any, wid: str, inputs: dict[str, str]) -> dict[str, Any]:
        leg = order[fire_idx["n"]]
        fire_idx["n"] += 1
        return {
            "runlog": {"id": wid, "status": 2, "steps_executed": REAL_EXEC[leg]},
            "evolution": [],
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, label: str, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "membership_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == set(probe.LEGS)
    assert "SHIP: type23 = in-set membership" in ev["verdict"]
    assert "SHIP: type24 = not-in" in ev["verdict"]
    assert "STRICT comma-set" in ev["verdict"]
    assert "eq rows cannot express a set" in ev["verdict"]
    assert "does NOT match element membership" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * len(probe.LEGS)

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, _ in calls if m == "POST") == len(probe.LEGS)
    assert sum(1 for m, _ in calls if m == "DELETE") == len(probe.LEGS)
    assert "verdict:" in capsys.readouterr().out


def test_build_leg_mutates_rows_field_and_inputs() -> None:
    doc, view = probe.build_leg(
        "scalar", [(23, "eu,us", None), (0, "x", "<<other>>")], "t", {"a": "v"}
    )
    assert doc["name"].endswith("-t")
    assert doc["runbook_start_type"] == 1
    assert "_chains" not in doc
    rows = next(s for s in doc["steps"] if s.get("step_conditions"))["step_conditions"]
    assert [r["type"] for r in rows] == [23, 0]
    assert rows[0]["value_string"] == "eu,us"
    assert rows[1]["fieldname"] == "<<other>>"  # field override (array legs)
    assert view[0]["value_string"] == "eu,us"
    assert next(v for v in doc["input_variables"] if v["key"] == "a")["value"] == "v"


def test_verdict_flags_broken_control_and_always_met() -> None:
    def ev(**overrides: Any) -> dict[str, Any]:
        table = dict(REAL_EXEC)
        table.update(overrides)
        return {"legs": {k: {"runlog": {"steps_executed": v}} for k, v in table.items()}}

    assert "BROKEN PROBE" in probe.verdict(ev(control=1))
    full = probe.verdict(ev(t23_neg_set=3))
    assert "ALWAYS MET" in full  # negative legs distinguish real membership
    assert "SHIP: type23" not in full
