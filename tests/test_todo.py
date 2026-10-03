from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.todo import (
    GraphMicrosoftTodoRepository,
    HaloTodoRepository,
    MicrosoftTodoTask,
    extract_description,
    import_tasks,
    note_html,
    task_from_graph,
)


runner = CliRunner()


def test_todo_help_loads() -> None:
    result = runner.invoke(app, ["todo", "--help"])

    assert result.exit_code == 0
    assert "import-ms" in result.output
    assert "add" in result.output


def test_import_ms_help_loads() -> None:
    result = runner.invoke(app, ["todo", "import-ms", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "import-ms" in result.output
    assert "Microsoft" in result.output


def test_import_ms_complete_source_requires_apply() -> None:
    result = runner.invoke(app, ["todo", "import-ms", "--complete-source"])

    assert result.exit_code != 0
    assert "requires" in result.output
    assert "apply" in result.output


def test_task_from_graph_maps_empty_body_cleanly() -> None:
    task = task_from_graph(
        {
            "id": "task-1",
            "title": "Independent todo list front end for HaloPSA",
            "status": "notStarted",
            "importance": "normal",
            "body": {"content": "", "contentType": "text"},
        },
        list_id="list-1",
        list_name="Tasks",
    )

    assert task.body == ""
    assert task.title == "Independent todo list front end for HaloPSA"
    assert task.list_name == "Tasks"


def test_graph_repository_requires_real_client_id(monkeypatch) -> None:
    monkeypatch.delenv("TODO_CLIENT_ID", raising=False)

    with pytest.raises(RuntimeError, match="TODO_CLIENT_ID"):
        GraphMicrosoftTodoRepository.from_shared_auth()


@pytest.mark.asyncio
async def test_halo_repository_create_posts_appointment_payload() -> None:
    calls = []

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, body))
            return [{"id": 123, **body[0]}]

    repository = HaloTodoRepository(FakeClient())
    result = await repository.create(
        title="Independent todo list front end for HaloPSA",
        description="Created from Microsoft To Do migration dry-run.",
        owner=37,
        tags=["microsoft-todo", "halo-todo"],
    )

    assert result["id"] == 123
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/Appointment"
    assert body[0]["subject"] == "Independent todo list front end for HaloPSA"
    assert body[0]["is_task"] is True
    assert body[0]["agent_id"] == 37
    assert "bifrost-todo" in body[0]["note_html"]


@pytest.mark.asyncio
async def test_halo_repository_defaults_owner_to_current_agent() -> None:
    calls = []

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/Agent/me":
                return {"id": 37, "name": "Thomas Bray"}
            return [{"id": 123, **body[0]}]

    repository = HaloTodoRepository(FakeClient())
    result = await repository.create(title="Default owner")

    assert result["owner"] == 37
    post_body = calls[-1][2]
    assert post_body[0]["agent_id"] == 37
    assert post_body[0]["start_date"]
    assert post_body[0]["end_date"]


def test_task_from_graph_maps_due_date() -> None:
    task = task_from_graph(
        {
            "id": "task-1",
            "title": "Due task",
            "dueDateTime": {"dateTime": "2026-04-30T00:00:00", "timeZone": "UTC"},
        }
    )

    assert task.due_date is not None
    assert task.due_date.isoformat() == "2026-04-30"


@pytest.mark.asyncio
async def test_import_tasks_creates_halo_then_completes_source() -> None:
    completed = []

    class FakeMicrosoftRepository:
        def list_tasks(self, *, list_name=None, include_completed=False, max_records=None):
            return [
                MicrosoftTodoTask(
                    id="ms-1",
                    list_id="list-1",
                    list_name="Tasks",
                    title="Migrate me",
                    body="Body",
                )
            ]

        def complete_task(self, task):
            completed.append(task.id)
            return {"id": task.id, "status": "completed"}

    class FakeHaloRepository:
        async def create(self, **kwargs):
            assert kwargs["title"] == "Migrate me"
            assert kwargs["source_metadata"]["microsoft_todo_id"] == "ms-1"
            return {"id": 321, "title": kwargs["title"]}

    results = await import_tasks(
        FakeMicrosoftRepository(),
        FakeHaloRepository(),
        complete_source=True,
    )

    assert results[0]["imported"] is True
    assert results[0]["halo_todo"]["id"] == 321
    assert completed == ["ms-1"]


@pytest.mark.asyncio
async def test_halo_repository_update_preserves_metadata_and_sets_links() -> None:
    calls = []
    existing_note = note_html(
        "Existing body",
        {"kind": "halocli.todo", "tags": ["microsoft-todo"], "microsoft_todo_id": "ms-1"},
    )

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {
                    "id": 123,
                    "subject": "Old",
                    "note_html": existing_note,
                    "is_task": True,
                    "complete_status": -1,
                    "client_id": 1,
                }
            return [{**body[0], "id": 123}]

    repository = HaloTodoRepository(FakeClient())
    result = await repository.update(
        123,
        title="New title",
        description="Updated body",
        priority="high",
        client_id=99,
        ticket_id=456,
        tags=["microsoft-todo", "triage"],
    )

    assert result["title"] == "New title"
    assert result["description"] == "Updated body"
    assert result["priority"] == "high"
    assert result["client_id"] == 99
    assert result["ticket_id"] == 456
    assert result["source_metadata"]["microsoft_todo_id"] == "ms-1"
    post_body = calls[-1][2][0]
    assert "microsoft_todo_id" in post_body["note_html"]
    assert "triage" in post_body["note_html"]


def test_extract_description_ignores_metadata_marker() -> None:
    value = note_html("Plain text body", {"kind": "halocli.todo", "tags": ["x"]})

    assert extract_description(value) == "Plain text body"


@pytest.mark.asyncio
async def test_halo_repository_searches_clients_and_tickets() -> None:
    calls = []

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, params))
            if path == "/Client":
                return [{"id": 12, "name": "Midtown Technology Group"}]
            if path == "/Tickets":
                return [{"id": 12345, "summary": "Backup alert", "client_id": 12, "status": "Open"}]
            return {}

    repository = HaloTodoRepository(FakeClient())
    clients = await repository.search_clients(q="Midtown")
    tickets = await repository.search_tickets(q="backup", client_id=12, open_only=True)

    assert clients == [{"id": 12, "name": "Midtown Technology Group"}]
    assert tickets == [{"id": 12345, "summary": "Backup alert", "client_id": 12, "status": "Open"}]
    assert calls[0] == ("GET", "/Client", {"search": "Midtown", "page_size": 25})
    assert calls[1] == (
        "GET",
        "/Tickets",
        {"search": "backup", "client_id": 12, "open_only": True, "page_size": 25},
    )


@pytest.mark.asyncio
async def test_halo_repository_logs_zero_duration_time_entry_with_context() -> None:
    calls = []
    existing_note = note_html("Existing body", {"kind": "halocli.todo", "tags": []})

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/Appointment/123":
                return {
                    "id": 123,
                    "subject": "Investigate backup alert",
                    "note_html": existing_note,
                    "is_task": True,
                    "complete_status": -1,
                    "client_id": 12,
                    "ticket_id": 12345,
                    "agent_id": 37,
                }
            if method == "GET" and path == "/Agent/me":
                return {"id": 37, "name": "Thomas Bray", "client_id": 12, "client_name": "Midtown"}
            if method == "POST" and path == "/TimesheetEvent":
                return [{"id": 9001, **body[0]}]
            return [{**body[0], "id": 123}]

    repository = HaloTodoRepository(FakeClient())
    result = await repository.log_time(123, note="Reviewed alert context.", minutes=0)

    assert result["id"] == 9001
    assert result["todo_id"] == 123
    assert result["duration_minutes"] == 0
    assert result["client_id"] == 12
    assert result["ticket_id"] == 12345
    time_payload = calls[-1][2][0]
    assert calls[-1][1] == "/TimesheetEvent"
    assert time_payload["timetaken"] == 0
    assert time_payload["client_id"] == 12
    assert time_payload["ticket_id"] == 12345
    assert time_payload["note"] == "Reviewed alert context."


@pytest.mark.asyncio
async def test_halo_repository_time_entry_client_override_updates_todo() -> None:
    calls = []

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/Appointment/123":
                return {
                    "id": 123,
                    "subject": "Investigate",
                    "note_html": note_html("", {"kind": "halocli.todo", "tags": []}),
                    "is_task": True,
                    "complete_status": -1,
                    "client_id": 12,
                }
            if method == "GET" and path == "/Agent/me":
                return {"id": 37}
            if method == "POST" and path == "/TimesheetEvent":
                return [{"id": 9002, **body[0]}]
            return [{**body[0], "id": 123}]

    repository = HaloTodoRepository(FakeClient())
    result = await repository.log_time(
        123, note="Moved to customer context.", minutes=0, client_id=99
    )

    appointment_updates = [call for call in calls if call[1] == "/Appointment"]
    assert appointment_updates[0][2][0]["client_id"] == 99
    assert result["client_id"] == 99


@pytest.mark.asyncio
async def test_halo_repository_reads_time_entry_history_for_todo() -> None:
    calls = []

    class FakeClient:
        async def raw(self, method, path, *, params=None, body=None):
            calls.append((method, path, params))
            if method == "GET" and path == "/Appointment/123":
                return {
                    "id": 123,
                    "subject": "Investigate backup alert",
                    "note_html": note_html("", {"kind": "halocli.todo", "tags": []}),
                    "is_task": True,
                    "complete_status": -1,
                    "client_id": 12,
                    "ticket_id": 12345,
                }
            if method == "GET" and path == "/TimesheetEvent":
                return [
                    {
                        "id": 9001,
                        "todo_id": 123,
                        "subject": "[Todo #123] Investigate backup alert",
                        "note": "Reviewed alert context.",
                        "timetaken": 0,
                        "client_id": 12,
                        "ticket_id": 12345,
                    },
                    {
                        "id": 9002,
                        "subject": "Unrelated",
                        "note": "Ignore me",
                        "timetaken": 1,
                        "client_id": 12,
                    },
                ]
            return {}

    repository = HaloTodoRepository(FakeClient())
    entries = await repository.list_time_entries(123)

    assert len(entries) == 1
    assert entries[0]["id"] == 9001
    assert entries[0]["note"] == "Reviewed alert context."
    assert entries[0]["duration_minutes"] == 0
    assert calls[-1] == (
        "GET",
        "/TimesheetEvent",
        {"todo_id": 123, "client_id": 12, "ticket_id": 12345, "page_size": 50},
    )


# ---------------------------------------------------------- server-side list


def _task(task_id: int, *, complete_status: int = -1) -> dict:
    return {
        "id": task_id,
        "is_task": True,
        "complete_status": complete_status,
        "subject": f"Task {task_id}",
        "agent_id": 37,
        "start_date": "2026-04-26",
    }


class _AppointmentPages:
    """Fake raw() serving /Appointment pages keyed by page_no (records calls)."""

    def __init__(self, pages: dict[int, list[dict]]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def raw(self, method, path, *, params=None, body=None):
        self.calls.append({"method": method, "path": path, "params": dict(params or {})})
        assert method == "GET" and path == "/Appointment"
        return {"appointments": self.pages.get(int((params or {}).get("page_no") or 1), [])}


@pytest.mark.asyncio
async def test_todo_list_uses_documented_server_filters(monkeypatch) -> None:
    """Proven live: tasksonly/hidecompleted/agents/page_no are honored; agent_id is NOT."""
    monkeypatch.setattr(
        HaloTodoRepository,
        "_current_agent_id",
        lambda self: _async_value(37),
    )
    client = _AppointmentPages({1: [_task(i) for i in range(100)]})
    todos = await HaloTodoRepository(client).list(status="open", max_records=200)

    params = client.calls[0]["params"]
    assert params["tasksonly"] == "true"
    assert params["hidecompleted"] == "true"
    assert params["page_no"] == "1"
    assert "agent_id" not in params  # undocumented -> silently ignored by Halo
    assert len(todos) == 100
    assert all(t["status"] == "open" for t in todos)


@pytest.mark.asyncio
async def test_todo_list_mine_uses_agents_param(monkeypatch) -> None:
    monkeypatch.setattr(
        HaloTodoRepository,
        "_current_agent_id",
        lambda self: _async_value(37),
    )
    client = _AppointmentPages({1: [_task(i) for i in range(100)]})
    await HaloTodoRepository(client).list(mine=True, max_records=10)

    params = client.calls[0]["params"]
    assert params["agents"] == "37"
    assert "agent_id" not in params


@pytest.mark.asyncio
async def test_todo_list_pages_past_the_first_window() -> None:
    """The old client-side filter read only the first200 appointments;
    server-side task paging must aggregate across pages."""
    pages = {
        1: [_task(i) for i in range(100)],
        2: [_task(i) for i in range(100, 180)],
    }
    client = _AppointmentPages(pages)
    todos = await HaloTodoRepository(client).list(status="open", max_records=200)

    assert len(todos) == 180  # both pages aggregated
    assert {t["id"] for t in todos} == set(range(180))


@pytest.mark.asyncio
async def test_todo_list_repeated_page_stops_the_loop() -> None:
    """A paging-ignored endpoint must not duplicate (the issue #24 lesson)."""
    pages = {1: [_task(i) for i in range(100)]}  # every page returns page 1
    client = _AppointmentPages(pages)
    todos = await HaloTodoRepository(client).list(status="open", max_records=200)

    assert len(todos) == 100
    assert len({t["id"] for t in todos}) == 100  # no duplicates
    assert len(client.calls) == 2  # page1 + the identical page2, then stop


@pytest.mark.asyncio
async def test_todo_list_done_filters_client_side_without_hidecompleted() -> None:
    rows = [_task(1, complete_status=-1), _task(2, complete_status=0), _task(3, complete_status=0)]
    client = _AppointmentPages({1: rows})
    todos = await HaloTodoRepository(client).list(status="done", max_records=50)

    assert "hidecompleted" not in client.calls[0]["params"]
    assert {t["id"] for t in todos} == {2, 3}
    assert all(t["status"] == "done" for t in todos)


def _async_value(value):
    async def _inner(self=None):
        return value

    return _inner()


# ------------------------------------------------------------- CLI gate tests


def _mock_appointments(monkeypatch, *, row: dict) -> list[dict]:
    """Serve /auth/token, GET/POST /Appointment; record requests."""
    import httpx

    async_client = httpx.AsyncClient
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        path = request.url.path  # HaloClient prefixes /api
        if "/Appointment/" in path and request.method == "GET":
            return httpx.Response(200, json=row)
        if path.endswith("/Appointment") and request.method == "POST":
            seen.append({"post": True, "body": request.content.decode("utf-8")[:400]})
            return httpx.Response(200, json={"id": row.get("id", 1)})
        if path.endswith("/Appointment") and request.method == "GET":
            seen.append({"get_appointments": True})
            return httpx.Response(200, json={"appointments": [row]})
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    return seen


def test_todo_complete_preview_makes_no_write(monkeypatch) -> None:
    seen = _mock_appointments(monkeypatch, row=_task(10257))

    result = runner.invoke(app, ["todo", "complete", "10257"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["payload"]["complete_status"] == 0  # Halo: 0 = done
    assert "--apply --yes" in payload["hint"]
    assert not any("post" in entry for entry in seen)  # zero writes


def test_todo_complete_requires_both_flags(monkeypatch) -> None:
    seen = _mock_appointments(monkeypatch, row=_task(10257))

    result = runner.invoke(app, ["todo", "complete", "10257", "--apply"])

    assert result.exit_code != 0
    assert not any("post" in entry for entry in seen)


def test_todo_complete_apply_fires_the_post(monkeypatch) -> None:
    seen = _mock_appointments(monkeypatch, row=_task(10257))

    result = runner.invoke(app, ["todo", "complete", "10257", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is True
    posts = [entry for entry in seen if "post" in entry]
    assert len(posts) == 1
    assert '"complete_status":0' in posts[0]["body"].replace(" ", "")


def test_todo_add_preview_makes_no_write(monkeypatch) -> None:
    seen = _mock_appointments(monkeypatch, row=_task(1))
    monkeypatch.setattr(HaloTodoRepository, "_current_agent", lambda self: _async_value({"id": 37}))

    result = runner.invoke(app, ["todo", "add", "Preview only task"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["payload"]["agent_id"] == 37  # owner resolved for a true preview
    assert "--apply --yes" in payload["hint"]
    assert not any("post" in entry for entry in seen)


def test_todo_add_apply_fires_the_post(monkeypatch) -> None:
    seen = _mock_appointments(monkeypatch, row=_task(1))
    monkeypatch.setattr(HaloTodoRepository, "_current_agent", lambda self: _async_value({"id": 37}))

    result = runner.invoke(app, ["todo", "add", "Real task", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert any("post" in entry for entry in seen)
