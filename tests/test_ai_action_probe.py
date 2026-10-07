"""Drive scripts/runbook_ai_action_probe.py with fakes.

The LIVE run + committed ai_action_evidence.json pin the network path;
these tests pin the leg loop, persistence checks, cleanup and the
verdict classifier offline.
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
    "ai_action_probe", REPO / "scripts" / "runbook_ai_action_probe.py"
)
assert _spec is not None
assert _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FIRE_ORDER = ["sql_query", "ai_eval", "ai_agent"]  # leg dict order


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.docs: dict[str, dict] = {}
        self.fires = 0
        self.next_wb = 0
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
            self.next_wb += 1
            wid = f"wb{self.next_wb}"
            self.docs[wid] = (json_body or [{}])[0]
            return [{"id": wid}]
        if method == "POST" and path == "/Tickets":
            body = (json_body or [{}])[0]
            tid = str(9700 + len(self.tickets))
            self.tickets[tid] = {"id": int(tid), "summary": body.get("summary")}
            return [{"id": int(tid), "summary": body.get("summary")}]
        if method == "GET" and path.startswith("/Webhook/"):
            wid = path.rsplit("/", 1)[-1]
            # persistence echo: the server kept exactly what we posted
            return dict(self.docs.get(wid) or {})
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
        # all three legs ran to status2 on the trial
        client.fires += 1
        return {
            "runlog": {"id": client.fires, "status": 2, "steps_executed": 1, "error": ""},
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "ai_action_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == {"sql_query", "ai_eval", "ai_agent"}
    # persistence: what the fake kept is what we posted (aa + edges + aat/ability)
    sql = ev["legs"]["sql_query"]["persisted"]
    assert sql["auto_action"] == 18
    assert sql["edge_types"] == [29, 29]
    assert ev["legs"]["ai_eval"]["persisted"]["auto_action_type"] == 44
    assert ev["legs"]["ai_eval"]["persisted"]["ai_ability_id"] == probe.AI_EVAL_ABILITY
    assert ev["legs"]["ai_agent"]["persisted"]["edge_types"] == [37, 37]
    assert "SHIP: aa18 SQL ran" in ev["verdict"]
    assert "SHIP: aa25 ability ran" in ev["verdict"]
    assert "SHIP: aa26 ability ran" in ev["verdict"]
    assert ev["cleanup"] == ["deleted (clean)"] * 3
    assert "deleted 1 probe ticket" in ev["ticket_cleanup"]

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/Webhook") == 3
    assert "verdict:" in capsys.readouterr().out


def test_verdict_degrades_honestly() -> None:
    empty: dict[str, Any] = {"legs": {}}
    v = probe.verdict(empty)
    assert "aa18 NOT run" in v
    assert "aa25 not run" in v
    assert "aa26 not run" in v
