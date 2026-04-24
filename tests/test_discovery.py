from __future__ import annotations

import httpx
import pytest

from halocli.discovery import DiscoveryStatus, discover_auth


@pytest.mark.asyncio
async def test_discovery_classifies_interactive_supported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://halo.example.com/auth/authorize",
                    "token_endpoint": "https://halo.example.com/auth/token",
                    "grant_types_supported": ["authorization_code", "client_credentials"],
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await discover_auth("https://halo.example.com", http=http)

    assert result.status == DiscoveryStatus.INTERACTIVE_SUPPORTED
    assert result.authorization_endpoint == "https://halo.example.com/auth/authorize"


@pytest.mark.asyncio
async def test_discovery_classifies_client_credentials_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(404)
        if request.url.path == "/auth/token":
            return httpx.Response(400, json={"error": "invalid_request"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await discover_auth("https://halo.example.com", http=http)

    assert result.status == DiscoveryStatus.CLIENT_CREDENTIALS_ONLY
    assert "client-credentials" in result.recommendation


@pytest.mark.asyncio
async def test_discovery_classifies_unknown_when_no_signal() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as http:
        result = await discover_auth("https://halo.example.com", http=http)

    assert result.status == DiscoveryStatus.UNKNOWN
