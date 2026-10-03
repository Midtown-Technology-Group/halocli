from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

from halocli.labels import RESOLVERS, label_key_for


console = Console()
error_console = Console(stderr=True)


def render(data: Any, *, output: str = "json", table_fields: tuple[str, ...] | None = None) -> None:
    if output == "table" and isinstance(data, dict) and isinstance(data.get("items"), list):
        _render_table(data["items"], table_fields=table_fields)
        return
    if output == "table" and isinstance(data, dict) and isinstance(data.get("item"), dict):
        _render_table([data["item"]], table_fields=table_fields)
        return
    # JSON is data, never presentation: rich would soft-wrap at the terminal
    # width (corrupting the stream when piped) and, under FORCE_COLOR
    # environments, interleave ANSI codes. Write it raw to stdout instead.
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def render_error(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str), file=sys.stderr)


def warn(message: str) -> None:
    """Non-fatal operator warning on stderr (kept out of JSON payloads)."""
    error_console.print(f"[yellow]warning:[/yellow] {message}")


def progress(message: str) -> None:
    """Stderr progress note for long local operations (never pollutes JSON)."""
    error_console.print(f"[dim]{message}[/dim]")


def _render_table(items: list[dict], *, table_fields: tuple[str, ...] | None = None) -> None:
    table = Table(show_header=True, header_style="bold")
    columns = _columns(items, table_fields=table_fields)
    for column in columns:
        table.add_column(column)
    for item in items:
        table.add_row(*(str(item.get(column, "")) for column in columns))
    console.print(table)


def _columns(items: list[dict], *, table_fields: tuple[str, ...] | None = None) -> list[str]:
    if table_fields:
        present = [column for column in table_fields if any(column in item for item in items)]
        if present:
            return _prefer_labels(present, items)[:6]
    preferred = ["id", "summary", "name", "status_name", "client_name", "agent_name"]
    present = [column for column in preferred if any(column in item for item in items)]
    if present:
        return present[:6]
    if not items:
        return ["id", "name"]
    return list(items[0].keys())[:6]


def _prefer_labels(columns: list[str], items: list[dict]) -> list[str]:
    """Show the hydrated label instead of the raw id column (table only).

    JSON keeps every key; this is presentation: `status_id` renders as
    `status_name` once hydration actually resolved it for these rows.
    """
    out: list[str] = []
    for column in columns:
        if column.endswith("_id") and column in RESOLVERS:
            label_key = label_key_for(column)
            if label_key not in columns and any(label_key in item for item in items):
                if label_key not in out:
                    out.append(label_key)
                continue
        if column not in out:
            out.append(column)
    return out
