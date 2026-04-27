from __future__ import annotations

from fastapi.testclient import TestClient

from halocli.todo_web import create_todo_api


class FakeTodoRepository:
    def __init__(self) -> None:
        self.todos = {
            1: {
                "id": 1,
                "title": "Independent todo list front end for HaloPSA",
                "description": "Build the first UI slice.",
                "status": "open",
                "priority": "normal",
                "due_date": "2026-04-27",
                "owner": 37,
                "client_id": 12,
                "ticket_id": 12345,
                "tags": ["microsoft-todo", "Tasks"],
                "notes": [],
                "time_entries": [],
                "source_metadata": {"source": "microsoft.todo"},
            }
        }
        self.clients = [{"id": 12, "name": "Midtown Technology Group"}]
        self.tickets = [{"id": 12345, "summary": "Backup alert", "client_id": 12, "status": "Open"}]

    async def list(self, **filters):
        items = list(self.todos.values())
        if filters.get("status"):
            items = [item for item in items if item["status"] == filters["status"]]
        if filters.get("tag"):
            items = [item for item in items if filters["tag"] in item["tags"]]
        if filters.get("q"):
            q = filters["q"].lower()
            items = [item for item in items if q in item["title"].lower()]
        return items

    async def get(self, todo_id):
        return self.todos[int(todo_id)]

    async def create(self, **data):
        todo_id = 2
        self.todos[todo_id] = {
            "id": todo_id,
            "status": "open",
            "priority": "normal",
            "tags": data.get("tags") or [],
            "notes": [],
            "time_entries": [],
            "source_metadata": {"source": "halocli"},
            **data,
        }
        return self.todos[todo_id]

    async def update(self, todo_id, **patch):
        self.todos[int(todo_id)].update({k: v for k, v in patch.items() if v is not None})
        return self.todos[int(todo_id)]

    async def complete(self, todo_id):
        self.todos[int(todo_id)]["status"] = "done"
        return self.todos[int(todo_id)]

    async def add_note(self, todo_id, note):
        self.todos[int(todo_id)]["notes"].append({"body": note})
        return self.todos[int(todo_id)]

    async def log_time(self, todo_id, **payload):
        entry = {"id": 9001, "todo_id": int(todo_id), "duration_minutes": payload.get("minutes", 0), **payload}
        self.todos[int(todo_id)]["time_entries"].append(entry)
        return entry

    async def list_time_entries(self, todo_id):
        return self.todos[int(todo_id)]["time_entries"]

    async def search_clients(self, q=None):
        return self.clients

    async def search_tickets(self, q=None, client_id=None, open_only=True):
        return [ticket for ticket in self.tickets if client_id is None or ticket["client_id"] == client_id]

    async def me(self):
        return {"id": 37, "name": "Thomas Bray", "client_id": 12, "client_name": "Midtown Technology Group"}


def test_todo_api_lists_filtered_items() -> None:
    client = TestClient(create_todo_api(lambda: FakeTodoRepository()))

    response = client.get("/api/todos", params={"status": "open", "tag": "Tasks", "q": "front end"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["title"] == "Independent todo list front end for HaloPSA"


def test_todo_api_create_update_complete_and_note() -> None:
    repository = FakeTodoRepository()
    client = TestClient(create_todo_api(lambda: repository))

    created = client.post(
        "/api/todos",
        json={"title": "Review migrated tasks", "tags": ["triage"], "ticket_id": 456},
    )
    assert created.status_code == 200
    assert created.json()["todo"]["id"] == 2

    updated = client.patch("/api/todos/2", json={"priority": "high", "client_id": 99})
    assert updated.status_code == 200
    assert updated.json()["todo"]["priority"] == "high"
    assert updated.json()["todo"]["client_id"] == 99

    noted = client.post("/api/todos/2/notes", json={"note": "Waiting on vendor."})
    assert noted.status_code == 200
    assert noted.json()["todo"]["notes"][0]["body"] == "Waiting on vendor."

    completed = client.post("/api/todos/2/complete")
    assert completed.status_code == 200
    assert completed.json()["todo"]["status"] == "done"


def test_todo_api_searches_clients_and_tickets() -> None:
    client = TestClient(create_todo_api(lambda: FakeTodoRepository()))

    clients = client.get("/api/clients", params={"q": "Midtown"})
    tickets = client.get("/api/tickets", params={"q": "backup", "client_id": 12, "open": True})
    me = client.get("/api/me")

    assert clients.status_code == 200
    assert clients.json()["items"] == [{"id": 12, "name": "Midtown Technology Group"}]
    assert tickets.status_code == 200
    assert tickets.json()["items"][0]["id"] == 12345
    assert me.status_code == 200
    assert me.json()["client_id"] == 12


def test_todo_api_logs_zero_duration_work() -> None:
    repository = FakeTodoRepository()
    client = TestClient(create_todo_api(lambda: repository))

    response = client.post(
        "/api/todos/1/time-entries",
        json={"note": "Reviewed alert context.", "minutes": 0, "client_id": 12, "ticket_id": 12345},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["time_entry"]["duration_minutes"] == 0
    assert payload["time_entry"]["client_id"] == 12
    assert payload["time_entry"]["ticket_id"] == 12345


def test_todo_api_reads_time_entry_history() -> None:
    repository = FakeTodoRepository()
    repository.todos[1]["time_entries"].append(
        {"id": 9001, "todo_id": 1, "duration_minutes": 0, "note": "Reviewed alert context."}
    )
    client = TestClient(create_todo_api(lambda: repository))

    response = client.get("/api/todos/1/time-entries")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["note"] == "Reviewed alert context."
