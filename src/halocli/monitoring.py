"""Normalized view over Halo's Backend Service Monitoring log tabs.

Halo exposes the same evidence through several routes with different envelopes
(bare lists, ``{"errors": [...]}``, ``{"results": [...]}``, and two singleton
documents). This module turns them into one ranked issue list so an operator or
an automation can answer "what is failing right now?" without knowing which tab
to open.

The reader is deliberately read-only and bounded. It never retries, never pages
an unbounded stream, and reports a stream that answered an unexpected status as
``unsupported``/``error`` in the snapshot instead of failing the whole call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from halocli.utils import normalize_halo_result, parse_page_result


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FAILURE_CODES = {408, 425, 429}


@dataclass(frozen=True)
class StreamSpec:
    """One Backend Service Monitoring surface.

    ``document`` marks the two singleton endpoints that describe current state
    rather than returning log rows. Nothing is inferred from the spec for those:
    the observed document keys are in ``collect_snapshot``.
    """

    name: str
    path: str
    list_key: str | None = None
    time_fields: tuple[str, ...] = ("date", "timestamp", "datecreated")
    params: tuple[tuple[str, str], ...] = ()
    optional: bool = False
    document: bool = False
    note: str = ""


STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec(
        "monitoring-events",
        "/TaskMonitorEvent",
        time_fields=("date", "timestamp", "created"),
        optional=True,
        note="Monitoring Events tab; HTTP 500 on the 2026-11-13 operator tenant.",
    ),
    StreamSpec(
        "integration-errors",
        "/IntegrationError",
        list_key="errors",
        time_fields=("date",),
    ),
    StreamSpec(
        "integration-requests",
        "/IntegrationRequest",
        list_key="requests",
        time_fields=("datelogged", "date"),
    ),
    StreamSpec(
        "integrator-logs",
        "/IntegratorLog",
        list_key="results",
        time_fields=("date", "datestarted", "timestamp"),
        optional=True,
    ),
    StreamSpec(
        "schedule-occurrences",
        "/ScheduleOccurrence",
        list_key="occurrences",
        time_fields=("creation_date", "date_processed", "timestamp"),
    ),
    StreamSpec(
        "outgoing-emails",
        "/Outgoingemail",
        time_fields=("datesent", "datecreated", "actiondate"),
        params=(("errorsonly", "true"),),
    ),
    StreamSpec(
        "notification-logs",
        "/NotificationLog",
        time_fields=("date", "timestamp", "datecreated"),
        optional=True,
    ),
    StreamSpec(
        "bulk-emails",
        "/BulkEmail",
        time_fields=("timestamp", "last_attempt_date"),
        optional=True,
    ),
    StreamSpec(
        "mail-campaign-logs",
        "/MailCampaignLog",
        list_key="mailcampaignlog",
        time_fields=("date", "timestamp"),
        optional=True,
    ),
    StreamSpec(
        "task-schedule",
        "/TaskSchedule",
        document=True,
        optional=True,
    ),
    StreamSpec(
        "backend-health",
        "/Health",
        document=True,
        optional=True,
    ),
)

STREAM_BY_NAME = {spec.name: spec for spec in STREAMS}

# Queue counts in /TaskSchedule that mean "background work is backing up".
QUEUE_KEYS = (
    "automations",
    "bulkemails",
    "escmsgs",
    "events",
    "outgoings",
    "scheduleoccurences",
    "schedules",
)


def parse_since(value: str) -> timedelta | None:
    """Parse ``24h`` / ``7d`` / ``90m`` / ``all`` into a window."""
    text = (value or "24h").strip().lower()
    if text in {"all", "0", "none"}:
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    unit = text[-1:]
    if unit not in units:
        raise ValueError("since must look like 90m, 24h, 7d, or all")
    number = text[:-1]
    try:
        amount = float(number)
    except ValueError as exc:
        raise ValueError("since must look like 90m, 24h, 7d, or all") from exc
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("since must be a non-negative duration or all")
    return timedelta(seconds=amount * units[unit])


def parse_time(value: Any) -> datetime | None:
    """Parse a Halo timestamp; unknown/blank values are ``None``, never now()."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_time(row: dict[str, Any], fields: tuple[str, ...]) -> datetime | None:
    for name in fields:
        parsed = parse_time(row.get(name))
        if parsed is not None:
            return parsed
    return None


def _text(row: dict[str, Any], *fields: str) -> str:
    for name in fields:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _issue_key(*parts: Any) -> str:
    return "|".join(str(part) for part in parts if part not in (None, ""))[:400]


def _issue(
    *,
    stream: str,
    key: str,
    severity: str,
    title: str,
    detail: str = "",
    seen: datetime | None = None,
    resource_id: Any = None,
) -> dict[str, Any]:
    return {
        "key": _issue_key(stream, key),
        "stream": stream,
        "severity": severity,
        "title": title,
        "detail": detail[:800],
        "resource_id": resource_id,
        "last_seen": seen.isoformat() if seen else None,
    }


def _code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _failure_code(value: Any) -> bool:
    code = _code(value)
    return code is not None and (code >= 400 or code in FAILURE_CODES)


def _severity_number(value: Any) -> str:
    # Halo's monitoring severity ordering is instance-configurable; numeric
    # levels are treated as "lower is worse" (Trace=0 .. Critical=5 is the
    # documented log-level order) and anything non-numeric as medium.
    number = _code(value)
    if number is None:
        text = str(value or "").strip().lower()
        if text in {"critical", "fatal", "error"}:
            return "critical"
        if text in {"warning", "warn"}:
            return "high"
        return "medium"
    if number <= 1:
        return "critical"
    if number == 2:
        return "high"
    if number == 3:
        return "medium"
    return "low"


def classify_row(spec: StreamSpec, row: dict[str, Any]) -> dict[str, Any] | None:
    """Return one issue for a failing row, or ``None`` when the row is healthy.

    This is intentionally conservative: a row is only an issue when a field
    explicitly carries a failure (an error message, a 4xx/5xx result code, or a
    non-success status). Absence of evidence is not treated as health.
    """
    seen = _row_time(row, spec.time_fields)
    name = spec.name

    if name == "integration-errors":
        module = _text(row, "module_name") or f"module {row.get('module_id')}"
        entity = _text(row, "entity_name") or str(row.get("entity_id") or "")
        entity_type = _text(row, "entity_type")
        return _issue(
            stream=name,
            key=_issue_key(module, entity_type, entity or row.get("id")),
            severity="high",
            title=f"Integration sync failed: {module}",
            detail=_text(row, "error") or f"{entity_type} {entity} failed to sync",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "integration-requests":
        if not _failure_code(row.get("resultcode")):
            return None
        kind = "outbound" if row.get("isoutgoing") else "inbound"
        operation = _text(row, "operation") or _text(row, "type") or "request"
        return _issue(
            stream=name,
            key=_issue_key(kind, operation, _text(row, "url") or row.get("msid") or row.get("id")),
            severity="high",
            title=f"Integration {kind} request failed: {operation}",
            detail=_text(row, "error") or _text(row, "response_body")
            or f"HTTP {row.get('resultcode')}",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "monitoring-events":
        return _issue(
            stream=name,
            key=_issue_key(row.get("log_type"), row.get("id") or _text(row, "message")),
            severity=_severity_number(row.get("severity")),
            title=_text(row, "message") or "Backend monitoring event",
            detail=_text(row, "description", "detail", "error"),
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "integrator-logs":
        error = _text(row, "error", "message")
        status = row.get("status")
        failed = bool(error) or (isinstance(status, str) and status.lower() in {"fail", "failed"})
        if not failed:
            return None
        return _issue(
            stream=name,
            key=_issue_key(row.get("module_id") or row.get("id")),
            severity="high",
            title="Halo Integrator run failed",
            detail=error or f"status {status}",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "schedule-occurrences":
        error = _text(row, "error")
        remaining = _code(row.get("retry_attempts_remaining"))
        exhausted = remaining == 0
        if not error and not exhausted:
            return None
        return _issue(
            stream=name,
            key=_issue_key(row.get("schedule_id") or row.get("id")),
            severity="critical" if exhausted and error else "high",
            title=_text(row, "description") or "Scheduled occurrence failed",
            detail=error or "Schedule occurrence has no retries remaining",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "outgoing-emails":
        return _issue(
            stream=name,
            key=_issue_key(row.get("id")),
            severity="medium",
            title=f"Outgoing email failed: {_text(row, 'subject') or 'no subject'}",
            detail=_text(row, "error", "likely_cause") or "Outgoing email is in the failed set",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "notification-logs":
        error = _text(row, "error", "message")
        status = str(row.get("status") or "").lower()
        if not error and status not in {"fail", "failed", "error"}:
            return None
        return _issue(
            stream=name,
            key=_issue_key(row.get("id")),
            severity="medium",
            title=_text(row, "description") or "Notification/webhook delivery failed",
            detail=error or f"status {row.get('status')}",
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "bulk-emails":
        status = str(row.get("status") or "").lower()
        retry = _text(row, "next_retry_date")
        if status not in {"fail", "failed", "error"} and not retry:
            return None
        return _issue(
            stream=name,
            key=_issue_key(row.get("id")),
            severity="medium",
            title=f"Bulk email failing: {_text(row, 'subject') or 'no subject'}",
            detail=f"status {row.get('status')}" + (f", next retry {retry}" if retry else ""),
            seen=seen,
            resource_id=row.get("id"),
        )

    if name == "mail-campaign-logs":
        failed = _code(row.get("failed")) or _code(row.get("failure_count"))
        if not failed:
            return None
        return _issue(
            stream=name,
            key=_issue_key(row.get("id")),
            severity="medium",
            title="Mail campaign has failed recipients",
            detail=f"{failed} recipient(s) failed",
            seen=seen,
            resource_id=row.get("id"),
        )

    return None


def classify_document(spec: StreamSpec, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify the two singleton documents (queue state and thread health)."""
    issues: list[dict[str, Any]] = []
    if spec.name == "task-schedule":
        last_run = parse_time(body.get("lastdailytaskrun"))
        if last_run is not None:
            age = datetime.now(timezone.utc) - last_run
            if age > timedelta(hours=36):
                issues.append(
                    _issue(
                        stream=spec.name,
                        key="daily-task-run",
                        severity="high",
                        title="Daily task scheduler has not run in over 36 hours",
                        detail=f"lastdailytaskrun={body.get('lastdailytaskrun')}",
                        seen=last_run,
                    )
                )
        return issues
    if spec.name == "backend-health":
        inuse = _code(body.get("inuse_worker_threads")) or 0
        maximum = _code(body.get("max_worker_threads")) or 0
        available = _code(body.get("available_worker_threads"))
        if available is not None and available <= 0:
            issues.append(
                _issue(
                    stream=spec.name,
                    key="worker-threads",
                    severity="critical",
                    title="Halo worker thread pool is exhausted",
                    detail=f"inuse={inuse} max={maximum}",
                )
            )
        elif maximum > 0 and inuse / maximum >= 0.9:
            issues.append(
                _issue(
                    stream=spec.name,
                    key="worker-threads",
                    severity="high",
                    title="Halo worker thread pool is above 90%",
                    detail=f"inuse={inuse} max={maximum}",
                )
            )
    return issues


ORDER_FIELDS = {"integration-errors": "date", "integration-requests": "datelogged"}


def _paging_params(spec: StreamSpec, limit: int) -> dict[str, Any]:
    params: dict[str, Any] = dict(spec.params)
    if spec.name in {"integration-errors", "integration-requests", "integrator-logs"}:
        params.update({"pageinate": True, "page_no": 1, "page_size": min(limit, 100)})
    else:
        params["count"] = limit
    # These logs default to oldest-first; without ordering the window filter
    # would inspect the first page of 2019 and call the instance healthy.
    if spec.name in ORDER_FIELDS:
        params.update({"order": ORDER_FIELDS[spec.name], "orderdesc": True})
    return params


def _stream_error(spec: StreamSpec, exc: Exception) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)
    message = str(exc) or type(exc).__name__
    unsupported = spec.optional and status_code in {404, 405, 500}
    return {
        "status": "unsupported" if unsupported else "error",
        "status_code": status_code,
        "error": message[:300],
        "rows": 0,
        "records": None,
        "filtered_out": 0,
        "issue_count": 0,
        "note": spec.note,
    }


async def _fetch_stream(
    client: Any,
    spec: StreamSpec,
    *,
    limit: int,
    cutoff: datetime | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = await client.request("GET", spec.path, params=_paging_params(spec, limit))
    except Exception as exc:  # noqa: BLE001 - one bad tab must not kill the snapshot
        return _stream_error(spec, exc), []

    if spec.document:
        body = normalize_halo_result(payload)
        if not isinstance(body, dict):
            return _stream_error(spec, ValueError("expected a document body")), []
        issues = classify_document(spec, body)
        meta = {
            "status": "ok",
            "rows": 1,
            "records": None,
            "filtered_out": 0,
            "issue_count": len(issues),
            "note": spec.note,
        }
        if spec.name == "task-schedule":
            meta["queue"] = {key: len(body.get(key) or []) for key in QUEUE_KEYS}
            meta["last_daily_task_run"] = body.get("lastdailytaskrun")
        return meta, issues

    page = parse_page_result(payload, list_key=spec.list_key)
    rows = [row for row in page.items if isinstance(row, dict)]
    kept: list[dict[str, Any]] = []
    filtered_out = 0
    for row in rows:
        seen = _row_time(row, spec.time_fields)
        if cutoff is not None and seen is not None and seen < cutoff:
            filtered_out += 1
            continue
        kept.append(row)
    available = len(kept)
    kept = kept[:limit]
    issues: list[dict[str, Any]] = []
    for row in kept:
        issue = classify_row(spec, row)
        if issue is not None:
            issues.append(issue)
    meta = {
        "status": "ok",
        "rows": len(kept),
        "records": page.record_count,
        "filtered_out": filtered_out,
        "issue_count": len(issues),
        "note": spec.note,
    }
    if available > limit:
        meta["truncated"] = True
    return meta, issues


async def collect_snapshot(
    client: Any,
    *,
    since: timedelta | None = timedelta(hours=24),
    limit: int = 200,
    streams: Iterable[str] | None = None,
    queue_threshold: int = 25,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read every selected stream once and return ranked issues plus per-stream state."""
    if limit < 1:
        raise ValueError("limit must be positive")
    current = now or datetime.now(timezone.utc)
    cutoff = current - since if since is not None else None
    selected = [STREAM_BY_NAME[name] for name in (streams or [spec.name for spec in STREAMS])]
    warnings: list[str] = []
    stream_meta: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []

    for spec in selected:
        meta, stream_issues = await _fetch_stream(
            client, spec, limit=limit, cutoff=cutoff
        )
        stream_meta[spec.name] = meta
        issues.extend(stream_issues)
        if meta["status"] == "error":
            warnings.append(f"{spec.name} could not be read: {meta.get('error', '')}")
        elif meta["status"] == "unsupported":
            warnings.append(
                f"{spec.name} is unavailable on this instance"
                + (f" ({meta.get('status_code')})" if meta.get("status_code") else "")
            )

    # Queue backlog is a derived signal: a pile-up is only actionable when it
    # crosses the caller's threshold, and the document alone has no issue row.
    queue = stream_meta.get("task-schedule", {}).get("queue") or {}
    for key, count in queue.items():
        if count > queue_threshold:
            issues.append(
                _issue(
                    stream="task-schedule",
                    key=f"backlog:{key}",
                    severity="medium",
                    title=f"Backend queue backlog: {key}",
                    detail=f"{count} pending item(s), threshold {queue_threshold}",
                )
            )
    issues = group_issues(issues)
    counts = {name: 0 for name in SEVERITY_RANK}
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    issues.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 9), item["key"]))

    return {
        "ok": True,
        "generated_at": current.isoformat(),
        "since": "all" if since is None else str(since),
        "limit": limit,
        "streams": stream_meta,
        "counts": {
            **counts,
            "total": len(issues),
            "events": sum(issue["count"] for issue in issues),
        },
        "issues": issues,
        "warnings": warnings,
    }


def group_issues(issues: list[dict[str, Any]], *, sample_limit: int = 10) -> list[dict[str, Any]]:
    """Collapse repeated failures into one issue with a count and time span.

    A broken integration writes one log row per attempt. Paging on raw rows
    would page once per retry; paging on a grouped issue is one alert that
    gets louder until it is fixed.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for issue in issues:
        key = issue["key"]
        current = grouped.get(key)
        if current is None:
            merged = dict(issue)
            merged["count"] = 1
            merged["first_seen"] = issue.get("last_seen")
            merged["resource_ids"] = (
                [issue["resource_id"]] if issue.get("resource_id") is not None else []
            )
            grouped[key] = merged
            continue
        current["count"] += 1
        if SEVERITY_RANK.get(issue["severity"], 9) < SEVERITY_RANK.get(
            current["severity"], 9
        ):
            current["severity"] = issue["severity"]
            current["title"] = issue["title"]
        seen = issue.get("last_seen")
        if seen and (current.get("last_seen") is None or seen > current["last_seen"]):
            current["last_seen"] = seen
            if issue.get("detail"):
                current["detail"] = issue["detail"]
        first = issue.get("last_seen")
        if first and (current.get("first_seen") is None or first < current["first_seen"]):
            current["first_seen"] = first
        resource_id = issue.get("resource_id")
        if resource_id is not None and len(current["resource_ids"]) < sample_limit:
            if resource_id not in current["resource_ids"]:
                current["resource_ids"].append(resource_id)
    return list(grouped.values())


def issues_only(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Trim a snapshot to the triage surface (issues + counts + warnings)."""
    return {
        "ok": snapshot.get("ok", False),
        "generated_at": snapshot.get("generated_at"),
        "since": snapshot.get("since"),
        "counts": snapshot.get("counts", {}),
        "issues": snapshot.get("issues", []),
        "warnings": snapshot.get("warnings", []),
    }
