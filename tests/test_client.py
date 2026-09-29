from __future__ import annotations

import httpx
import pytest

from halocli.client import HaloClient
from halocli.config import HaloProfile


@pytest.mark.asyncio
async def test_client_reuses_cached_token() -> None:
    token_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, api_calls
        if request.url.path == "/auth/token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        api_calls += 1
        assert request.headers["Authorization"] == "Bearer abc"
        return httpx.Response(200, json={"clients": [{"id": api_calls}], "record_count": 1})

    profile = HaloProfile(
        tenant_url="https://halo.example.com",
        client_id="id",
        client_secret="secret",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HaloClient(profile, http=http)
        await client.request("GET", "/Client")
        await client.request("GET", "/Client")

    assert token_calls == 1
    assert api_calls == 2


@pytest.mark.asyncio
async def test_client_uses_retry_after_for_rate_limit(monkeypatch) -> None:
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "4"}, json={"error": "slow"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("halocli.client.asyncio.sleep", fake_sleep)
    profile = HaloProfile(tenant_url="https://halo.example.com", client_id="id", client_secret="secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HaloClient(profile, http=http)
        result = await client.request("GET", "/Client")

    assert result == {"ok": True}
    assert sleeps == [4]


@pytest.mark.asyncio
async def test_request_keeps_profile_timeout_unless_one_is_given() -> None:
    """An omitted timeout must not become ``timeout=None``.

    httpx treats those differently: omitting it uses the client default
    (``{'connect': 30.0, ...}`` for a 30s profile) while passing ``None`` sets
    every phase to ``None`` — i.e. no timeout at all. Supplying the parameter
    unconditionally therefore silently dropped the profile-wide timeout from
    every request that does not pass one (list, get, raw, search, MCP, todo),
    and neither the test suite nor a live smoke test can see it: MockTransport
    ignores timeouts and no request in those runs happened to stall. Caught by
    review on PR #18.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions.get("timeout")
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(200, json={"ok": True})

    profile = HaloProfile(
        tenant_url="https://halo.example.com",
        client_id="id",
        client_secret="secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=profile.timeout
    ) as http:
        client = HaloClient(profile, http=http)
        await client.request("GET", "/Client")  # no timeout argument
        await client.request("GET", "/Report/1", timeout=120.0)  # explicit

    default_ext = seen["/api/Client"]
    assert default_ext is not None, "profile timeout was dropped for an ordinary request"
    assert default_ext["connect"] == profile.timeout, default_ext

    explicit_ext = seen["/api/Report/1"]
    assert explicit_ext["connect"] == 120.0, explicit_ext
