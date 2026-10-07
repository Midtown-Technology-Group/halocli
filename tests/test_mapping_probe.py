"""Fake-driven tests for scripts/runbook_mapping_probe.py (the live run is committed)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "mapping_probe", REPO / "scripts" / "runbook_mapping_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FIRE_ORDER = ["control", "map_whole", "map_path"]
REAL_EXEC = {"control": 1, "map_whole": 3, "map_path": 3}


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.tickets: dict[str, dict] = {}

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, path))
        if method == "POST" and path == "/Webhook":
            return [{"id": f"wb{len(self.calls)}"}]
        if method == "POST" and path == "/Tickets":
            body = (json_body or [{}])[0]
            tid = str(9600 + len(self.tickets))
            self.tickets[tid] = {"id": int(tid), "summary": body.get("summary")}
            return [{"id": int(tid), "summary": body.get("summary")}]
        if method == "DELETE":
            return {}
        raise AssertionError(f"unexpected {method} {path}")


class _FakeHalo:
    requester: _FakeRequester = _FakeRequester()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeRequester:
        type(self).requester = _FakeRequester()
        return type(self).requester

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def test_probe_leg_loop_and_ship_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(probe.ph, "REPO", tmp_path)
    monkeypatch.setattr(
        probe.ph,
        "load_profile",
        lambda _n: type("P", (), {"tenant_url": "https://clidev.trial.usehalo.com"})(),
    )
    monkeypatch.setattr(probe.ph, "HaloClient", _FakeHalo)

    async def fake_fire(client: Any, wid: str, inputs: dict[str, str]) -> dict[str, Any]:
        leg = FIRE_ORDER[client.fires]
        client.fires += 1
        return {
            "runlog": {
                "id": client.fires,
                "status": 2,
                "steps_executed": REAL_EXEC[leg],
                "error": "",
            },
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "mapping_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == {"control", "map_whole", "map_path"}
    assert "SHIP: <<response>> mapping materializes" in ev["verdict"]
    assert "SHIP: <<response^summary>> path mapping materializes" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * 3
    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/Webhook") == 3
    assert "verdict:" in capsys.readouterr().out


def test_condition_doc_mutates_field() -> None:
    doc = probe.build_condition_doc("x", {"label": "v"})
    cond = next(s for s in doc["steps"] if s.get("step_conditions"))
    assert cond["step_conditions"][0]["fieldname"] == "<<probe_var>>"


def test_verdict_flags_broken_control() -> None:
    assert "BROKEN PROBE" in probe.verdict({"legs": {}})
