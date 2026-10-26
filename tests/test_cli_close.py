"""`halocli tickets close`: closure goes through a Halo action/outcome.

Halo's own flows never write `status_id` to close a ticket - the outcome carries
the new status, plus the note, agent and audit trail. These tests pin that the
command previews offline, proves the outcome closes before sending anything, and
refuses outcomes that would leave the ticket open.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

import halocli.cli as halocli_cli
from halocli.cli import app

runner = CliRunner()

OUTCOMES = [
    {"id": 7, "outcome": "Internal Note", "newstatus": 0},
    {"id": 4, "outcome": "Resolved", "newstatus": 9},
    {"id": 40, "outcome": "Triage", "newstatus": 37},
]
STATUSES = [
    {"id": 9, "name": "Closed"},
    {"id": 37, "name": "Pre Process"},
]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def raw(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Any:
        self.calls.append((method, path))
        if path == "/Outcome":
            return OUTCOMES
        if path == "/Status":
            return STATUSES
        if path == "/Actions":
            return [{"id": 999, "ticket_id": body[0]["ticket_id"]}]
        raise AssertionError(f"unexpected call {method} {path}")


def install_fake(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(halocli_cli, "HaloClient", lambda *a, **k: client)
    monkeypatch.setattr(halocli_cli, "load_profile", lambda *a, **k: object())


def test_preview_is_offline_and_builds_action_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("preview must not construct a client")

    monkeypatch.setattr(halocli_cli, "HaloClient", boom)
    result = runner.invoke(app, ["tickets", "close", "1,2", "--note", "done"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["outcome"] == "Resolved"
    assert payload["count"] == 2
    bodies = [op["body"][0] for op in payload["operations"]]
    assert [b["ticket_id"] for b in bodies] == [1, 2]
    assert all(b["outcome"] == "Resolved" for b in bodies)
    assert all(op["path"] == "/Actions" for op in payload["operations"])


def test_apply_submits_one_action_per_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    install_fake(monkeypatch, client)
    result = runner.invoke(app, ["tickets", "close", "10", "11", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["applied"] == 2
    assert payload["status_source"] == "outcome"
    assert payload["closed_status"] == "Closed"
    actions = [call for call in client.calls if call[1] == "/Actions"]
    assert len(actions) == 2


def test_refuses_an_outcome_that_does_not_close(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, FakeClient())
    result = runner.invoke(
        app, ["tickets", "close", "10", "--outcome", "Triage", "--apply", "--yes"]
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "not a closed status" in output


def test_refuses_an_outcome_that_sets_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, FakeClient())
    result = runner.invoke(
        app, ["tickets", "close", "10", "--outcome", "Internal Note", "--apply", "--yes"]
    )
    assert result.exit_code == 1
    assert "cannot close a ticket" in (result.output + (result.stderr or ""))


def test_refuses_an_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, FakeClient())
    result = runner.invoke(app, ["tickets", "close", "10", "--outcome", "Nope", "--apply", "--yes"])
    assert result.exit_code == 1
    assert "no outcome named" in (result.output + (result.stderr or ""))


def test_rejects_a_non_numeric_ticket_id() -> None:
    result = runner.invoke(app, ["tickets", "close", "abc"])
    assert result.exit_code != 0
