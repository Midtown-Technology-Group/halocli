"""Offline mirror package: sync bounds/honesty, SQL guard, local ops.

No test here contacts a tenant: sync runs against an httpx mock transport,
sql/standup/triage read fixtures built directly into tmp_path databases.

click 8.5 keeps stderr separate from stdout: `result.output` is stdout
(JSON parses cleanly even though sync prints progress to stderr), and
error-message assertions read `result.stderr` via `_all`.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.mirror import (
    DEFAULT_SYNC_LIMIT,
    connect,
    load_resource,
    replace_rows,
    state_for,
    sync_resources,
)
from halocli.sqlguard import SQLGuardError, validate_select_only

runner = CliRunner()

# halocli.client.httpx IS the httpx module: patching AsyncClient through it
# is global. Capture the original ONCE so later patches can re-wrap safely.
_ORIGINAL_ASYNC = httpx.AsyncClient


def _all(result) -> str:
    """stdout + stderr for error-message assertions."""
    return result.output + (result.stderr or "")


def _json(result) -> dict:
    """Parse a command payload from mixed output (typer's runner folds
    stderr progress lines into result.output; JSON is the last block)."""
    text = result.output
    start = text.find("{")
    assert start != -1, f"no JSON in output: {text[:200]!r}"
    return json.loads(text[start:])


# --------------------------------------------------------------- sync plumbing


def _ticket(ticket_id: int, **extra) -> dict:
    row = {"id": ticket_id, "summary": f"Ticket {ticket_id}"}
    row.update(extra)
    return row


def _pages(count_per_page: int, how_many: int) -> dict[int, list[dict]]:
    return {
        page: [_ticket(page * 100 + i) for i in range(count_per_page)]
        for page in range(1, how_many + 1)
    }


def _mock_sync_env(monkeypatch, *, record_count: int, pages: dict[int, list[dict]]):
    """Serve /auth/token + paged /Tickets envelopes; record page requests."""
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if path.endswith("/Tickets") and request.method == "GET":
            page = int(request.url.params.get("page_no", "1"))
            seen.append(page)
            return httpx.Response(
                200,
                json={"tickets": pages.get(page, []), "record_count": record_count},
            )
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: _ORIGINAL_ASYNC(transport=transport),
    )
    return seen


def test_sync_is_bounded_by_default(monkeypatch, tmp_path: Path) -> None:
    """Regression: None from the CLI must never mean 'page the whole
    tenant' - the house default ceiling applies when neither --all nor
    --max-records is given."""
    seen = _mock_sync_env(monkeypatch, record_count=10_000, pages=_pages(100, 8))
    db = tmp_path / "mirror.db"

    result = runner.invoke(app, ["sync", "-r", "tickets", "--db", str(db)])

    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    (entry,) = payload["items"]
    assert entry["rows"] == DEFAULT_SYNC_LIMIT
    assert entry["truncated"] is True
    assert entry["total"] == 10_000
    assert len(load_resource(db, "tickets")) == DEFAULT_SYNC_LIMIT
    # it stopped paging at the ceiling instead of walking all eight pages
    assert len(seen) <= 6
    state = state_for(db, "tickets")
    assert state is not None and state["truncated"] == 1


def test_sync_all_removes_the_ceiling(monkeypatch, tmp_path: Path) -> None:
    _mock_sync_env(monkeypatch, record_count=700, pages=_pages(100, 7))
    db = tmp_path / "mirror.db"

    result = runner.invoke(app, ["sync", "-r", "tickets", "--all", "--db", str(db)])

    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    assert payload["items"][0]["rows"] == 700
    assert payload["items"][0]["truncated"] is False


def test_sync_flag_combinations_refused(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["sync", "--all", "--max-records", "10", "--db", str(tmp_path / "m.db")]
    )
    assert result.exit_code != 0
    result = runner.invoke(
        app, ["sync", "--all-resources", "-r", "tickets", "--db", str(tmp_path / "m.db")]
    )
    assert result.exit_code != 0
    result = runner.invoke(app, ["sync", "-r", "not-a-resource", "--db", str(tmp_path / "m.db")])
    assert result.exit_code != 0
    assert "Unknown resource" in _all(result)


def test_replace_rows_preserves_duplicate_ids(tmp_path: Path) -> None:
    """Rows sharing an id across parents must both survive (seq), because
    keying by id alone silently drops one."""
    db = tmp_path / "mirror.db"
    conn = connect(db)
    shared = [
        {"id": 7, "ticket_id": 100, "summary": "a"},
        {"id": 7, "ticket_id": 200, "summary": "b"},
        {"id": 7, "ticket_id": 300, "summary": "c"},
        {"summary": "no id at all"},
    ]
    stored = replace_rows(conn, "actions", shared)
    conn.close()

    assert stored == 4
    rows = load_resource(db, "actions")
    assert len(rows) == 4
    assert sorted(r["ticket_id"] for r in rows if "ticket_id" in r) == [100, 200, 300]
    assert sum(1 for r in rows if r.get("summary") == "no id at all") == 1


def test_replace_rows_is_fresh_per_sync(tmp_path: Path) -> None:
    db = tmp_path / "mirror.db"
    conn = connect(db)
    replace_rows(conn, "tickets", [_ticket(1), _ticket(2)])
    replace_rows(conn, "tickets", [_ticket(3)])
    conn.close()

    rows = load_resource(db, "tickets")
    assert [r["id"] for r in rows] == [3]  # replaced, not merged


def test_failed_sync_keeps_prior_rows_and_records_error(monkeypatch, tmp_path: Path) -> None:
    _mock_sync_env(monkeypatch, record_count=2, pages={1: [_ticket(1), _ticket(2)]})
    db = tmp_path / "mirror.db"
    first = runner.invoke(app, ["sync", "-r", "tickets", "--db", str(db)])
    assert first.exit_code == 0, _all(first)

    # now break the endpoint: every /Tickets call 500s
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: _ORIGINAL_ASYNC(transport=transport),
    )

    result = runner.invoke(app, ["sync", "-r", "tickets", "--db", str(db)])

    assert result.exit_code == 0, _all(result)  # the run completes; failure is data
    payload = _json(result)
    assert payload["failed"] == 1
    assert "500" in payload["items"][0]["error"]
    # previous rows intact: a transient failure must not wipe the mirror
    assert len(load_resource(db, "tickets")) == 2
    state = state_for(db, "tickets")
    assert state is not None and state["error"]


@pytest.mark.asyncio
async def test_sync_stops_on_repeated_page_without_duplicates(tmp_path: Path) -> None:
    """An endpoint that answers every page with page 1 (issue #24 class):
    rows are never doubled and paging_ignored is recorded honestly."""
    db = tmp_path / "mirror.db"
    page1 = _pages(100, 1)[1]

    class _Client:
        async def list_resource(self, name, **kwargs):
            _ = kwargs
            return {"tickets": page1, "record_count": 5_000}

    summary = await sync_resources(
        _Client(), ["tickets"], db_path=db, max_records=DEFAULT_SYNC_LIMIT, labels=False
    )
    (entry,) = summary["results"]
    assert entry["error"] is None
    assert entry["paging_ignored"] is True
    assert entry["rows"] == 100  # one page, never appended twice
    state = state_for(db, "tickets")
    assert state is not None and state["paging_ignored"] == 1


# -------------------------------------------------------------------- sqlguard


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM tickets",
        "select id from tickets;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN QUERY PLAN SELECT 1",
        "SELECT 'we updated the deleted items' AS note",  # suffix forms pass
    ],
)
def test_sqlguard_allows_selects(query: str) -> None:
    assert validate_select_only(query) == query.strip().rstrip(";").strip()


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM tickets",
        "SELECT 1; DELETE FROM tickets",
        "SELECT 1;\nDROP TABLE tickets",
        "SELECT 1; PRAGMA writable_schema=1",
        "select 1; vacuum",
        "INSERT INTO tickets VALUES (1)",
        "ATTACH DATABASE 'x' AS y",
        "SELECT 'please update this row' AS note",  # conservative: literal rejected
        "UPDATE tickets SET summary = 'x'",
        "  ",
        "",
        ";",
    ],
)
def test_sqlguard_refuses_mutations(query: str) -> None:
    with pytest.raises(SQLGuardError):
        validate_select_only(query)


# ------------------------------------------------------------------ sql + ops


def _fixture_mirror(tmp_path: Path) -> Path:
    db = tmp_path / "mirror.db"
    conn = connect(db)
    tickets = [
        _ticket(
            1,
            summary="Oldest open",
            status_id=1,
            status_name="New",
            agent_id=37,
            agent_name="Thomas Bray",
            client_id=5,
            client_name="Acme",
            dateoccurred="2026-09-01T09:00:00",
            dateclosed=None,
            hasbeenclosed=None,
        ),
        _ticket(
            2,
            summary="Closed yesterday",
            status_id=9,
            status_name="Closed",
            agent_id=37,
            agent_name="Thomas Bray",
            client_id=5,
            client_name="Acme",
            dateoccurred="2026-10-02T09:00:00",
            dateclosed="2026-10-02T17:00:00",
            hasbeenclosed=True,
        ),
        _ticket(
            3,
            summary="Closed this morning",
            status_id=9,
            status_name="Closed",
            agent_id=38,
            agent_name="Other Agent",
            client_id=6,
            client_name="Beta",
            dateoccurred="2026-10-03T01:00:00",
            dateclosed="2026-10-03T06:00:00",
            hasbeenclosed=True,
        ),
        _ticket(
            4,
            summary="Unknown status, flag decides",
            status_id=99,  # not in the statuses mirror: name path unavailable
            agent_id=38,
            agent_name="Other Agent",
            client_id=6,
            client_name="Beta",
            dateoccurred="2026-10-01T09:00:00",
            hasbeenclosed=True,  # proven correlation: true <=> Closed
        ),
    ]
    replace_rows(conn, "tickets", tickets)
    replace_rows(
        conn,
        "statuses",
        [{"id": 1, "name": "New"}, {"id": 9, "name": "Closed"}],
    )
    replace_rows(
        conn,
        "agents",
        [{"id": 37, "name": "Thomas Bray"}, {"id": 38, "name": "Other Agent"}],
    )
    replace_rows(
        conn,
        "clients",
        [{"id": 5, "name": "Acme"}, {"id": 6, "name": "Beta"}],
    )
    conn.execute(
        "INSERT INTO mirror_state(resource, synced_at, rows, total, truncated, "
        "paging_ignored, labels_added, endpoint, error) VALUES "
        "('tickets', '2026-10-03T00:00:00+00:00', 4, 100, 1, 0, 9, '/Tickets', NULL)"
    )
    conn.commit()
    conn.close()
    return db


def test_sql_query_returns_rows(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(
        app,
        ["sql", "SELECT summary, status_name FROM tickets ORDER BY id", "--db", str(db)],
    )
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    assert payload["count"] == 4
    assert payload["items"][0]["summary"] == "Oldest open"
    assert payload["columns"] == ["summary", "status_name"]


def test_sql_refuses_writes_and_missing_db(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(app, ["sql", "DELETE FROM tickets", "--db", str(db)])
    assert result.exit_code == 1
    assert "Refusing" in _all(result)

    result = runner.invoke(app, ["sql", "SELECT 1", "--db", str(tmp_path / "missing.db")])
    assert result.exit_code == 1
    assert "halocli sync" in _all(result)


def test_sql_limit_is_reported_not_implied(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(app, ["sql", "SELECT id FROM tickets", "--db", str(db), "--limit", "2"])
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    assert payload["count"] == 2
    assert payload["truncated"] is True
    assert "--limit" in payload["hint"]


def test_triage_open_oldest_first_with_names(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(app, ["triage", "--db", str(db), "-o", "json"])
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    # only ticket 1 is open: status-name path excludes 2/3, the
    # hasbeenclosed flag excludes the unknown-status row 4
    ids = [item["id"] for item in payload["items"]]
    assert ids == [1]
    item = payload["items"][0]
    assert item["agent"] == "Thomas Bray"
    assert item["client"] == "Acme"
    assert item["status"] == "New"
    assert item["stale"] is True  # 2026-09-01 vs default stale-days=7
    assert payload["open"] == 1
    # honesty: a partial tickets mirror is surfaced, never implied complete
    assert payload["tickets_truncated"] is True
    assert "halocli sync --resource tickets --all" in payload["hint"]


def test_triage_closed_status_override(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(
        app,
        ["triage", "--db", str(db), "--closed-status", "New", "-o", "json"],
    )
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    # "New" now counts as closed (ticket 1 out); "Closed" no longer does
    # (tickets 2/3 in); ticket 4 stays out via the flag fallback
    ids = [item["id"] for item in payload["items"]]
    assert ids == [2, 3]


def test_triage_agent_filter(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(
        app,
        ["triage", "--agent", "other", "--closed-status", "New", "--db", str(db), "-o", "json"],
    )
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    assert [item["agent"] for item in payload["items"]] == ["Other Agent"]


def test_standup_window_and_per_agent_rollup(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(
        app, ["standup", "--since", "2026-10-03T00:00", "--db", str(db), "-o", "json"]
    )
    assert result.exit_code == 0, _all(result)
    payload = _json(result)
    # only ticket 3 closed after midnight today; ticket 2 closed yesterday
    assert payload["closed_in_window"] == 1
    by_agent = {row["agent"]: row for row in payload["items"]}
    assert by_agent["Other Agent"]["closed_in_window"] == 1
    assert by_agent["Thomas Bray"]["closed_in_window"] == 0
    assert by_agent["Thomas Bray"]["open_now"] == 1  # ticket 1 still open
    assert by_agent["Thomas Bray"]["top_client"] == "Acme"  # open workload counts
    assert payload["tickets_truncated"] is True


def test_standup_rejects_bad_since(tmp_path: Path) -> None:
    db = _fixture_mirror(tmp_path)
    result = runner.invoke(app, ["standup", "--since", "gibberish", "--db", str(db)])
    assert result.exit_code == 1
    assert "Cannot parse --since" in _all(result)


def test_ops_missing_mirror_is_friendly(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope.db")
    for command in (["triage"], ["standup"], ["sql", "SELECT 1"]):
        result = runner.invoke(app, [*command, "--db", missing])
        assert result.exit_code == 1
        assert "halocli sync" in _all(result)
