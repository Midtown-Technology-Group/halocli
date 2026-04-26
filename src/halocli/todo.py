from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol


DEFAULT_CACHE_NAMESPACE = "mtg-shared-microsoft-auth"
PLACEHOLDER_CLIENT_ID = "11111111-1111-1111-1111-111111111111"


@dataclass(frozen=True)
class MicrosoftTodoTask:
    id: str
    title: str
    list_id: str | None = None
    list_name: str | None = None
    status: str = "notStarted"
    importance: str = "normal"
    body: str = ""
    due_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "list_id": self.list_id,
            "list_name": self.list_name,
            "status": self.status,
            "importance": self.importance,
            "body": self.body,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class MicrosoftTodoRepository(Protocol):
    def list_tasks(
        self,
        *,
        list_name: str | None = None,
        include_completed: bool = False,
        max_records: int | None = None,
    ) -> list[MicrosoftTodoTask]:
        """Return Microsoft To Do tasks."""


class GraphMicrosoftTodoRepository:
    def __init__(self, graph_client: Any) -> None:
        self.graph_client = graph_client

    @classmethod
    def from_shared_auth(cls, *, scopes: list[str] | None = None) -> GraphMicrosoftTodoRepository:
        client_id = required_client_id()
        try:
            from mtg_microsoft_auth import AuthConfig, AuthMode, GraphAuthSession, GraphClient
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Live Microsoft To Do preview requires mtg-microsoft-auth to be installed."
            ) from exc

        config = AuthConfig(
            client_id=client_id,
            tenant_id=os.environ.get("TODO_TENANT_ID", "common"),
            scopes=scopes or scopes_from_env(),
            mode=AuthMode(os.environ.get("TODO_AUTH_MODE", "auto")),
            cache_namespace=os.environ.get("MTG_AUTH_CACHE_NAMESPACE", DEFAULT_CACHE_NAMESPACE),
            allow_broker=env_bool("TODO_ALLOW_BROKER", True),
        )
        session = GraphAuthSession(config)
        return cls(GraphClient(session))

    def list_tasks(
        self,
        *,
        list_name: str | None = None,
        include_completed: bool = False,
        max_records: int | None = None,
    ) -> list[MicrosoftTodoTask]:
        lists_payload = self.graph_client.get("/me/todo/lists")
        lists = lists_payload.get("value", []) if isinstance(lists_payload, dict) else []
        tasks: list[MicrosoftTodoTask] = []
        for todo_list in lists:
            name = str(todo_list.get("displayName") or "")
            if list_name and name.lower() != list_name.lower():
                continue
            list_id = str(todo_list.get("id") or "")
            payload = self.graph_client.get_all(f"/me/todo/lists/{list_id}/tasks")
            rows = payload.get("value", []) if isinstance(payload, dict) else payload
            for raw in rows or []:
                tasks.append(task_from_graph(raw, list_id=list_id, list_name=name))
        return filter_tasks(tasks, include_completed=include_completed, max_records=max_records)


class JsonMicrosoftTodoRepository:
    def __init__(self, path: str) -> None:
        self.path = path

    def list_tasks(
        self,
        *,
        list_name: str | None = None,
        include_completed: bool = False,
        max_records: int | None = None,
    ) -> list[MicrosoftTodoTask]:
        with open(self.path, encoding="utf-8") as stream:
            payload = json.load(stream)
        tasks = tasks_from_payload(payload)
        if list_name:
            tasks = [task for task in tasks if (task.list_name or "").lower() == list_name.lower()]
        return filter_tasks(tasks, include_completed=include_completed, max_records=max_records)


class HaloTodoRepository:
    def __init__(self, halo_client: Any) -> None:
        self.halo_client = halo_client

    async def create(
        self,
        *,
        title: str,
        description: str = "",
        owner: int | None = None,
        due: date | None = None,
        tags: list[str] | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_owner = owner
        if resolved_owner is None:
            resolved_owner = await self._current_agent_id()
        payload = appointment_payload(
            title=title,
            description=description,
            owner=resolved_owner,
            due=due,
            tags=tags or [],
            source_metadata=source_metadata or {"source": "halocli"},
        )
        result = await self.halo_client.raw("POST", "/Appointment", body=[payload])
        item = first_result(result) or {**payload}
        return todo_from_appointment(item)

    async def _current_agent_id(self) -> int:
        agent = await self.halo_client.raw("GET", "/Agent/me")
        if isinstance(agent, dict) and agent.get("id") is not None:
            return int(agent["id"])
        raise RuntimeError("Could not resolve current Halo agent id.")


def preview_import(
    repository: MicrosoftTodoRepository,
    *,
    list_name: str | None = None,
    include_completed: bool = False,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    tasks = repository.list_tasks(
        list_name=list_name,
        include_completed=include_completed,
        max_records=max_records,
    )
    return [preview_task(task) for task in tasks]


def preview_task(task: MicrosoftTodoTask) -> dict[str, Any]:
    return {
        "source": task.to_dict(),
        "skipped_reason": "completed" if task.is_completed else None,
        "proposed": {
            "title": task.title,
            "description": task.body,
            "priority": priority_from_importance(task.importance),
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "tags": [tag for tag in ("microsoft-todo", task.list_name or "") if tag],
            "source_metadata": {
                "source": "microsoft.todo",
                "microsoft_todo_id": task.id,
                "microsoft_todo_list_id": task.list_id,
                "microsoft_todo_list_name": task.list_name,
                "microsoft_todo_status": task.status,
            },
        },
    }


def task_from_graph(raw: dict[str, Any], *, list_id: str | None = None, list_name: str | None = None) -> MicrosoftTodoTask:
    body = raw.get("body")
    body_content = body.get("content") if isinstance(body, dict) else raw.get("body") or raw.get("description") or ""
    return MicrosoftTodoTask(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or raw.get("subject") or "").strip(),
        list_id=list_id or clean_optional(raw.get("list_id") or raw.get("listId")),
        list_name=list_name or clean_optional(raw.get("list_name") or raw.get("listName")),
        status=str(raw.get("status") or "notStarted"),
        importance=str(raw.get("importance") or "normal"),
        body=clean_body(str(body_content or "")),
        due_date=parse_graph_date(raw.get("dueDateTime")),
        created_at=parse_datetime(raw.get("createdDateTime") or raw.get("created")),
        updated_at=parse_datetime(raw.get("lastModifiedDateTime") or raw.get("updated")),
        completed_at=parse_graph_datetime(raw.get("completedDateTime")),
        raw=dict(raw),
    )


def appointment_payload(
    *,
    title: str,
    description: str,
    owner: int | None,
    due: date | None,
    tags: list[str],
    source_metadata: dict[str, Any],
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Todo title is required.")
    metadata = {
        **source_metadata,
        "kind": "halocli.todo",
        "tags": tags,
    }
    payload: dict[str, Any] = {
        "subject": clean_title,
        "note_html": note_html(description, metadata),
        "is_task": True,
        "complete_status": -1,
        "allday": True,
        "is_private": False,
        "reminderminutes": 0,
        "agent_status": 1,
        "user_id": -1,
        "appointment_type_id": 0,
        "shift_type_id": 0,
        "type": 5,
    }
    if owner is not None:
        payload["agent_id"] = owner
        payload["agents"] = [{"id": owner, "use": "agent"}]
    if due:
        payload.update(due_fields(due))
    else:
        payload.update(default_task_window())
    return payload


def todo_from_appointment(item: dict[str, Any]) -> dict[str, Any]:
    metadata = extract_metadata(str(item.get("note_html") or ""))
    return {
        "id": item.get("id"),
        "title": item.get("subject"),
        "status": "done" if str(item.get("complete_status")) == "0" else "open",
        "owner": item.get("agent_id"),
        "due_date": parse_halo_date(item.get("start_date")),
        "tags": metadata.get("tags", []),
        "source_metadata": metadata,
    }


def tasks_from_payload(payload: Any) -> list[MicrosoftTodoTask]:
    if isinstance(payload, dict) and isinstance(payload.get("lists"), list):
        tasks: list[MicrosoftTodoTask] = []
        for todo_list in payload["lists"]:
            list_id = str(todo_list.get("id") or "")
            list_name = str(todo_list.get("displayName") or todo_list.get("name") or "")
            for raw in todo_list.get("tasks", []):
                tasks.append(task_from_graph(raw, list_id=list_id, list_name=list_name))
        return tasks
    rows = payload.get("tasks") or payload.get("value") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Unsupported Microsoft To Do JSON payload shape.")
    return [task_from_graph(row) for row in rows]


def filter_tasks(
    tasks: list[MicrosoftTodoTask],
    *,
    include_completed: bool,
    max_records: int | None,
) -> list[MicrosoftTodoTask]:
    filtered = [task for task in tasks if include_completed or not task.is_completed]
    return filtered[:max_records] if max_records is not None else filtered


def required_client_id() -> str:
    client_id = os.environ.get("TODO_CLIENT_ID", "").strip()
    if not client_id or client_id == PLACEHOLDER_CLIENT_ID:
        raise RuntimeError(
            "TODO_CLIENT_ID must be set to a real Entra public client application ID."
        )
    return client_id


def scopes_from_env() -> list[str]:
    return [scope.strip() for scope in os.environ.get("TODO_SCOPES", "Tasks.Read").split(",") if scope.strip()]


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def priority_from_importance(value: str) -> str:
    return "high" if value.lower() == "high" else "normal"


def parse_graph_date(value: Any) -> date | None:
    data = value if isinstance(value, dict) else {}
    candidate = data.get("dateTime") or data.get("date")
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(str(candidate).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_graph_datetime(value: Any) -> datetime | None:
    data = value if isinstance(value, dict) else {}
    return parse_datetime(data.get("dateTime") or value)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_halo_date(value: Any) -> str | None:
    parsed = parse_datetime(value)
    return parsed.date().isoformat() if parsed else None


def clean_body(value: str) -> str:
    clean = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    clean = re.sub(r"</p\s*>", "\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<.*?>", "", clean)
    return html.unescape(clean).strip()


def clean_optional(value: Any) -> str | None:
    clean = str(value or "").strip()
    return clean or None


def note_html(description: str, metadata: dict[str, Any]) -> str:
    body = f"<p>{html.escape(description)}</p>" if description else ""
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return f"{body}<!-- bifrost-todo:{encoded} -->"


def extract_metadata(value: str) -> dict[str, Any]:
    match = re.search(r"<!-- bifrost-todo:(.*?)-->", value or "", flags=re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def due_fields(value: date) -> dict[str, Any]:
    start = f"{value.isoformat()}T00:00:00.000"
    end = f"{value.isoformat()}T23:59:59.000"
    return {
        "start_date": start,
        "end_date": end,
        "start_date_only": value.isoformat(),
        "end_date_only": value.isoformat(),
    }


def default_task_window() -> dict[str, Any]:
    today = date.today()
    start = datetime.combine(today, time(9, 0))
    end = start + timedelta(minutes=15)
    return {
        "start_date": start.isoformat(timespec="seconds"),
        "end_date": end.isoformat(timespec="seconds"),
        "start_date_only": today.isoformat(),
        "end_date_only": today.isoformat(),
    }


def first_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, list):
        return result[0] if result else None
    if isinstance(result, dict):
        for key in ("items", "appointments", "value"):
            if isinstance(result.get(key), list):
                return result[key][0] if result[key] else None
        return result
    return None
