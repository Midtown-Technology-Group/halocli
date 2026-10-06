"""Drive scripts/runbook_ticket_crud_probe.py with fakes.

The LIVE run + committed ticket_crud_evidence.json pin the network
path; these tests pin aa8_runbook's step shape, the leg loop with
readbacks, cleanup, and the verdict classifier offline.
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
    "crud_probe", REPO / "scripts" / "runbook_ticket_crud_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.updated = False
        self.tickets: dict[str, dict] = {
            "9000": {"id": 9000, "summary": "note target"},
            "9001": {"id": 9001, "summary": "update target"},
        }
        self.next_wb = 0

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
            tid = path.rsplit("/", 1)[-1]
            t = dict(self.tickets.get(tid) or {"id": int(tid)})
            if self.updated and str(t.get("summary") or "").endswith("update target"):
                t["summary"] = probe.AAT2_MARKER
            return t
        if method == "GET" and path == "/Actions":
            return [{"actiontype": "note", "action_text": probe.AAT3_MARKER}]
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


def test_probe_all_four_legs_and_ship_verdict(
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
        client.fires += 1
        n = client.fires
        if n == 2:  # aat1 leg ran: the created ticket shows up in lists
            client.tickets["2945"] = {"id": 2945, "summary": probe.AAT1_MARKER}
        if n == 3:  # aat2 leg ran: target summary changes
            client.updated = True
        if n == 4:  # bad id -> Unsuccessful edge -> Fail terminal
            return {
                "runlog": {
                    "id": 4,
                    "status": 1,
                    "steps_executed": 1,
                    "runbook_step": 1,
                    "error": "Failed result reached. Last step=3",
                },
                "evolution": [],
                "trigger_fire": {"ok": True},
            }
        return {
            "runlog": {"id": n, "status": 2, "steps_executed": 1, "runbook_step": 2, "error": ""},
            "evolution": [],
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, label: str, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "ticket_crud_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == {"aat3_lit", "aat1_create", "aat2_update", "aat2_bad"}
    assert "SHIP: aat3 note with literal id" in ev["verdict"]
    assert "SHIP: aat1 create" in ev["verdict"]
    assert "SHIP: aat2 update" in ev["verdict"]
    assert "bad id routes the Unsuccessful edge" in ev["verdict"]
    assert ev["legs"]["aat1_create"]["created_id"] == "2945"
    assert ev["cleanup"] == ["deleted (clean)"] * 4
    assert "deleted 3 probe ticket" in ev["ticket_cleanup"]

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, _ in calls if m == "POST" and _ == "/Webhook") == 4
    assert sum(1 for m, _ in calls if m == "DELETE" and _.startswith("/Tickets")) == 3
    assert "verdict:" in capsys.readouterr().out


def test_aa8_runbook_shape() -> None:
    doc = probe.aa8_runbook("t", 2, '{"id": 1}')
    assert doc["type"] == 1 and doc["active"] is True
    assert doc["runbook_start_type"] == 1
    step, success, fail = doc["steps"]
    assert step["auto_action"] == 8 and step["auto_action_type"] == 2
    assert step["isstart"] is True
    assert [(a["action_name"], a["approval_result"], a["end_step"]) for a in step["actions"]] == [
        ("Successful", 1, 2),
        ("Unsuccessful", 0, 3),
    ]
    assert success["isend"] is True and "auto_action" not in success
    assert fail["auto_action"] == 1 and fail["isend"] is True


def test_verdict_flags_missing_pieces() -> None:
    good = {
        "legs": {
            "aat3_lit": {"action_marker_found": True},
            "aat1_create": {"created_id": "1"},
            "aat2_update": {"updated": True},
            "aat2_bad": {"took_failure_path": True},
        }
    }
    assert probe.verdict(good).count("SHIP") == 3
    bad = {
        "legs": {
            "aat3_lit": {},
            "aat1_create": {},
            "aat2_update": {},
            "aat2_bad": {},
        }
    }
    v = probe.verdict(bad)
    assert "NOT found" in v and "NOT observed" in v and "did NOT route" in v
