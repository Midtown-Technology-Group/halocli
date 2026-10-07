"""Offline mirror: bounded GET sync into a local SQLite database.

The foundation for `halocli sql`, `standup` and `triage`: sync once, then
answer cross-resource questions locally instead of guessing filter params or
re-paging a 137k-row tenant (the pains that motivated this surface).

Design decisions, each backed by evidence in mirror_evidence.json:

- **JSON-first rows** (``resource, row_key, seq, data``): no column
  materialization. Ticket payloads carry ``agent_id`` but no ``agent_name``
  (proven), and column extraction is where a full mirror goes wrong; label
  hydration bakes names INTO the JSON at sync time instead.
- **seq preserves duplicate ids**: keying children by id alone silently
  overwrites rows that share an id across parents.
- **Full replace per resource, only after a successful fetch**: a failed
  refresh records the error and keeps the previous rows (data loss on a
  transient failure is worse than staleness).
- **Bounded by default** (house list contract): truncation is recorded in
  ``mirror_state`` and reported in the command payload, never implied.
- **Sync-time ordering** for tickets is pinned to newest-first
  (``order=dateoccurred&orderdesc=true`` proven; the literal ``"true"``
  matters - ``"1"`` sorts ascending), so a ceiling grabs the recent window
  rather than the oldest rows.

Excluded by evidence, not oversight: ``/Feed`` is not a registry resource and
must never be page-walked (page 2 is page 1 verbatim and ``page_size`` is
ignored - proven); ``/Actions`` unfiltered times out even at 20s (proven), so
it is not in the core sync set.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from halocli.config import default_config_file
from halocli.labels import hydrate_items
from halocli.resources import get_resource
from halocli.utils import list_all

MIRROR_ENV_VAR = "HALOCLI_MIRROR_DB"

# Per-resource ceiling for a default sync (same scale as DEFAULT_LIST_LIMIT:
# bounded, reported, --all opts out). Tickets benefit: newest-first order
# means the ceiling keeps the recent, operationally useful window.
DEFAULT_SYNC_LIMIT = 500

# The ops-relevant core: enough to answer triage/standup/SQL questions
# locally in one bounded pass. `--all-resources` syncs the full registry.
CORE_SYNC_RESOURCES: tuple[str, ...] = (
    "tickets",
    "appointments",
    "statuses",
    "agents",
    "clients",
    "users",
    "teams",
    "sites",
    "assets",
    "priorities",
    "kb",
    "contracts",
    "to-dos",
    "ticket-areas",
)

# Evidence-proven server-side ordering (mirror_evidence.json ->
# tickets_ordering): literal "true" required.
SYNC_ORDER: dict[str, dict[str, Any]] = {
    "tickets": {"order": "dateoccurred", "orderdesc": "true"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_rows (
    resource TEXT NOT NULL,
    row_key  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    data     TEXT NOT NULL,
    PRIMARY KEY (resource, row_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_mirror_rows_resource ON mirror_rows(resource);
CREATE TABLE IF NOT EXISTS mirror_state (
    resource   TEXT PRIMARY KEY,
    synced_at  TEXT NOT NULL,
    rows       INTEGER NOT NULL,
    total      INTEGER,
    truncated  INTEGER NOT NULL DEFAULT 0,
    paging_ignored INTEGER NOT NULL DEFAULT 0,
    labels_added   INTEGER NOT NULL DEFAULT 0,
    endpoint   TEXT NOT NULL DEFAULT '',
    error      TEXT
);
CREATE TABLE IF NOT EXISTS mirror_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1"


class MirrorMissingError(RuntimeError):
    """The mirror database does not exist yet."""


def default_mirror_path() -> Path:
    override = os.environ.get(MIRROR_ENV_VAR)
    if override:
        return Path(override)
    return default_config_file().parent / "mirror.db"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO mirror_meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def open_existing(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise MirrorMissingError(f"Mirror database not found at {path}. Run 'halocli sync' first.")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_views(conn)
    return conn


def replace_rows(conn: sqlite3.Connection, resource: str, rows: Iterable[dict]) -> int:
    """Replace one resource's rows (full-fresh semantics, dup ids preserved).

    ``seq`` disambiguates repeated ids within a fetch so rows that share an
    id across parents both survive; keying by id alone would drop one.
    """
    seqs: dict[str, int] = {}
    payload: list[tuple[str, str, int, str]] = []
    for item in rows:
        raw_id = item.get("id") if isinstance(item, dict) else None
        key = str(raw_id) if raw_id not in (None, "") else f"anon:{len(payload)}"
        seq = seqs.get(key, 0)
        seqs[key] = seq + 1
        payload.append((resource, key, seq, json.dumps(item, default=str)))
    with conn:
        conn.execute("DELETE FROM mirror_rows WHERE resource = ?", (resource,))
        if payload:
            conn.executemany(
                "INSERT OR REPLACE INTO mirror_rows(resource, row_key, seq, data) "
                "VALUES (?, ?, ?, ?)",
                payload,
            )
        _rebuild_view(conn, resource)
    return len(payload)


# Top-level JSON keys promoted to view columns. Conservative: only plain
# identifiers become json_extract projections (exotic keys stay reachable
# through the data column), and the structural columns are reserved.
_SAFE_KEY = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
_RESERVED_KEYS = {"row_key", "seq", "data"}
_MAX_VIEW_KEYS = 200
_VIEW_KEY_SAMPLE = 2000


def _rebuild_view(conn: sqlite3.Connection, resource: str) -> None:
    """(Re)create a per-resource view: SELECT summary FROM tickets works."""
    keys: list[str] = []
    seen: set[str] = set()
    for (data,) in conn.execute(
        "SELECT data FROM mirror_rows WHERE resource = ? LIMIT ?",
        (resource, _VIEW_KEY_SAMPLE),
    ):
        try:
            obj = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        for key in obj:
            name = str(key)
            if (
                name in seen
                or name in _RESERVED_KEYS
                or not _SAFE_KEY.match(name)
                or len(keys) >= _MAX_VIEW_KEYS
            ):
                continue
            seen.add(name)
            keys.append(name)
    quoted = '"' + resource.replace('"', '""') + '"'
    conn.execute(f"DROP VIEW IF EXISTS {quoted}")
    if not keys:
        return
    projections = ", ".join(f"json_extract(data, '$.{name}') AS \"{name}\"" for name in keys)
    literal = resource.replace("'", "''")
    conn.execute(
        f"CREATE VIEW {quoted} AS SELECT row_key, seq, data, {projections} "
        f"FROM mirror_rows WHERE resource = '{literal}'"
    )


def ensure_views(conn: sqlite3.Connection) -> None:
    """Build views for any synced resource that lacks one (legacy DBs)."""
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    for (resource,) in conn.execute("SELECT DISTINCT resource FROM mirror_rows ORDER BY resource"):
        if resource not in existing:
            _rebuild_view(conn, resource)
    conn.commit()


def write_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO mirror_state("
            "resource, synced_at, rows, total, truncated, paging_ignored, "
            "labels_added, endpoint, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state["resource"],
                state["synced_at"],
                state["rows"],
                state.get("total"),
                int(bool(state.get("truncated"))),
                int(bool(state.get("paging_ignored"))),
                int(state.get("labels_added") or 0),
                state.get("endpoint") or "",
                state.get("error"),
            ),
        )


def read_state(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute("SELECT * FROM mirror_state ORDER BY resource")]


def state_for(path: Path, resource: str) -> dict[str, Any] | None:
    """Sync-state entry for one resource (ops use it for honesty notes)."""
    conn = open_existing(path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM mirror_state WHERE resource = ?", (resource,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_resource(path: Path, resource: str) -> list[dict]:
    """Materialize one resource's JSON rows (ops read through this)."""
    conn = open_existing(path)
    try:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT data FROM mirror_rows WHERE resource = ? ORDER BY row_key, seq",
                (resource,),
            )
        ]
    finally:
        conn.close()


def resource_names(path: Path) -> list[str]:
    conn = open_existing(path)
    try:
        return [
            row[0]
            for row in conn.execute("SELECT DISTINCT resource FROM mirror_rows ORDER BY resource")
        ]
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def sync_resources(
    client: Any,
    names: list[str],
    *,
    db_path: Path,
    max_records: int | None = DEFAULT_SYNC_LIMIT,
    labels: bool = True,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Sync each resource into the mirror; one resource's failure never
    stops the run (its error is recorded, prior rows stay intact)."""
    conn = connect(db_path)
    results: list[dict[str, Any]] = []
    try:
        for name in names:
            resource = get_resource(name)
            stats: dict[str, Any] = {}
            state: dict[str, Any] = {
                "resource": name,
                "synced_at": _utcnow(),
                "rows": 0,
                "total": None,
                "endpoint": resource.endpoint,
                # always present so JSON consumers can rely on the key
                "error": None,
            }
            try:
                rows = await list_all(
                    lambda _name=name, **kwargs: client.list_resource(_name, **kwargs),
                    page_size=100,
                    max_records=max_records,
                    list_key=resource.list_key,
                    stats=stats,
                    cursor_paging=resource.cursor_paging,
                    **SYNC_ORDER.get(name, {}),
                )
                # Bake tenant labels into the stored JSON (agent_id ->
                # agent_name, status_id -> status_name): ticket payloads ship
                # neither name (proven), and the ops filter on them offline.
                labels_added = await hydrate_items(client, rows, enabled=labels)
                stored = replace_rows(conn, name, rows)
                state.update(
                    rows=stored,
                    total=stats.get("record_count"),
                    truncated=bool(stats.get("truncated")),
                    paging_ignored=bool(stats.get("paging_ignored")),
                    labels_added=labels_added,
                )
            except Exception as exc:  # noqa: BLE001 - record and continue
                state["error"] = f"{type(exc).__name__}: {exc}"
                state["rows"] = _existing_row_count(conn, name)
            write_state(conn, state)
            results.append(state)
            if progress is not None:
                progress(state)
    finally:
        conn.close()
    ok = [r for r in results if not r.get("error")]
    return {
        "mirror": str(db_path),
        "resources": len(results),
        "synced": len(ok),
        "failed": len(results) - len(ok),
        "rows": sum(r["rows"] for r in ok),
        "truncated": [r["resource"] for r in results if r.get("truncated")],
        "results": results,
    }


def _existing_row_count(conn: sqlite3.Connection, resource: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM mirror_rows WHERE resource = ?", (resource,)
    ).fetchone()
    return int(row[0]) if row else 0
