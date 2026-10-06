"""Drive scripts/runbook_intbool_probe.py with fakes.

The LIVE16-leg run + committed intbool_guard_evidence.json pin the
network path; these tests pin build_leg's row-override merging, the
leg loop, cleanup, and the verdict classifier offline.
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
    "intbool_probe", REPO / "scripts" / "runbook_intbool_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# the real trial outcomes (intbool_guard_evidence.json)
REAL_EXEC = {
    "int_ctrl": 3,
    "float_ctrl": 3,
    "bool_ctrl": 3,
    "intA_in": 3,
    "intA_out": 1,
    "intB_in": 3,
    "intB_out": 1,
    "int_overlap": 1,
    "fltA_in": 1,
    "fltA_out": 1,
    "fltB_in": 3,
    "fltB_out": 1,
    "bool_t_10": 3,
    "bool_f_10": 1,
    "bool_t_TF": 1,
    "bool_f_TF": 1,
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

    ev = json.loads((tmp_path / "intbool_guard_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == set(probe.LEGS)
    assert "SHIP: int sets type23 with base value_type int" in ev["verdict"]
    assert "STRICT comma-set" in ev["verdict"]
    assert "SHIP: float sets type23 with value_type string" in ev["verdict"]
    assert "SHIP: bool bare truthiness = criteria5 >0 with inputs 1/0" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * len(probe.LEGS)

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, _ in calls if m == "POST") == len(probe.LEGS)
    assert sum(1 for m, _ in calls if m == "DELETE") == len(probe.LEGS)
    assert "verdict:" in capsys.readouterr().out


def test_build_leg_merges_row_overrides_and_inputs() -> None:
    doc, view = probe.build_leg(
        "int",
        [{"type": 23, "value_int": 0, "value_string": "1,2"}, {"fieldname": "<<other>>"}],
        "t",
        {"level": "2"},
    )
    assert doc["name"].endswith("-t")
    assert doc["active"] is True
    assert "_chains" not in doc
    rows = next(s for s in doc["steps"] if s.get("step_conditions"))["step_conditions"]
    assert [r["type"] for r in rows] == [23, 0]
    assert rows[0]["value_string"] == "1,2"
    assert rows[0]["value_int"] == 0
    assert rows[1]["fieldname"] == "<<other>>"  # field override (bool legs)
    assert rows[0]["value_type"] == "int"  # inherited from the int-eq base
    assert view[0]["value_string"] == "1,2"
    assert next(v for v in doc["input_variables"] if v["key"] == "level")["value"] == "2"


def test_verdict_flags_broken_controls_and_unproven_variants() -> None:
    def ev(**overrides: Any) -> dict[str, Any]:
        table = dict(REAL_EXEC)
        table.update(overrides)
        return {"legs": {k: {"runlog": {"steps_executed": v}} for k, v in table.items()}}

    assert "BROKEN PROBE" in probe.verdict(ev(int_ctrl=1))
    no_bool = probe.verdict(ev(bool_t_10=1))
    assert "bool truthiness UNPROVEN" in no_bool
    no_flt = probe.verdict(ev(fltB_in=1))
    assert "float set membership UNPROVEN" in no_flt
    substring = probe.verdict(ev(int_overlap=3))
    assert "SUBSTRING" in substring
