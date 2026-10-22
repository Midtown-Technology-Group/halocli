"""Bounded JSONL bulk driver: strict parsing, zero-network preview, ordered apply.

The contract under test is the house one: a plan is reviewed before it is applied,
preview performs no network work at all, execution is sequential and a failure stops
the run unless the operator opts into continuing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from halocli.bulk import (
    BulkError,
    BulkItem,
    execute_plan,
    parse_jsonl,
    preview_plan,
)
from halocli.cli import app

runner = CliRunner()


class RecordingClient:
    """Duck-typed stand-in: records calls, optionally fails on a given path."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on = fail_on

    async def raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "params": params, "body": body})
        if self.fail_on is not None and path == self.fail_on:
            raise RuntimeError("tenant said no")
        return {"path": path}


class ExplodingClient(RecordingClient):
    """Any call at all is a test failure: previews must stay offline."""

    async def raw(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - asserted
        raise AssertionError("preview must not touch the network")


PLAN = "\n".join(
    [
        json.dumps({"method": "POST", "path": "/Actions", "body": {"note": "hello"}}),
        "# a comment line is allowed",
        "",
        json.dumps({"method": "GET", "path": "/Tickets", "params": {"take": 5}}),
        json.dumps({"path": "/Tickets/7"}),
    ]
)


def test_parse_accepts_comments_and_defaults_to_get() -> None:
    items = parse_jsonl(PLAN)
    assert [item.method for item in items] == ["POST", "GET", "GET"]
    assert [item.index for item in items] == [0, 1, 2]
    assert items[1].params == {"take": "5"}
    assert items[1].path == "/Tickets"
    assert items[0].write is True and items[2].write is False
    assert items[1].lineno == 4


@pytest.mark.parametrize(
    "line, fragment",
    [
        ('{"method": "HEAD", "path": "/Tickets"}', "not allowed"),
        ('{"method": "GET"}', "'path' is required"),
        ('{"path": "Tickets"}', "must start with '/'"),
        ('["not", "an", "object"]', "JSON object"),
        ('{"path": "/Tickets", "params": {"x": {"nested": 1}}}', "scalar"),
        ('{"path": "/Tickets"', "invalid JSON"),
    ],
)
def test_parse_rejects_unsafe_plans(line: str, fragment: str) -> None:
    with pytest.raises(BulkError) as excinfo:
        parse_jsonl(line, source="plan.jsonl")
    assert fragment in str(excinfo.value)


def test_parse_rejects_an_empty_plan() -> None:
    with pytest.raises(BulkError) as excinfo:
        parse_jsonl("\n# only comments\n")
    assert "no operations found" in str(excinfo.value)


def test_preview_reports_the_plan_without_network() -> None:
    items = parse_jsonl(PLAN)
    payload = preview_plan(items, source="plan.jsonl", limit=2)
    assert payload["apply"] is False
    assert payload["count"] == 3
    assert payload["writes"] == 1
    assert payload["methods"] == {"POST": 1, "GET": 2}
    assert len(payload["operations"]) == 2
    assert payload["truncated"] is True
    assert "--apply --yes" in payload["hint"]
    # Proven offline: the preview path never receives a client at all.
    ExplodingClient()


@pytest.mark.anyio
async def test_execute_applies_in_order() -> None:
    client = RecordingClient()
    payload = await execute_plan(client, parse_jsonl(PLAN))
    assert payload["ok"] is True
    assert payload["applied"] == 3
    assert payload["skipped"] == 0
    assert [call["path"] for call in client.calls] == ["/Actions", "/Tickets", "/Tickets/7"]
    assert client.calls[0]["body"] == {"note": "hello"}


@pytest.mark.anyio
async def test_execute_stops_at_the_first_failure() -> None:
    client = RecordingClient(fail_on="/Tickets")
    payload = await execute_plan(client, parse_jsonl(PLAN))
    assert payload["ok"] is False
    assert payload["applied"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 1
    assert payload["results"][1]["ok"] is False
    assert "remaining operations were not sent" in payload["hint"]
    assert [call["path"] for call in client.calls] == ["/Actions", "/Tickets"]


@pytest.mark.anyio
async def test_execute_can_continue_after_a_failure() -> None:
    client = RecordingClient(fail_on="/Tickets")
    payload = await execute_plan(client, parse_jsonl(PLAN), continue_on_error=True)
    assert payload["applied"] == 2
    assert payload["failed"] == 1
    assert payload["skipped"] == 0
    assert "hint" not in payload


def test_bulk_cli_previews_without_a_profile(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text(PLAN, encoding="utf-8")
    result = runner.invoke(app, ["bulk", "--file", str(plan)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["count"] == 3
    assert payload["writes"] == 1


def test_bulk_cli_refuses_apply_without_yes(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text(PLAN, encoding="utf-8")
    result = runner.invoke(app, ["bulk", "--file", str(plan), "--apply"])
    assert result.exit_code != 0
    assert "--yes" in (result.output + (result.stderr or ""))


def test_bulk_cli_rejects_a_bad_line(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text('{"method": "HEAD", "path": "/Tickets"}\n', encoding="utf-8")
    result = runner.invoke(app, ["bulk", "--file", str(plan)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["category"] == "validation"
    assert "not allowed" in payload["error"]


def test_bulk_item_operation_shape() -> None:
    item = BulkItem(index=0, lineno=3, method="GET", path="/Tickets", params={"a": "b"})
    assert item.as_operation() == {
        "index": 0,
        "lineno": 3,
        "method": "GET",
        "path": "/Tickets",
        "params": {"a": "b"},
    }
