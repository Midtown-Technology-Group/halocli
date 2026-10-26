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
    """Tickets start Pre Process (37); a POST /Actions with the closing outcome
    moves them to Closed (9), which is what the command must verify by re-reading."""

    def __init__(
        self, *, already_closed: set[int] | None = None, ignore_action: bool = False
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed: set[int] = set(already_closed or ())
        self.ignore_action = ignore_action

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
            payload = body[0]
            # Halo only applies the outcome when the NUMERIC id is sent.
            if not self.ignore_action and payload.get("outcome_id") == 4:
                self.closed.add(payload["ticket_id"])
            return [{"id": 999, "ticket_id": payload["ticket_id"]}]
        if path.startswith("/Tickets/"):
            ticket_id = int(path.rsplit("/", 1)[1])
            return {"id": ticket_id, "status_id": 9 if ticket_id in self.closed else 37}
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
    # the numeric id is resolved on apply; the preview shows that it will be
    assert all("Resolved" in str(b["outcome_id"]) for b in bodies)
    assert all(b["sendemail"] is False for b in bodies)
    assert all(op["path"] == "/Actions" for op in payload["operations"])


def test_apply_submits_one_action_per_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    install_fake(monkeypatch, client)
    result = runner.invoke(app, ["tickets", "close", "10", "11", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["closed"] == 2
    assert payload["outcome_id"] == 4
    assert payload["status_source"] == "outcome"
    assert payload["closed_status"] == "Closed"
    assert all(r["verified"] for r in payload["results"])
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


def test_skips_a_ticket_that_is_already_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake(monkeypatch, FakeClient(already_closed={10}))
    result = runner.invoke(app, ["tickets", "close", "10", "11", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["skipped"] == 1
    assert payload["closed"] == 1


def test_reports_failure_when_the_action_does_not_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An action that leaves the ticket open is a failure, not a success.

    Halo accepts an action carrying a plain `outcome` label and records it without
    applying the status; that is exactly how 25 tickets were reported closed while
    staying in Pre Process.
    """
    install_fake(monkeypatch, FakeClient(ignore_action=True))
    result = runner.invoke(app, ["tickets", "close", "10", "--apply", "--yes"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["failed"] == 1
    assert payload["closed"] == 0
    assert "still status" in payload["results"][0]["error"]


def test_notify_user_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    install_fake(monkeypatch, client)
    result = runner.invoke(app, ["tickets", "close", "10", "--apply", "--yes", "--notify-user"])
    assert result.exit_code == 0, result.output
    # default is internal closure; notifying is an explicit opt-in
    assert json.loads(result.output)["ok"] is True
