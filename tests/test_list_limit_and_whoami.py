"""Tests for the list ceiling (--all opt-in) and `auth whoami`."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.token_cache import KeyringTokenCache
from halocli.utils import DEFAULT_LIST_LIMIT, list_all

runner = CliRunner()

# CI (and FORCE_COLOR runs) render click's rich errors with ANSI styling, which
# interleaves escape codes inside option names ("--all", "--check"). Strip them
# before substring assertions so tests do not depend on the color environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


# --------------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_list_all_reports_truncation_when_record_count_exceeds_limit() -> None:
    async def fetch(**kwargs: Any) -> Any:
        # Halo reports the true total even though the page is smaller.
        return {"clients": [{"id": kwargs["page_no"]}], "record_count": 99}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=1, max_records=5, stats=stats)

    assert len(rows) == 5
    assert stats["truncated"] is True
    assert stats["record_count"] == 99
    assert stats["returned"] == 5


@pytest.mark.asyncio
async def test_list_all_does_not_report_truncation_when_limit_reaches_total() -> None:
    async def fetch(**kwargs: Any) -> Any:
        if kwargs["page_no"] > 2:
            return {"clients": [], "record_count": 2}
        return {"clients": [{"id": kwargs["page_no"]}], "record_count": 2}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=1, max_records=2, stats=stats)

    # Hit max_records exactly at Halo's total: that is a complete read.
    assert len(rows) == 2
    assert stats["truncated"] is False


@pytest.mark.asyncio
async def test_list_all_exhausted_result_is_not_truncated() -> None:
    async def fetch(**kwargs: Any) -> Any:
        if kwargs["page_no"] > 1:
            return {"clients": [], "record_count": 1}
        return {"clients": [{"id": 1}], "record_count": 1}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=10, stats=stats)

    assert rows == [{"id": 1}]
    assert stats["truncated"] is False
    assert stats["record_count"] == 1


@pytest.mark.asyncio
async def test_list_all_max_pages_reports_truncation() -> None:
    async def fetch(**kwargs: Any) -> Any:
        return {"clients": [{"id": kwargs["page_no"]}], "record_count": 50}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=1, max_pages=2, stats=stats)

    assert len(rows) == 2
    assert stats["truncated"] is True
    assert stats["record_count"] == 50


@pytest.mark.asyncio
async def test_list_all_detects_ignored_paging_and_recovers_single_page() -> None:
    """Issue #24: /CRMNote answers page 2 with page 1 verbatim.

    The duplicate must never be appended, and one bounded recovery with
    ``count=<record_count>`` (the documented workaround) fetches the rest.
    """
    first_page = {"notes": [{"id": 1}, {"id": 2}], "record_count": 5}
    calls: list[dict[str, Any]] = []

    async def fetch(**kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        if kwargs.get("count") == 5:
            return {"notes": [{"id": i} for i in range(1, 6)], "record_count": 5}
        return first_page  # every page_no returns page 1 verbatim

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=2, stats=stats, toplevel_id="1")

    assert [row["id"] for row in rows] == [1, 2, 3, 4, 5]
    assert stats["paging_ignored"] is True
    assert stats["truncated"] is False
    assert stats["record_count"] == 5
    # page 1, duplicate page 2, exactly one recovery attempt
    assert len(calls) == 3
    assert calls[2]["count"] == 5
    assert calls[2]["page_no"] == 1
    assert calls[2]["toplevel_id"] == "1"  # caller params survive the recovery


@pytest.mark.asyncio
async def test_list_all_paging_ignored_and_count_ignored_is_honest_truncation() -> None:
    """If count is ignored too (TimesheetEvent shape): page 1 stands, flagged."""

    async def fetch(**kwargs: Any) -> Any:
        return {"notes": [{"id": 1}, {"id": 2}], "record_count": 5}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=2, stats=stats)

    assert rows == [{"id": 1}, {"id": 2}]  # never duplicated
    assert stats["paging_ignored"] is True
    assert stats["truncated"] is True
    assert stats["returned"] == 2


@pytest.mark.asyncio
async def test_list_all_pinned_count_skips_recovery() -> None:
    """An explicit count is the caller's choice: respect it, never override."""
    calls: list[dict[str, Any]] = []

    async def fetch(**kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        return {"notes": [{"id": 1}, {"id": 2}], "record_count": 5}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=2, stats=stats, count=2)

    assert rows == [{"id": 1}, {"id": 2}]
    assert len(calls) == 2  # page 1 + duplicate page 2; no recovery call
    assert stats["paging_ignored"] is True
    assert stats["truncated"] is True


@pytest.mark.asyncio
async def test_list_all_empty_page_with_unmet_total_is_truncated() -> None:
    """An empty page while Halo's total is unmet must not read as complete."""

    async def fetch(**kwargs: Any) -> Any:
        if kwargs["page_no"] == 1:
            return {"notes": [{"id": 1}, {"id": 2}], "record_count": 5}
        return {"notes": [], "record_count": 5}

    stats: dict[str, Any] = {}
    rows = await list_all(fetch, page_size=2, stats=stats)

    assert len(rows) == 2
    assert stats["truncated"] is True
    assert "paging_ignored" not in stats


def test_default_list_limit_is_sane() -> None:
    # Large enough to be useful, small enough that a bare `list` on a 137k-record
    # tenant cannot look like a hang.
    assert 0 < DEFAULT_LIST_LIMIT <= 1000


def _mock_halo(monkeypatch: pytest.MonkeyPatch, *, total: int) -> list[dict]:
    """Serve a Halo-shaped paged collection of ``total`` records.

    Pages are generated from page_size/page_no exactly as Halo does, so
    pagination is exercised rather than stubbed out.
    """
    async_client = httpx.AsyncClient
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        page_no = int(request.url.params.get("page_no", "1"))
        page_size = int(request.url.params.get("page_size", "100"))
        seen.append(dict(request.url.params))
        start = (page_no - 1) * page_size
        stop = min(start + page_size, total)
        items = [{"id": n} for n in range(start + 1, stop + 1)]
        return httpx.Response(200, json={"sites": items, "record_count": total})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    return seen


def test_list_defaults_to_ceiling_and_reports_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    # 900 records exist; the default ceiling stops at DEFAULT_LIST_LIMIT and must
    # say so rather than let a partial payload read as a complete one.
    _mock_halo(monkeypatch, total=900)

    result = runner.invoke(app, ["sites", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == DEFAULT_LIST_LIMIT
    assert payload["truncated"] is True
    assert payload["total_available"] == 900
    assert "--all" in plain(payload["hint"])


def test_list_all_flag_fetches_every_record(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_halo(monkeypatch, total=900)

    result = runner.invoke(app, ["sites", "list", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # --all ignores the ceiling and reads every record.
    assert payload["count"] == 900
    assert payload.get("truncated") is not True
    assert "hint" not in payload
    assert seen and "pageinate" in seen[0]


def test_list_all_rejects_combination_with_explicit_limits() -> None:
    result = runner.invoke(app, ["sites", "list", "--all", "--max-records", "5"])

    assert result.exit_code != 0
    assert "--all" in plain(result.output)


def _mock_halo_ignoring_paging(
    monkeypatch: pytest.MonkeyPatch, *, total: int, honor_count: bool
) -> list[dict]:
    """Serve the /CRMNote quirk from issue #24: page_no is ignored.

    ``count`` optionally works (crm-notes honours it; timesheet-event ignores
    count as well), and record_count always reports the true total.
    """
    async_client = httpx.AsyncClient
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        params = dict(request.url.params)
        seen.append(params)
        page_size = int(params.get("page_size", "100"))
        size = int(params.get("count", page_size)) if honor_count else page_size
        size = min(size, total)
        items = [{"id": n} for n in range(1, size + 1)]  # page_no deliberately unused
        return httpx.Response(200, json={"sites": items, "record_count": total})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    return seen


def test_list_recovers_single_page_when_halo_ignores_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The issue #24 symptom: 150 items, 50 distinct. After the fix: the full
    # 133 via one count=<total> recovery, flagged, zero duplicates.
    seen = _mock_halo_ignoring_paging(monkeypatch, total=133, honor_count=True)

    result = runner.invoke(app, ["sites", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = [item["id"] for item in payload["items"]]
    assert payload["count"] == 133
    assert len(set(ids)) == 133
    assert payload["paging_ignored"] is True
    assert payload.get("truncated") is not True
    assert seen and seen[-1].get("count") == "133"


def test_list_reports_paging_ignored_when_count_is_ignored_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # count ignored as well: page 1 stands alone, flagged and honestly truncated.
    _mock_halo_ignoring_paging(monkeypatch, total=133, honor_count=False)

    result = runner.invoke(app, ["sites", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 100  # default page size, never duplicated
    assert len({item["id"] for item in payload["items"]}) == 100
    assert payload["paging_ignored"] is True
    assert payload["truncated"] is True
    assert payload["total_available"] == 133
    assert "ignores paging" in plain(payload["hint"])


def test_explicit_max_records_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_halo(monkeypatch, total=900)

    result = runner.invoke(app, ["sites", "list", "--max-records", "2"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Explicit ceiling is honoured, and truncation is still reported.
    assert payload["count"] == 2
    assert payload["truncated"] is True


def test_small_result_is_not_reported_as_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_halo(monkeypatch, total=3)

    result = runner.invoke(app, ["sites", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 3
    assert "truncated" not in payload
    assert "hint" not in payload


# -------------------------------------------------------------------------- whoami


def _seed_token(scope: str) -> None:
    KeyringTokenCache().save(
        "default",
        {
            "access_token": "tok",
            "expires_at": 9999999999.0,
            "expires_in": 3600,
            "scope": scope,
            "token_type": "Bearer",
        },
    )


def test_whoami_reports_granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "id": 37,
                "name": "Thomas Bray",
                "email": "t@example.com",
                "team": "Infrastructure",
                "is_agent": True,
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    _seed_token("read:tickets read:customers")

    result = runner.invoke(app, ["auth", "whoami"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["identity"]["name"] == "Thomas Bray"
    assert payload["granted_scope"]["scope"] == ["read:customers", "read:tickets"]
    # profile requested scope differs from what Halo granted -> explained
    assert payload["requested_scope"] == "all"
    assert "scope_note" in payload


def test_whoami_check_reports_unreachable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.url.path.endswith("/Agent/me"):
            return httpx.Response(200, json={"id": 1, "name": "A"})
        return httpx.Response(403, text="")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    _seed_token("read:tickets")

    result = runner.invoke(app, ["auth", "whoami", "--check", "/Invoice"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    check = payload["checks"][0]
    assert check["endpoint"] == "/Invoice"
    assert check["reachable"] is False
    assert check["category"] == "permission"
    assert check["status_code"] == 403
    assert "whoami" in check["diagnostic"]
    assert payload["checks_all_reachable"] is False


def test_whoami_check_reports_reachable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.url.path.endswith("/Agent/me"):
            return httpx.Response(200, json={"id": 1, "name": "A"})
        return httpx.Response(200, json={"tickets": [{"id": 1}], "record_count": 1})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    _seed_token("read:tickets")

    result = runner.invoke(app, ["auth", "whoami", "--check", "Tickets"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    check = payload["checks"][0]
    # A bare name is normalised to a leading-slash path.
    assert check["endpoint"] == "/Tickets"
    assert check["reachable"] is True
    assert payload["checks_all_reachable"] is True


def test_whoami_help_loads() -> None:
    result = runner.invoke(app, ["auth", "whoami", "--help"])

    assert result.exit_code == 0
    assert "--check" in plain(result.output)
    assert "read-only" in plain(result.output)
