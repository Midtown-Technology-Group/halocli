"""Drive scripts/runbook_aat48_probe.py with fakes.

The LIVE run + committed aat48_evidence.json pin the network path;
these tests pin the leg loop, marker scan, cleanup and the verdict
classifier offline.
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
    "aat48_probe", REPO / "scripts" / "runbook_aat48_probe.py"
)
assert _spec is not None
assert _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FIRE_ORDER = list(probe.LEGS)
REAL_RUNLOG = {
    "aat1_control": {"status": 2, "steps_executed": 1, "error": ""},
    "aat4": {"status": 2, "steps_executed": 1, "error": ""},
    "aat5": {"status": 2, "steps_executed": 1, "error": ""},
    "aat6": {
        "status": 1,
        "steps_executed": 1,
        "error": "Failed result reached. Last step=3",
    },
}


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.next_wb = 0
        self.users: list[dict] = []
        self.tickets: list[dict] = []

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
            self.next_wb += 1
            return [{"id": f"wb{self.next_wb}"}]
        if method == "GET" and path == "/Users":
            return list(self.users)
        if method == "GET" and path == "/Tickets":
            return list(self.tickets)
        if method == "GET" and path in ("/Clients", "/Sites"):
            return []
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


def test_probe_leg_loop_scan_and_verdict(
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
        # the real outcome: aat1 makes a ticket; aat4/5 make users; aat6 fails
        if leg == "aat1_control":
            client.tickets.append({"id": 940, "summary": probe.AAT1_MARKER})
        elif leg == "aat4":
            client.users.append({"id": 94, "name": probe.AAT4_MARKER})
        elif leg == "aat5":
            client.users.append({"id": 95, "name": probe.AAT5_MARKER})
        return {
            "runlog": {"id": client.fires, **REAL_RUNLOG[leg]},
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "aat48_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == set(probe.LEGS)
    assert "control: aat1 created the marker ticket" in ev["verdict"]
    assert "SHIP: aat4 -> /Users id=94" in ev["verdict"]
    assert "SHIP: aat5 -> /Users id=95" in ev["verdict"]
    assert "aat6 created no matching entity" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * len(probe.LEGS)
    # control ticket + two created users all deleted
    assert ev["entity_cleanup"] == "deleted 3 created entit(ies)"

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/Webhook") == len(probe.LEGS)
    deletes = [p for m, p in calls if m == "DELETE" and not p.startswith("/Webhook")]
    assert len(deletes) == 3  # control ticket + two created users
    assert "verdict:" in capsys.readouterr().out


def test_verdict_degrades_honestly() -> None:
    empty: dict[str, Any] = {"legs": {k: {} for k in probe.LEGS}}
    assert "control FAILED" in probe.verdict(empty)
