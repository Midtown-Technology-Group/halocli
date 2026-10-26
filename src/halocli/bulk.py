"""Bounded JSONL bulk driver (issue #73).

HaloPSA has no bulk API: its own "bulk update" tooling is PowerShell looping over
the REST API. This module is the HaloCLI shape of that loop, and it inherits the
house write contract rather than inventing a second one:

* a plan is **JSONL** - one operation per line - so a human can review, diff and
  version it;
* ``preview_plan`` performs **zero network calls** and returns the exact wire
  operations, the method histogram and the write count;
* execution is **sequential** (Halo's fair-use limit rewards it and it keeps the
  failure story simple), stops at the first failure unless ``continue_on_error``
  is set, and reports every attempted item with its own outcome.

Parsing is strict on purpose: an unknown method, a non-object line or a non-string
query parameter fails the whole plan before anything is sent. A bulk run is never
a blind blast - it is a reviewed plan applied in order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

BULK_PREVIEW_LIMIT = 25

# Fields the vendored spec does not declare but our tenants enforce at the wire.
# Halo's spec declares no required properties anywhere, so a plan missing one of
# these previewed clean and died on the first real call ("Category is mandatory").
# These are house knowledge, kept deliberately small and keyed by (method, path):
# add a row with the exact wire error it prevents, never a guess.
HOUSE_REQUIRED_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("POST", "/tickets"): (
        "category_1",  # Halo 400: "Category is mandatory"
    ),
}


class BulkError(ValueError):
    """A plan line could not be turned into a safe operation."""


@dataclass(frozen=True)
class BulkItem:
    """One reviewed operation from the plan."""

    index: int
    lineno: int
    method: str
    path: str
    params: dict[str, str] | None = None
    body: Any = None

    @property
    def write(self) -> bool:
        return self.method in WRITE_METHODS

    def as_operation(self) -> dict[str, Any]:
        operation: dict[str, Any] = {
            "index": self.index,
            "lineno": self.lineno,
            "method": self.method,
            "path": self.path,
        }
        if self.params:
            operation["params"] = self.params
        if self.body is not None:
            operation["body"] = self.body
        return operation


def _normalize_params(raw: Any, *, where: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BulkError(f"{where}: 'params' must be an object of string values")
    params: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise BulkError(f"{where}: 'params' keys must be strings")
        if isinstance(value, str):
            params[key] = value
        elif isinstance(value, bool):
            params[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            params[key] = str(value)
        else:
            raise BulkError(f"{where}: params[{key!r}] must be a scalar value")
    return params


def parse_jsonl(text: str, *, source: str = "<plan>") -> list[BulkItem]:
    """Turn JSONL text into reviewed items, or raise :class:`BulkError`.

    Blank lines and lines starting with ``#`` are ignored so a plan can carry
    comments. Every other line must be a JSON object with a recognized ``method``
    and a ``/``-rooted ``path``.
    """
    items: list[BulkItem] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{source}:{lineno}"
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BulkError(f"{where}: invalid JSON ({exc.msg})") from exc
        if not isinstance(payload, dict):
            raise BulkError(f"{where}: each line must be a JSON object")
        method = str(payload.get("method") or "GET").strip().upper()
        if method not in ALLOWED_METHODS:
            raise BulkError(
                f"{where}: method {method!r} is not allowed; use one of "
                + ", ".join(ALLOWED_METHODS)
            )
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise BulkError(f"{where}: 'path' is required")
        path = path.strip()
        if not path.startswith("/"):
            raise BulkError(f"{where}: 'path' must start with '/' and be relative to the API root")
        items.append(
            BulkItem(
                index=len(items),
                lineno=lineno,
                method=method,
                path=path,
                params=_normalize_params(payload.get("params"), where=where),
                body=payload.get("body"),
            )
        )
    if not items:
        raise BulkError(f"{source}: no operations found")
    return items


def house_rule_problems(item: BulkItem) -> list[str]:
    """Field/shape rules Halo enforces but the vendored spec does not declare."""
    problems: list[str] = []
    required = HOUSE_REQUIRED_FIELDS.get((item.method, _canonical_path(item.path)), ())
    if required:
        entries = item.body if isinstance(item.body, list) else [item.body]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            missing = [name for name in required if entry.get(name) in (None, "", [], {})]
            if missing:
                where = f"body[{index}]" if isinstance(item.body, list) else "body"
                problems.append(
                    f"line {item.lineno}: {where} is missing {', '.join(missing)} "
                    f"(Halo rejects {item.method} {item.path} without it)"
                )
    return problems


def _canonical_path(path: str) -> str:
    """Lower-case the collection path so '/Tickets' and '/tickets' match one rule."""
    return path.rstrip("/").lower()


def plan_problems(items: Iterable[BulkItem]) -> list[str]:
    """Every problem the preview can catch before a plan goes near the wire.

    Combines the vendored-spec check (``validate_request``) with the house rules
    above, so a plan that would fail halfway through a run fails up front instead.
    """
    from halocli.schema import validate_request

    problems: list[str] = []
    for item in items:
        if item.write:
            for problem in validate_request(item.method, item.path, item.body):
                if not problem.startswith("warning: "):
                    problems.append(f"line {item.lineno}: {problem}")
        problems.extend(house_rule_problems(item))
    return problems


def preview_plan(
    items: Iterable[BulkItem],
    *,
    source: str = "<plan>",
    limit: int = BULK_PREVIEW_LIMIT,
) -> dict[str, Any]:
    """Describe a plan without touching the network.

    This is the review gate: the same items execute unchanged, so an operator
    approves exactly what was printed.
    """
    ordered = list(items)
    writes = [item for item in ordered if item.write]
    counts: dict[str, int] = {}
    for item in ordered:
        counts[item.method] = counts.get(item.method, 0) + 1
    shown = ordered[:limit]
    payload: dict[str, Any] = {
        "ok": True,
        "apply": False,
        "source": source,
        "count": len(ordered),
        "writes": len(writes),
        "methods": counts,
        "operations": [item.as_operation() for item in shown],
        "hint": (
            f"Preview only - zero network calls. Showing {len(shown)} of {len(ordered)} "
            "operations in plan order. Re-run the identical plan with --apply --yes to "
            "execute it sequentially; the first failure stops the run unless "
            "--continue-on-error is passed."
        ),
    }
    if len(ordered) > len(shown):
        payload["truncated"] = True
        payload["hint"] += f" Raise --limit above {limit} to see the rest."
    return payload


async def execute_plan(
    client: Any,
    items: Iterable[BulkItem],
    *,
    continue_on_error: bool = False,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Apply reviewed items in order through ``client``.

    Returns a per-item outcome list; ``applied``/``failed``/``skipped`` sum to the
    plan size, so an early stop is explicit rather than implied. ``client`` is
    duck-typed (``await client.raw(...)``) so tests never touch a tenant.
    """
    ordered = list(items)
    results: list[dict[str, Any]] = []
    for item in ordered:
        entry: dict[str, Any] = {
            "index": item.index,
            "lineno": item.lineno,
            "method": item.method,
            "path": item.path,
        }
        try:
            data = await client.raw(item.method, item.path, params=item.params, body=item.body)
        except Exception as exc:  # isolated per item, categorised when HaloCLIError
            entry.update(
                ok=False,
                category=getattr(exc, "category", "unknown"),
                status_code=getattr(exc, "status_code", None),
                error=f"{type(exc).__name__}: {exc}" if not hasattr(exc, "category") else str(exc),
            )
        else:
            entry.update(ok=True, data=data)
        results.append(entry)
        if on_result is not None:
            on_result(entry)
        if not entry["ok"] and not continue_on_error:
            break
    applied = sum(1 for entry in results if entry["ok"])
    failed = sum(1 for entry in results if not entry["ok"])
    payload: dict[str, Any] = {
        "ok": failed == 0,
        "apply": True,
        "count": len(ordered),
        "executed": len(results),
        "applied": applied,
        "failed": failed,
        "skipped": len(ordered) - len(results),
        "results": results,
    }
    if payload["skipped"]:
        payload["hint"] = (
            "Run stopped at the first failure; the remaining operations were not sent. "
            "Fix the plan (or pass --continue-on-error) and re-run."
        )
    return payload
