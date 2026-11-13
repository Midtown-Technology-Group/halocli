"""Backend service monitoring snapshot: normalization, ranking, and failure modes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner as ClickCliRunner
from typer.main import get_command

from halocli.cli import app
from halocli.errors import HaloCLIError
from halocli.monitoring import (
    STREAM_BY_NAME,
    collect_snapshot,
    issues_only,
    parse_since,
    parse_time,
)


NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)


def _iso(offset_minutes: int = 0) -> str:
    return (NOW - timedelta(minutes=offset_minutes)).isoformat()


class FakeClient:
    """Canned per-path responses; an exception value is raised instead."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method: str, path: str, *, params=None, **kwargs):
        self.calls.append((path, dict(params or {})))
        value = self.responses.get(path, [] if method == "GET" else None)
        if isinstance(value, Exception):
            raise value
        return value


def _healthy_tasks() -> dict:
    return {
        "automations": [],
        "bulkemails": [],
        "escmsgs": [],
        "events": [],
        "outgoings": [],
        "scheduleoccurences": [],
        "schedules": [],
        "lastdailytaskrun": _iso(),
        "timestamp": _iso(),
    }


def _all_streams(**overrides) -> dict[str, object]:
    responses: dict[str, object] = {
        "/TaskMonitorEvent": HaloCLIError(
            "HaloPSA server error (500) on /api/TaskMonitorEvent",
            category="server",
            status_code=500,
        ),
        "/IntegrationError": {"errors": [], "record_count": 0},
        "/IntegrationRequest": {"requests": [], "record_count": 0},
        "/IntegratorLog": {"results": [], "record_count": 0},
        "/ScheduleOccurrence": {"occurrences": [], "record_count": 0},
        "/Outgoingemail": [],
        "/NotificationLog": [],
        "/BulkEmail": [],
        "/MailCampaignLog": {"mailcampaignlog": [], "record_count": 0},
        "/TaskSchedule": _healthy_tasks(),
        "/Health": {
            "inuse_worker_threads": 2,
            "available_worker_threads": 100,
            "max_worker_threads": 32767,
        },
    }
    responses.update(overrides)
    return responses


def _run(responses: dict[str, object], **kwargs):
    return asyncio.run(collect_snapshot(FakeClient(responses), now=NOW, **kwargs))


def test_parse_since_units_and_invalid() -> None:
    assert parse_since("all") is None
    assert parse_since("0") is None
    assert parse_since("90m") == timedelta(minutes=90)
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("7d") == timedelta(days=7)
    for bad in ("24", "h", "-1h", "nanh", "1w"):
        with pytest.raises(ValueError):
            parse_since(bad)


def test_parse_time_handles_halo_shapes() -> None:
    assert parse_time("2026-11-13T03:42:33.587").tzinfo is not None
    assert parse_time("2026-11-13T03:42:33Z") == datetime(
        2026, 11, 13, 3, 42, 33, tzinfo=timezone.utc
    )
    assert parse_time("") is None
    assert parse_time("not a date") is None


def test_snapshot_classifies_each_failure_stream() -> None:
    responses = _all_streams(
        **{
            "/IntegrationError": {
                "errors": [
                    {
                        "id": 11,
                        "date": _iso(),
                        "module_name": "NinjaOne",
                        "entity_type": "asset",
                        "entity_id": 9,
                        "entity_name": "PC-1",
                        "error": "sync exploded",
                    }
                ],
                "record_count": 1,
            },
            "/IntegrationRequest": {
                "requests": [
                    {
                        "id": 12,
                        "datelogged": _iso(),
                        "isoutgoing": True,
                        "operation": "delete invoice",
                        "type": "Xero",
                        "url": "https://xero.example/invoice/1",
                        "resultcode": 500,
                        "response_body": "",
                    }
                ],
                "record_count": 1,
            },
            "/ScheduleOccurrence": {
                "occurrences": [
                    {
                        "id": 13,
                        "creation_date": _iso(),
                        "description": "Daily backup ticket",
                        "schedule_id": 7,
                        "error": "creation failed",
                        "retry_attempts_remaining": 0,
                    }
                ],
                "record_count": 1,
            },
            "/Outgoingemail": [
                {
                    "id": 14,
                    "datesent": _iso(),
                    "to": "customer@example.com",
                    "subject": "Ticket update",
                    "error": "SMTP 550",
                }
            ],
        }
    )
    snapshot = _run(responses, since=None)

    assert snapshot["ok"] is True
    by_stream = {issue["stream"]: issue for issue in snapshot["issues"]}
    assert by_stream["integration-errors"]["severity"] == "high"
    assert "NinjaOne" in by_stream["integration-errors"]["title"]
    assert by_stream["integration-requests"]["severity"] == "high"
    assert by_stream["schedule-occurrences"]["severity"] == "critical"
    assert by_stream["outgoing-emails"]["severity"] == "medium"
    # Stable dedupe keys: same entity, same key, across runs.
    again = _run(responses, since=None)
    assert [i["key"] for i in snapshot["issues"]] == [i["key"] for i in again["issues"]]
    # Every issue points at one of the declared streams.
    assert all(issue["stream"] in STREAM_BY_NAME for issue in snapshot["issues"])


def test_successful_rows_are_not_issues() -> None:
    responses = _all_streams(
        **{
            "/IntegrationRequest": {
                "requests": [
                    {
                        "id": 20,
                        "datelogged": _iso(),
                        "resultcode": 200,
                        "operation": "sync",
                        "url": "https://ok.example",
                    }
                ],
                "record_count": 1,
            },
            "/ScheduleOccurrence": {
                "occurrences": [
                    {
                        "id": 21,
                        "creation_date": _iso(),
                        "description": "ok",
                        "error": "",
                        "retry_attempts_remaining": 3,
                    }
                ],
                "record_count": 1,
            },
        }
    )
    snapshot = _run(responses, since=None)
    assert snapshot["issues"] == []
    assert snapshot["counts"]["total"] == 0


def test_repeated_same_failure_groups_into_one_issue() -> None:
    rows = [
        {
            "id": 50 + index,
            "date": _iso(offset_minutes=10 - index),
            "module_name": "MicrosoftCSP",
            "entity_type": "CSP Subscriptions/SKUs",
            "entity_id": 0,
            "entity_name": "CSP Subscriptions/SKUs",
            "error": "same failure",
        }
        for index in range(3)
    ]
    snapshot = _run(
        _all_streams(**{"/IntegrationError": {"errors": rows, "record_count": len(rows)}}),
        since=None,
        streams=["integration-errors"],
    )
    assert len(snapshot["issues"]) == 1
    issue = snapshot["issues"][0]
    assert issue["count"] == 3
    assert issue["first_seen"] < issue["last_seen"]
    assert len(issue["resource_ids"]) == 3
    assert snapshot["counts"]["events"] == 3


def test_optional_stream_500_degrades_to_unsupported() -> None:
    snapshot = _run(_all_streams(), since=None)
    assert snapshot["ok"] is True
    assert snapshot["streams"]["monitoring-events"]["status"] == "unsupported"
    assert snapshot["streams"]["monitoring-events"]["status_code"] == 500
    assert any("unavailable" in warning for warning in snapshot["warnings"])


def test_non_optional_stream_error_is_reported_not_fatal() -> None:
    responses = _all_streams(
        **{"/IntegrationError": HaloCLIError("boom", category="server", status_code=502)}
    )
    snapshot = _run(responses, since=None)
    assert snapshot["ok"] is True
    assert snapshot["streams"]["integration-errors"]["status"] == "error"
    assert any("integration-errors" in warning for warning in snapshot["warnings"])


def test_since_window_drops_old_rows() -> None:
    responses = _all_streams(
        **{
            "/Outgoingemail": [
                {"id": 30, "datesent": _iso(60 * 48), "subject": "old", "error": "old"},
                {"id": 31, "datesent": _iso(5), "subject": "new", "error": "new"},
            ]
        }
    )
    snapshot = _run(responses, since=timedelta(hours=24))
    assert [issue["resource_id"] for issue in snapshot["issues"]] == [31]
    assert snapshot["streams"]["outgoing-emails"]["filtered_out"] == 1


def test_log_streams_request_newest_first() -> None:
    client = FakeClient(_all_streams())
    asyncio.run(
        collect_snapshot(
            client,
            since=None,
            streams=["integration-errors", "integration-requests"],
            now=NOW,
        )
    )
    by_path = {path: params for path, params in client.calls}
    assert by_path["/IntegrationError"]["order"] == "date"
    assert by_path["/IntegrationError"]["orderdesc"] is True
    assert by_path["/IntegrationRequest"]["order"] == "datelogged"


def test_limit_bounds_rows_and_reports_truncation() -> None:
    rows = [
        {"id": 40 + index, "datesent": _iso(), "subject": "s", "error": "e"} for index in range(3)
    ]
    snapshot = _run(_all_streams(**{"/Outgoingemail": rows}), since=None, limit=1)
    assert len(snapshot["issues"]) == 1
    assert snapshot["streams"]["outgoing-emails"]["rows"] == 1
    assert snapshot["streams"]["outgoing-emails"]["truncated"] is True


def test_task_schedule_backlog_and_stale_daily_run() -> None:
    tasks = _healthy_tasks()
    tasks["events"] = [{"id": index} for index in range(30)]
    tasks["lastdailytaskrun"] = _iso(60 * 40)
    snapshot = _run(_all_streams(**{"/TaskSchedule": tasks}), since=None, queue_threshold=25)
    keys = {issue["key"] for issue in snapshot["issues"]}
    assert "task-schedule|backlog:events" in keys
    assert "task-schedule|daily-task-run" in keys


def test_health_saturation_is_critical() -> None:
    snapshot = _run(
        _all_streams(
            **{
                "/Health": {
                    "inuse_worker_threads": 200,
                    "available_worker_threads": 0,
                    "max_worker_threads": 200,
                }
            }
        ),
        since=None,
        streams=["backend-health"],
    )
    assert snapshot["issues"][0]["severity"] == "critical"
    assert snapshot["issues"][0]["stream"] == "backend-health"


def test_issues_only_strips_stream_detail() -> None:
    snapshot = _run(_all_streams(), since=None)
    triage = issues_only(snapshot)
    assert "streams" not in triage
    assert set(triage) == {"ok", "generated_at", "since", "counts", "issues", "warnings"}


def test_monitoring_commands_are_registered() -> None:
    click_app = get_command(app)
    result = ClickCliRunner().invoke(click_app, ["monitoring", "--help"])
    assert result.exit_code == 0
    assert "snapshot" in result.output
    assert "issues" in result.output
    assert "queue" in result.output
