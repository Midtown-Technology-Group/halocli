from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from halocli.client import HaloClient
from halocli.config import load_profile
from halocli.todo import HaloTodoRepository

try:
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ModuleNotFoundError:  # pragma: no cover
    Depends = FastAPI = HTTPException = FileResponse = StaticFiles = BaseModel = None  # type: ignore[assignment]


if BaseModel is not None:

    class TodoCreate(BaseModel):
        title: str
        description: str = ""
        due_date: date | None = None
        priority: str = "normal"
        owner: int | None = None
        client_id: int | None = None
        site_id: int | None = None
        ticket_id: int | None = None
        tags: list[str] = []

    class TodoPatch(BaseModel):
        title: str | None = None
        description: str | None = None
        status: str | None = None
        due_date: date | None = None
        priority: str | None = None
        owner: int | None = None
        client_id: int | None = None
        site_id: int | None = None
        ticket_id: int | None = None
        tags: list[str] | None = None

    class TodoNote(BaseModel):
        note: str

    class TodoTimeEntry(BaseModel):
        note: str
        minutes: float | None = None
        hours: float | None = None
        start: datetime | None = None
        end: datetime | None = None
        client_id: int | None = None
        ticket_id: int | None = None


def create_todo_api(repository_factory: Callable[[], Any]):
    if FastAPI is None or Depends is None:
        raise RuntimeError("Install halocli with the web extra: pip install 'halocli[web]'.")

    async def repository():
        return repository_factory()

    app = FastAPI(title="Halo Todo", version="0.1.0")

    @app.get("/api/todos")
    async def list_todos(
        repo: Any = Depends(repository),
        status: str | None = "open",
        mine: bool = False,
        client_id: int | None = None,
        ticket_id: int | None = None,
        tag: str | None = None,
        q: str | None = None,
        max_records: int = 200,
    ):
        items = await repo.list(
            status=status,
            mine=mine,
            client_id=client_id,
            ticket_id=ticket_id,
            tag=tag,
            q=q,
            max_records=max_records,
        )
        return {"count": len(items), "items": items}

    @app.get("/api/clients")
    async def search_clients(repo: Any = Depends(repository), q: str | None = None):
        items = await repo.search_clients(q=q)
        return {"count": len(items), "items": items}

    @app.get("/api/tickets")
    async def search_tickets(
        repo: Any = Depends(repository),
        q: str | None = None,
        client_id: int | None = None,
        open: bool = True,  # noqa: A002
    ):
        items = await repo.search_tickets(q=q, client_id=client_id, open_only=open)
        return {"count": len(items), "items": items}

    @app.get("/api/me")
    async def me(repo: Any = Depends(repository)):
        return await repo.me()

    @app.post("/api/todos")
    async def create_todo(payload: TodoCreate, repo: Any = Depends(repository)):
        if not payload.title.strip():
            raise HTTPException(status_code=400, detail="Todo title is required.")
        todo = await repo.create(
            title=payload.title,
            description=payload.description,
            owner=payload.owner,
            due=payload.due_date,
            priority=payload.priority,
            client_id=payload.client_id,
            site_id=payload.site_id,
            ticket_id=payload.ticket_id,
            tags=payload.tags,
        )
        return {"todo": todo}

    @app.get("/api/todos/{todo_id}")
    async def get_todo(todo_id: int, repo: Any = Depends(repository)):
        return {"todo": await repo.get(todo_id)}

    @app.patch("/api/todos/{todo_id}")
    async def update_todo(todo_id: int, payload: TodoPatch, repo: Any = Depends(repository)):
        todo = await repo.update(
            todo_id,
            title=payload.title,
            description=payload.description,
            status=payload.status,
            due=payload.due_date,
            priority=payload.priority,
            owner=payload.owner,
            client_id=payload.client_id,
            site_id=payload.site_id,
            ticket_id=payload.ticket_id,
            tags=payload.tags,
        )
        return {"todo": todo}

    @app.post("/api/todos/{todo_id}/complete")
    async def complete_todo(todo_id: int, repo: Any = Depends(repository)):
        return {"todo": await repo.complete(todo_id)}

    @app.post("/api/todos/{todo_id}/notes")
    async def add_todo_note(todo_id: int, payload: TodoNote, repo: Any = Depends(repository)):
        if not payload.note.strip():
            raise HTTPException(status_code=400, detail="Note is required.")
        return {"todo": await repo.add_note(todo_id, payload.note)}

    @app.post("/api/todos/{todo_id}/time-entries")
    async def add_time_entry(todo_id: int, payload: TodoTimeEntry, repo: Any = Depends(repository)):
        if not payload.note.strip():
            raise HTTPException(status_code=400, detail="Work log note is required.")
        entry = await repo.log_time(
            todo_id,
            note=payload.note,
            minutes=payload.minutes,
            hours=payload.hours,
            start=payload.start,
            end=payload.end,
            client_id=payload.client_id,
            ticket_id=payload.ticket_id,
        )
        todo = await repo.get(todo_id)
        todo["time_entries"] = [entry, *list(todo.get("time_entries") or [])]
        return {"time_entry": entry, "todo": todo}

    @app.get("/api/todos/{todo_id}/time-entries")
    async def list_time_entries(todo_id: int, repo: Any = Depends(repository)):
        items = await repo.list_time_entries(todo_id)
        return {"count": len(items), "items": items}

    static_dir = web_static_dir()
    if static_dir.exists():
        assets = static_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def index(path: str = ""):
            candidate = static_dir / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


def create_halo_todo_api(*, profile: str):
    halo_profile = load_profile(profile)

    def repository_factory() -> HaloTodoRepository:
        client = HaloClient(halo_profile, profile_name=profile)
        return ManagedHaloTodoRepository(client)

    return create_todo_api(repository_factory)


class ManagedHaloTodoRepository(HaloTodoRepository):
    async def _run(self, name: str, *args: Any, **kwargs: Any) -> Any:
        async with self.halo_client:
            method = getattr(super(), name)
            return await method(*args, **kwargs)

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        return await self._run("list", **filters)

    async def get(self, todo_id: int | str) -> dict[str, Any]:
        return await self._run("get", todo_id)

    async def create(self, **data: Any) -> dict[str, Any]:
        return await self._run("create", **data)

    async def update(self, todo_id: int | str, **patch: Any) -> dict[str, Any]:
        return await self._run("update", todo_id, **patch)

    async def complete(self, todo_id: int | str) -> dict[str, Any]:
        return await self._run("complete", todo_id)

    async def add_note(self, todo_id: int | str, note: str) -> dict[str, Any]:
        return await self._run("add_note", todo_id, note)

    async def log_time(self, todo_id: int | str, **payload: Any) -> dict[str, Any]:
        return await self._run("log_time", todo_id, **payload)

    async def list_time_entries(self, todo_id: int | str) -> list[dict[str, Any]]:
        return await self._run("list_time_entries", todo_id)

    async def search_clients(self, q: str | None = None) -> list[dict[str, Any]]:
        return await self._run("search_clients", q)

    async def search_tickets(
        self,
        *,
        q: str | None = None,
        client_id: int | None = None,
        open_only: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run("search_tickets", q=q, client_id=client_id, open_only=open_only)

    async def me(self) -> dict[str, Any]:
        return await self._run("me")


def web_static_dir() -> Path:
    return Path(str(resources.files("halocli") / "web_static"))
