from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


MAX_PAGE_SIZE = 100

# Default record ceiling for `halocli <resource> list`. Halo's /Tickets alone holds
# 137k records (~18 min of paging at 0.8s/page), so an unbounded default would make
# a bare `list` invocation appear to hang. Operators opt into the full fetch with --all.
DEFAULT_LIST_LIMIT = 500


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
    stats: dict[str, Any] | None = None,
    **params: Any,
) -> list[dict]:
    """Fetch pages until exhausted or a limit is hit.

    ``stats`` (optional, caller-owned) is populated with ``record_count`` (what
    Halo reports as the total), ``returned`` and ``truncated``. Truncation is
    reported rather than implied: a caller that stopped at a limit must be able
    to tell "these are all the records" from "these are the first N".
    """
    rows: list[dict] = []
    safe_page_size = clamp_page_size(page_size)
    page_no = 1
    record_count: int | None = None
    stopped_at_limit = False
    while True:
        if max_pages is not None and page_no > max_pages:
            stopped_at_limit = True
            break
        page = parse_page_result(
            await fetch(pageinate=True, page_no=page_no, page_size=safe_page_size, **params),
            list_key=list_key,
        )
        if page.record_count is not None:
            record_count = page.record_count
        if not page.items:
            break
        for item in page.items:
            rows.append(item)
            if max_records is not None and len(rows) >= max_records:
                stopped_at_limit = True
                break
        if stopped_at_limit:
            break
        if record_count is not None:
            if len(rows) >= record_count:
                break
        elif len(page.items) < safe_page_size:
            break
        page_no += 1
    if stats is not None:
        stats["record_count"] = record_count
        stats["returned"] = len(rows)
        # Stopping at a limit only counts as truncation when Halo says there is
        # more to fetch; hitting max_records exactly at the total is a full read.
        stats["truncated"] = stopped_at_limit and (
            record_count is None or len(rows) < record_count
        )
    return rows


def _first_list(data: dict[str, Any], list_key: str | None) -> tuple[list | None, str | None]:
    if list_key and isinstance(data.get(list_key), list):
        return data[list_key], list_key
    for key, value in data.items():
        if key != "columns" and isinstance(value, list):
            return value, key
    return None, None
