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
    StreamSpec,
    _severity_number,
    classify_document,
    classify_row,
    collect_snapshot,
    group_issues,
    issues_only,
    parse_since,
    parse_time,
)


def core_severity(value):
    return _severity_number(value)


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


def test_optional_stream_classifiers():
    assert core_severity("critical") == "critical"
    assert core_severity("warning") == "high"
    assert core_severity("info") == "low"
    assert core_severity("not-a-number") == "medium"
    # No documented numeric ordering: never guess which end is worse.
    assert core_severity(0) == "medium"
    assert core_severity(2) == "medium"
    assert core_severity(3) == "medium"
    assert core_severity(5) == "medium"
    assert core_severity(True) == "medium"

    monitoring = STREAM_BY_NAME["monitoring-events"]
    issue = classify_row(
        monitoring,
        {"id": 1, "severity": 2, "message": "webhook failed", "log_type": "Event"},
    )
    assert issue is not None
    assert issue["severity"] == "medium"
    assert issue["title"] == "webhook failed"

    integrator = STREAM_BY_NAME["integrator-logs"]
    assert classify_row(integrator, {"id": 1, "status": "ok"}) is None
    # A success row can carry a human message; that is not failure evidence.
    assert (
        classify_row(
            integrator, {"id": 1, "status": "success", "message": "Completed successfully"}
        )
        is None
    )
    failed = classify_row(integrator, {"id": 1, "status": "Failed"})
    assert failed is not None
    assert failed["severity"] == "high"
    assert classify_row(integrator, {"id": 1, "error": "boom"}) is not None

    notifications = STREAM_BY_NAME["notification-logs"]
    assert classify_row(notifications, {"id": 1, "status": "sent"}) is None
    assert classify_row(notifications, {"id": 1, "status": "sent", "message": "delivered"}) is None
    assert classify_row(notifications, {"id": 1, "error": "boom"}) is not None

    bulk = STREAM_BY_NAME["bulk-emails"]
    assert classify_row(bulk, {"id": 1, "status": "sent"}) is None
    retrying = classify_row(bulk, {"id": 1, "status": "pending", "next_retry_date": _iso()})
    assert retrying is not None
    assert "next retry" in retrying["detail"]

    campaigns = STREAM_BY_NAME["mail-campaign-logs"]
    assert classify_row(campaigns, {"id": 1}) is None
    assert classify_row(campaigns, {"id": 1, "failure_count": "3"}) is not None

    assert classify_row(monitoring, {}) is not None
    unknown = StreamSpec("unknown-stream", "/Unknown")
    assert classify_row(unknown, {"id": 1}) is None


def test_health_near_saturation_and_grouping_edge_cases():
    near = classify_document(
        STREAM_BY_NAME["backend-health"],
        {
            "inuse_worker_threads": 95,
            "available_worker_threads": 5,
            "max_worker_threads": 100,
        },
    )
    assert near[0]["severity"] == "high"

    grouped = group_issues(
        [
            {"key": "k", "severity": "medium", "title": "worse", "last_seen": _iso(10)},
            {
                "key": "k",
                "severity": "critical",
                "title": "worst",
                "last_seen": _iso(1),
                "detail": "latest",
            },
            {"key": "k", "severity": "low", "title": "old", "last_seen": _iso(30)},
        ]
    )
    assert len(grouped) == 1
    assert grouped[0]["severity"] == "critical"
    assert grouped[0]["title"] == "worst"
    assert grouped[0]["first_seen"] == _iso(30)
    assert grouped[0]["detail"] == "latest"


def test_document_staleness_uses_the_snapshot_clock():
    tasks = dict(_all_streams()["/TaskSchedule"])
    tasks["lastdailytaskrun"] = "2030-01-01T00:00:00"
    future = datetime(2030, 1, 3, tzinfo=timezone.utc)
    snapshot = asyncio.run(
        collect_snapshot(
            FakeClient(_all_streams(**{"/TaskSchedule": tasks})),
            since=None,
            streams=["task-schedule"],
            now=future,
        )
    )
    assert [issue["key"] for issue in snapshot["issues"]] == ["task-schedule|daily-task-run"]


def test_paged_streams_honor_the_requested_limit():
    class PagedClient:
        def __init__(self):
            self.pages = []

        async def request(self, method, path, *, params=None, **kwargs):
            page_no = params["page_no"]
            self.pages.append(page_no)
            start = (page_no - 1) * 100
            rows = [
                {
                    "id": start + index,
                    "date": _iso(),
                    "module_name": "M",
                    "entity_type": "E",
                    "entity_id": start + index,
                    "error": "boom",
                }
                for index in range(100)
            ]
            return {"errors": rows, "record_count": 250}

    client = PagedClient()
    snapshot = asyncio.run(
        collect_snapshot(client, since=None, streams=["integration-errors"], limit=150, now=NOW)
    )
    state = snapshot["streams"]["integration-errors"]
    assert client.pages == [1, 2]
    assert state["rows"] == 150
    assert state["records"] == 250
    assert state["truncated"] is True


def test_invalid_collection_payload_is_a_stream_error():
    snapshot = _run(
        _all_streams(**{"/NotificationLog": b"<html>gateway error</html>"}),
        since=None,
        streams=["notification-logs"],
    )
    state = snapshot["streams"]["notification-logs"]
    assert state["status"] == "error"
    assert "JSON collection" in state["error"]
    assert any("notification-logs" in warning for warning in snapshot["warnings"])


def test_snapshot_rejects_bad_limit_and_document_body():
    with pytest.raises(ValueError):
        asyncio.run(collect_snapshot(FakeClient(_all_streams()), limit=0))
    snapshot = _run(
        _all_streams(**{"/Health": ["not-a-document"]}), since=None, streams=["backend-health"]
    )
    assert snapshot["streams"]["backend-health"]["status"] == "error"


class _FakeHaloClient:
    responses = _all_streams()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, path, *, params=None, **kwargs):
        value = self.responses.get(path, [])
        if isinstance(value, Exception):
            raise value
        return value


def _cli(monkeypatch):
    import halocli.cli as cli

    monkeypatch.setattr(cli, "load_profile", lambda name: object())
    monkeypatch.setattr(cli, "HaloClient", _FakeHaloClient)
    return get_command(app)


def test_cli_issues_exits_two_when_issues_exist(monkeypatch):
    _FakeHaloClient.responses = _all_streams(
        **{"/Outgoingemail": [{"id": 9, "datesent": _iso(), "subject": "s", "error": "e"}]}
    )
    result = ClickCliRunner().invoke(
        _cli(monkeypatch),
        ["monitoring", "issues", "--since", "all", "--fail-on-issue"],
    )
    assert result.exit_code == 2
    assert '"total": 1' in result.output


def test_cli_snapshot_and_document_commands(monkeypatch):
    _FakeHaloClient.responses = _all_streams()
    cli_app = _cli(monkeypatch)
    snapshot = ClickCliRunner().invoke(cli_app, ["monitoring", "snapshot", "--since", "all"])
    assert snapshot.exit_code == 0
    assert '"streams"' in snapshot.output
    health = ClickCliRunner().invoke(cli_app, ["monitoring", "health"])
    assert health.exit_code == 0
    assert '"health"' in health.output
    queue = ClickCliRunner().invoke(cli_app, ["monitoring", "queue"])
    assert queue.exit_code == 0
    assert '"task_schedule"' in queue.output


def test_cli_monitoring_input_validation(monkeypatch):
    cli_app = _cli(monkeypatch)
    for args in (
        ["monitoring", "issues", "--since", "eventually"],
        ["monitoring", "issues", "--stream", "nope"],
        ["monitoring", "issues", "--limit", "0"],
    ):
        result = ClickCliRunner().invoke(cli_app, args)
        assert result.exit_code != 0
