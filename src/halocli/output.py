from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table


console = Console()
error_console = Console(stderr=True)


def render(data: Any, *, output: str = "json", table_fields: tuple[str, ...] | None = None) -> None:
    if output == "table" and isinstance(data, dict) and isinstance(data.get("items"), list):
        _render_table(data["items"], table_fields=table_fields)
        return
    if output == "table" and isinstance(data, dict) and isinstance(data.get("item"), dict):
        _render_table([data["item"]], table_fields=table_fields)
        return
    console.print(json.dumps(data, indent=2, sort_keys=True, default=str))


def render_error(data: dict[str, Any]) -> None:
    error_console.print(json.dumps(data, indent=2, sort_keys=True, default=str))


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
            return present[:6]
    preferred = ["id", "summary", "name", "status_name", "client_name", "agent_name"]
    present = [column for column in preferred if any(column in item for item in items)]
    if present:
        return present[:6]
    if not items:
        return ["id", "name"]
    return list(items[0].keys())[:6]
