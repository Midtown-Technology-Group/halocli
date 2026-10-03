"""Local operator digests over the offline mirror: standup and triage.

Both read the synced JSON rows (never the network) and resolve names from
the labels baked in at sync time, falling back to a lookup against the
mirrored agents/clients/statuses rows.

Evidence-backed semantics (mirror_evidence.json):

- Tickets carry ``datecreated: null`` always; ``dateoccurred`` is the real
  creation timestamp (proven50/50) - ages and windows use it.
- ``dateclosed`` is a parseable ISO timestamp on closed rows - "closed in
  window" reads it directly instead of guessing from status.
- Status IDs are tenant-specific (Servosity's hardcoded 8/9 does not exist
  here - there is no status 8), so open/closed is decided by status NAME
  against a configurable set, never by hardcoded ids.
- ``hasbeenclosed`` correlates exactly with status Closed on this tenant
  (29/29 vs 21/21) and is the per-row fallback when a row has no baked
  status name (labels were off at sync time).

Excluded scope (documented, not silently missing): reopen detection and
time-logged aggregates need action history; /Actions unfiltered times out
(proven), so it is not synced and these digests do not pretend to cover it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from halocli.mirror import load_resource, state_for

# Tenant-configurable via --closed-status; ids are NOT used (tenant-specific).
DEFAULT_CLOSED_STATUSES: tuple[str, ...] = (
    "closed",
    "complete",
    "work completed",
    "resolved",
    "done",
)


class OpsError(ValueError):
    """Bad operator input or unusable mirror content."""


def _mirror_context(db_path: Path) -> dict[str, Any]:
    """Honesty block: a partial tickets mirror bounds what the digest means."""
    state = state_for(db_path, "tickets") or {}
    rows = state.get("rows")
    total = state.get("total")
    truncated = bool(state.get("truncated"))
    context: dict[str, Any] = {
        "tickets_rows": rows,
        "tickets_total": total,
        "tickets_truncated": truncated,
    }
    if truncated:
        context["hint"] = (
            f"Tickets mirror is partial ({rows} of {total or 'unknown'} rows, "
            "newest-first): results cover the synced window only. "
            "Run 'halocli sync --resource tickets --all' for the full set."
        )
    return context


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_when(value: str) -> datetime:
    """'24h' / '7d' / 'yesterday' / ISO datetime -> naive datetime."""
    text = (value or "").strip().lower()
    now = datetime.now()
    if text == "yesterday":
        return datetime(now.year, now.month, now.day) - timedelta(days=1)
    if text == "now":
        return now
    simple = text.replace("hours", "h").replace("days", "d")
    if simple.endswith(("h", "d")) and simple[:-1].isdigit():
        amount = int(simple[:-1])
        return now - timedelta(hours=amount) if simple[-1] == "h" else now - timedelta(days=amount)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OpsError(
            f"Cannot parse --since {value!r}: use '24h', '7d', 'yesterday' "
            "or an ISO datetime (2026-10-01T09:00)."
        ) from exc
    return parsed


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)[:19]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _name_maps(db_path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """agent_id -> name, client_id -> name, status_id -> name from mirrors."""
    agents = {
        str(row.get("id")): str(row.get("name") or row.get("friendlyname") or "")
        for row in load_resource(db_path, "agents")
        if row.get("id") is not None
    }
    clients = {
        str(row.get("id")): str(row.get("name") or "")
        for row in load_resource(db_path, "clients")
        if row.get("id") is not None
    }
    statuses = {
        str(row.get("id")): str(row.get("name") or "")
        for row in load_resource(db_path, "statuses")
        if row.get("id") is not None
    }
    return agents, clients, statuses


def _decorate(
    db_path: Path, tickets: list[dict]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Attach resolved names + age; return (open, context)."""
    agents, clients, statuses = _name_maps(db_path)
    now = datetime.now()

    def agent_name(row: dict) -> str:
        baked = row.get("agent_name")
        if baked:
            return str(baked)
        return agents.get(str(row.get("agent_id")), "")

    def client_name(row: dict) -> str:
        baked = row.get("client_name")
        if baked:
            return str(baked)
        return clients.get(str(row.get("client_id")), "")

    def status_name(row: dict) -> str:
        baked = row.get("status_name")
        if baked:
            return str(baked)
        return statuses.get(str(row.get("status_id")), "")

    decorated: list[dict[str, Any]] = []
    for row in tickets:
        occurred = _parse_ts(row.get("dateoccurred"))
        age_days = 0
        if occurred is not None:
            age_days = max(0, (now - occurred).days)
        decorated.append(
            {
                "row": row,
                "subject": str(row.get("summary") or row.get("subject") or ""),
                "agent": agent_name(row),
                "client": client_name(row),
                "status": status_name(row),
                "occurred": occurred,
                "age_days": age_days,
            }
        )
    return (
        {str(d["row"].get("id")): d for d in decorated},
        {"agents": agents, "clients": clients, "statuses": statuses, "now": now},
    )


def _is_open(entry: dict[str, Any], closed: set[str]) -> bool:
    row = entry["row"]
    name = entry["status"]
    if name:
        return _norm(name) not in closed
    # No baked/mirrored status name: fall back to the proven flag correlation
    # (true <=> status Closed on this tenant).
    return row.get("hasbeenclosed") is not True


def run_triage(
    db_path: Path,
    *,
    stale_days: int = 7,
    limit: int = 20,
    agent: str | None = None,
    closed_statuses: tuple[str, ...] = DEFAULT_CLOSED_STATUSES,
) -> dict[str, Any]:
    closed = {_norm(name) for name in closed_statuses}
    tickets = load_resource(db_path, "tickets")
    if not tickets:
        raise OpsError("Mirror has no tickets rows. Run 'halocli sync' first.")
    by_id, _ctx = _decorate(db_path, tickets)
    open_rows = [d for d in by_id.values() if _is_open(d, closed)]
    if agent:
        needle = _norm(agent)
        open_rows = [d for d in open_rows if needle in _norm(d["agent"])]
    open_rows.sort(key=lambda d: d["occurred"] or datetime.min, reverse=False)

    items = [
        {
            "id": d["row"].get("id"),
            "age_days": d["age_days"],
            "stale": d["age_days"] >= stale_days,
            "status": d["status"],
            "agent": d["agent"],
            "client": d["client"],
            "subject": d["subject"],
        }
        for d in open_rows[:limit]
    ]
    by_status: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    stale_count = 0
    for d in open_rows:
        by_status[d["status"] or "?"] = by_status.get(d["status"] or "?", 0) + 1
        by_agent[d["agent"] or "(unassigned)"] = by_agent.get(d["agent"] or "(unassigned)", 0) + 1
        if d["age_days"] >= stale_days:
            stale_count += 1
    return {
        "triage": {
            **_mirror_context(db_path),
            "open": len(open_rows),
            "stale": stale_count,
            "stale_days": stale_days,
            "closed_statuses": sorted(closed),
            "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
            "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1])),
            "shown": len(items),
            "items": items,
        }
    }


def run_standup(
    db_path: Path,
    *,
    since: str = "24h",
    agent: str | None = None,
    closed_statuses: tuple[str, ...] = DEFAULT_CLOSED_STATUSES,
) -> dict[str, Any]:
    closed = {_norm(name) for name in closed_statuses}
    since_dt = _parse_when(since)
    tickets = load_resource(db_path, "tickets")
    if not tickets:
        raise OpsError("Mirror has no tickets rows. Run 'halocli sync' first.")
    by_id, _ctx = _decorate(db_path, tickets)
    entries = list(by_id.values())
    if agent:
        needle = _norm(agent)
        entries = [d for d in entries if needle in _norm(d["agent"])]

    since_key = since_dt.isoformat(timespec="seconds")[:19]
    per_agent: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return per_agent.setdefault(
            name,
            {"closed_in_window": 0, "open_now": 0, "oldest_open_days": 0, "top_client": ""},
        )

    client_counts: dict[str, dict[str, int]] = {}
    for d in entries:
        name = d["agent"] or "(unassigned)"
        row = d["row"]
        closed_at = str(row.get("dateclosed") or "")[:19]
        in_window = bool(closed_at) and closed_at >= since_key
        is_open = _is_open(d, closed)
        created_in_window = bool(d["occurred"] and d["occurred"] >= since_dt)
        # "touched" = window closures, window creations, or still-open work:
        # open tickets are current workload and belong in the top-client view.
        touched = in_window or created_in_window or is_open
        if touched and d["client"]:
            counts = client_counts.setdefault(name, {})
            counts[d["client"]] = counts.get(d["client"], 0) + 1
        if in_window and not is_open:
            bucket(name)["closed_in_window"] += 1
        if is_open:
            slot = bucket(name)
            slot["open_now"] += 1
            slot["oldest_open_days"] = max(slot["oldest_open_days"], d["age_days"])

    for name, slot in per_agent.items():
        counts = client_counts.get(name, {})
        if counts:
            slot["top_client"] = max(counts.items(), key=lambda kv: kv[1])[0]

    ordered = sorted(
        ({"agent": name, **slot} for name, slot in per_agent.items()),
        key=lambda row: (-row["closed_in_window"], -row["open_now"], row["agent"]),
    )
    return {
        "standup": {
            **_mirror_context(db_path),
            "since": since_dt.isoformat(timespec="seconds"),
            "closed_in_window": sum(r["closed_in_window"] for r in ordered),
            "open_now": sum(r["open_now"] for r in ordered),
            "agents": ordered,
            "note": "time-logged/reopens need action history (/Actions not synced)",
        }
    }
