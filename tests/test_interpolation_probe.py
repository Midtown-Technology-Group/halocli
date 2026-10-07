"""Drive scripts/runbook_interpolation_probe.py with fakes.

The LIVE run + committed interpolation_evidence.json pin the network
path; these tests pin the leg loop (aa8 + condition legs), readbacks,
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
    "interp_probe", REPO / "scripts" / "runbook_interpolation_probe.py"
)
assert _spec is not None
assert _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# the real trial outcomes (interpolation_evidence.json), in fire order
FIRE_ORDER = list(probe.AA8) + list(probe.COND)
REAL_EXEC = {
    "aat1_var": (2, 1, ""),  # (status, steps_executed, error)
    "aat1_missing": (1, 1, "Failed result reached. Last step=3"),
    "aat3_var": (2, 1, ""),
    "bool_eq_t": (2, 3, ""),
    "bool_eq_f": (2, 1, ""),
    "boolset_t": (2, 3, ""),
    "boolset_f": (2, 1, ""),
    "boolset0_f": (2, 3, ""),
    "boolset0_t": (2, 1, ""),
    "boolset_capital": (2, 1, ""),
}


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.tickets: dict[str, dict] = {
            "9000": {"id": 9000, "summary": "interp note target"},
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
            return dict(self.tickets.get(path.rsplit("/", 1)[-1]) or {})
        if method == "GET" and path == "/Actions":
            return [{"actiontype": "note", "action_text": probe.NOTE_IN}]
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


def test_probe_full_leg_loop_and_ship_verdict(
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
        status, steps, error = REAL_EXEC[leg]
        if leg == "aat1_var":  # the created ticket shows up in lists
            client.tickets["9100"] = {"id": 9100, "summary": probe.SUBJECT_IN}
        return {
            "runlog": {
                "id": client.fires,
                "status": status,
                "steps_executed": steps,
                "runbook_step": 2 if status == 2 else 1,
                "error": error,
            },
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "interpolation_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == set(probe.LEGS)
    assert "SHIP: <<input>> vars interpolate in aa8 bodies" in ev["verdict"]
    assert "unresolved token fails the step" in ev["verdict"]
    assert "SHIP: dynamic note text via <<input>>" in ev["verdict"]
    assert "SHIP: flag == True -> eq value_int1" in ev["verdict"]
    assert "encodes as '1'" in ev["verdict"]
    assert "encodes False as '0'" in ev["verdict"]
    assert "'True' spelling fails" in ev["verdict"]
    assert ev["legs"]["aat1_var"]["created_summary"] == probe.SUBJECT_IN
    assert ev["cleanup"] == ["deleted (clean)"] * len(probe.LEGS)
    assert "deleted 2 probe ticket" in ev["ticket_cleanup"]

    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/Webhook") == len(probe.LEGS)
    assert "verdict:" in capsys.readouterr().out


def test_verdict_degrades_honestly() -> None:
    empty = {"legs": {k: {} for k in probe.LEGS}}
    v = probe.verdict(empty)
    assert "BROKEN PROBE" in v  # aat1_var exec missing
    partial = {
        "legs": {
            **{k: {"runlog": {"steps_executed": 1}} for k in probe.LEGS},
            "aat1_var": {"runlog": {"steps_executed": 1}},
        }
    }
    v = probe.verdict(partial)
    assert "NOT observed" in v or "NOT pinned" in v
