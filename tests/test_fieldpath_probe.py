"""Fake-driven tests for scripts/runbook_fieldpath_probe.py (live run committed)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fieldpath_probe", REPO / "scripts" / "runbook_fieldpath_probe.py"
)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FIRE_ORDER = ["control", "deref"]
REAL_EXEC = {"control": (2, 2), "deref": (1, 2)}  # leg -> (status, exec)


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fires = 0
        self.tickets: dict[str, dict] = {}
        self.next_method = 300

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
            tid = str(9700 + len(self.tickets))
            self.tickets[tid] = {"id": int(tid), "summary": body.get("summary")}
            return [{"id": int(tid), "summary": body.get("summary")}]
        if method == "POST" and path == "/CustomIntegrationMethod":
            self.next_method += 1
            return [{"id": self.next_method}]
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


def test_probe_legs_verdict_and_cleanup(
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
        status, steps = REAL_EXEC[leg]
        return {
            "runlog": {
                "id": client.fires,
                "status": status,
                "steps_executed": steps,
                "error": "" if status == 2 else "Failed result reached. Last step=6",
            },
            "trigger_fire": {"ok": True},
        }

    async def fake_gone(client: Any, path: str) -> str:
        return "deleted (clean)"

    monkeypatch.setattr(probe.ph.bc, "_fire_runbook", fake_fire)
    monkeypatch.setattr(probe.ph.bc, "_gone", fake_gone)

    assert asyncio.run(probe.main()) == 0

    ev = json.loads((tmp_path / "fieldpath_evidence.json").read_text(encoding="utf-8"))
    assert set(ev["legs"]) == {"control", "deref", "method_paths"}
    assert "PROVEN NON-WORKING: <<var^field>>" in ev["verdict"]
    assert "SHIP: BOTH templated method-path styles create" in ev["verdict"]
    assert "accepted" in ev["legs"]["method_paths"]["brace"]
    assert "accepted" in ev["legs"]["method_paths"]["var"]
    assert "deleted (clean)" in ev["cleanup"]
    assert "2 probe method(s) deleted" in ev["cleanup"]
    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/CustomIntegrationMethod") == 2
    assert (
        sum(1 for m, p in calls if m == "DELETE" and p.startswith("/CustomIntegrationMethod")) == 2
    )
    assert "verdict:" in capsys.readouterr().out


def test_verdict_broken_control() -> None:
    assert "BROKEN PROBE" in probe.verdict({"legs": {}})
