"""Drive scripts/runbook_errorpath_probe.py with fakes.

The LIVE run + committed errorpath_evidence.json pin the network path;
these tests pin errorpath_runbook's graph shape, the leg loop,
cleanup and the verdict classifier offline.
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
    "errorpath_probe", REPO / "scripts" / "runbook_errorpath_probe.py"
)
assert _spec is not None
assert _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# the real trial outcomes (errorpath_evidence.json), in fire order
FIRE_ORDER = list(probe.LEGS)
REAL_RUNLOG = {
    "try_ok": {"status": 2, "steps_executed": 3, "runbook_step": 5, "error": ""},
    "try_fail": {"status": 2, "steps_executed": 2, "runbook_step": 5, "error": ""},
    "control_fail": {
        "status": 1,
        "steps_executed": 1,
        "runbook_step": 1,
        "error": "Failed result reached. Last step=6",
    },
    "req_interp": {
        "status": 1,
        "steps_executed": 1,
        "runbook_step": 1,
        "error": "Failed result reached. Last step=4",
    },
}


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.next_wb = 0
        self.tickets: dict[str, dict] = {"9000": {"id": 9000, "summary": "update target"}}

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
        if method == "POST" and path == "/Tickets":
            body = (json_body or [{}])[0]
            tid = str(9500 + len(self.tickets))
            self.tickets[tid] = {"id": int(tid), "summary": body.get("summary")}
            return [{"id": int(tid), "summary": body.get("summary")}]
        if method == "GET" and path == "/Tickets" and params:
            return [{"id": t["id"], "summary": t["summary"]} for t in self.tickets.values()]
        if method == "GET" and path.startswith("/Tickets/"):
            return dict(self.tickets.get(path.rsplit("/", 1)[-1]) or {})
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


def test_probe_leg_loop_and_verdict(
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
            "runlog": {"id": client.fires, **REAL_RUNLOG[leg]},
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "errorpath_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == set(probe.LEGS)
    assert "success path runs the normal hops" in ev["verdict"]
    assert "SHIP: Unsuccessful edge routes to a recovery hop" in ev["verdict"]
    assert "control: edge2 -> Fail" in ev["verdict"]
    assert "run failed (status=1)" in ev["verdict"]  # request interp negative
    assert ev["cleanup"] == ["deleted (clean)"] * len(probe.LEGS)
    assert "deleted 1 probe ticket" in ev["ticket_cleanup"]

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/Webhook") == len(probe.LEGS)
    assert "verdict:" in capsys.readouterr().out


def test_errorpath_runbook_shapes() -> None:
    ok = probe.errorpath_runbook("t", probe.LEGS["try_ok"], 42)
    assert ok["steps"][0]["auto_action_type"] == 2
    write, n1, n2, recovered, success = ok["steps"]
    # success path: write -> n1 -> n2 -> Success (recovery skipped)
    assert [(a["action_name"], a["end_step"]) for a in write["actions"]] == [
        ("Successful", 2),
        ("Unsuccessful", 4),
    ]
    assert n2["actions"][0]["end_step"] == 5  # converges past the handler
    assert recovered["actions"][0]["end_step"] == 5
    # every hop is an aa21 sequencing step (the shape the engine needs -
    # hop steps without auto_action get "Next step not found")
    for hop in (n1, n2, recovered):
        assert hop["auto_action"] == 21
        assert hop["duration"] == 0
    # TARGET substituted with the real ticket id
    assert json.loads(write["message"])["id"] == 42

    ctl = probe.errorpath_runbook("t", probe.LEGS["control_fail"], None)
    assert ctl["steps"][0]["actions"][1]["end_step"] == 6  # edge2 -> Fail
    assert ctl["steps"][-1]["auto_action"] == 1

    req = probe.errorpath_runbook("t", probe.LEGS["req_interp"], None)
    assert len(req["steps"]) == 3  # plain one-step v15 shape


def test_verdict_degrades_honestly() -> None:
    empty = {"legs": {k: {} for k in probe.LEGS}}
    assert "BROKEN PROBE" in probe.verdict(empty)
