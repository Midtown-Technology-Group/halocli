from __future__ import annotations

import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.todo import (
    GraphMicrosoftTodoRepository,
    HaloTodoRepository,
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
    assert "--source-json" in result.output


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
