from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


MAX_PAGE_SIZE = 100


@dataclass
class PageResult:
    items: list[dict]
    list_key: str | None
    record_count: int | None


def normalize_halo_result(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: normalize_halo_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize_halo_result(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: normalize_halo_result(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def clamp_page_size(page_size: int) -> int:
    return max(1, min(int(page_size), MAX_PAGE_SIZE))


def parse_page_result(result: Any, *, list_key: str | None = None) -> PageResult:
    data = normalize_halo_result(result)
    if isinstance(data, list):
        return PageResult(items=data, list_key=None, record_count=len(data))
    if not isinstance(data, dict):
        return PageResult(items=[], list_key=None, record_count=0)

    raw_items, detected_key = _first_list(data, list_key)
    items = [normalize_halo_result(item) for item in (raw_items or [])]
    record_count = data.get("record_count", len(items))
    try:
        record_count = int(record_count)
    except (TypeError, ValueError):
        record_count = len(items)
    return PageResult(items=items, list_key=detected_key, record_count=record_count)


def coerce_batch_response(result: Any) -> list[dict]:
    page = parse_page_result(result)
    if page.items:
        return page.items
    data = normalize_halo_result(result)
    if isinstance(data, dict) and data:
        return [data]
    return []


async def list_all(
    fetch: Callable[..., Awaitable[Any]],
    *,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int | None = None,
    max_records: int | None = None,
    list_key: str | None = None,
    **params: Any,
) -> list[dict]:
    rows: list[dict] = []
    safe_page_size = clamp_page_size(page_size)
    page_no = 1
    while True:
        if max_pages is not None and page_no > max_pages:
            break
        page = parse_page_result(
            await fetch(pageinate=True, page_no=page_no, page_size=safe_page_size, **params),
            list_key=list_key,
        )
        if not page.items:
            break
        for item in page.items:
            rows.append(item)
            if max_records is not None and len(rows) >= max_records:
                return rows
        if page.record_count is not None:
            if len(rows) >= page.record_count:
                break
        elif len(page.items) < safe_page_size:
            break
        page_no += 1
    return rows


def _first_list(data: dict[str, Any], list_key: str | None) -> tuple[list | None, str | None]:
    if list_key and isinstance(data.get(list_key), list):
        return data[list_key], list_key
    for key, value in data.items():
        if key != "columns" and isinstance(value, list):
            return value, key
    return None, None
