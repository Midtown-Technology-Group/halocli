from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from halocli.client import HaloClient
from halocli.config import HaloProfile
from halocli.errors import HaloCLIError

Handler = Callable[[httpx.Request], httpx.Response]

TOKEN_RESPONSE = {"access_token": "abc", "expires_in": 3600}


def profile() -> HaloProfile:
    return HaloProfile(
        tenant_url="https://halo.example.com", client_id="id", client_secret="secret"
    )


def token_if_needed(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/auth/token":
        return httpx.Response(200, json=TOKEN_RESPONSE)
    return None


def client_for(handler: Handler) -> HaloClient:
    return HaloClient(profile(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class NonSeekableFile:
    """Minimal file-like object with no ``seek``: reading consumes it for good."""

    def __init__(self, content: bytes, name: str = "notes.txt") -> None:
        self._content = content
        self._offset = 0
        self.name = name

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._content[self._offset :]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_json_response_is_parsed_like_before() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, json={"clients": [{"id": 1}], "record_count": 1})

    async with client_for(handler) as client:
        result = await client.request("GET", "/Client")

    assert result == {"clients": [{"id": 1}], "record_count": 1}
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_empty_success_body_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(204)

    async with client_for(handler) as client:
        assert await client.request("DELETE", "/Client/1") is None


@pytest.mark.asyncio
async def test_multipart_upload_reaches_transport_with_files() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(201, json={"id": 7})

    async with client_for(handler) as client:
        result = await client.request(
            "POST",
            "/Attachments",
            data={"ticket_id": "12"},
            files={"file": ("notes.txt", b"file-bytes", "text/plain")},
        )

    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert b"file-bytes" in captured["body"]
    assert b'name="ticket_id"' in captured["body"]
    assert result == {"id": 7}


@pytest.mark.asyncio
async def test_multipart_retry_resends_full_body_for_non_seekable_file(monkeypatch) -> None:
    async def fake_sleep(value: float) -> None:
        return None

    monkeypatch.setattr("halocli.client.asyncio.sleep", fake_sleep)
    attempts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        attempts.append(request.content)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(201, json={"id": 9})

    async with client_for(handler) as client:
        result = await client.request(
            "POST",
            "/Attachments",
            data={"ticket_id": "12"},
            files={"file": ("notes.txt", NonSeekableFile(b"file-bytes"))},
        )

    assert result == {"id": 9}
    assert len(attempts) == 2
    # The retried upload must carry the full file, not an exhausted stream.
    assert b"file-bytes" in attempts[0]
    assert b"file-bytes" in attempts[1]


@pytest.mark.asyncio
async def test_json_body_still_sent_when_no_files_or_data() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    async with client_for(handler) as client:
        await client.request("POST", "/Tickets", json_body={"subject": "hi"})

    assert captured["content_type"].startswith("application/json")
    assert json.loads(captured["body"]) == {"subject": "hi"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("application/pdf", b"%PDF-1.7 \x00\x01"),
        ("application/octet-stream", b"\x89PNG\r\n\x1a\n"),
        ("text/csv; charset=utf-8", b"id,name\n1,Acme\n"),
        ("application/zip", b"PK\x03\x04"),
        ("image/png", b"\x89PNG\r\n\x1a\n"),
    ],
)
async def test_non_json_success_body_returns_raw_bytes(content_type: str, payload: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, content=payload, headers={"content-type": content_type})

    async with client_for(handler) as client:
        result = await client.request("GET", "/Report/export")

    assert isinstance(result, bytes)
    assert result == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        ("text/plain", b"123"),
        ("text/plain", b"null"),
        ("text/plain", b'{ "id": 3 }\n'),
        ("text/csv; charset=utf-8", b"id,name\n1,Acme\n"),
    ],
)
async def test_declared_non_json_media_type_is_never_parsed_as_json(
    content_type: str, payload: bytes
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, content=payload, headers={"content-type": content_type})

    async with client_for(handler) as client:
        result = await client.request("GET", "/Report/export")

    assert isinstance(result, bytes)
    assert result == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"id": 3}', {"id": 3}),  # sloppy endpoint: no header, still JSON
        (b"plain words", b"plain words"),
    ],
)
async def test_missing_content_type_falls_back_to_json_then_bytes(
    payload: bytes, expected: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, content=payload)

    async with client_for(handler) as client:
        result = await client.request("GET", "/Client")

    assert result == expected


@pytest.mark.asyncio
async def test_declared_json_body_that_fails_to_parse_still_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, content=b'{"id":', headers={"content-type": "application/json"})

    async with client_for(handler) as client:
        with pytest.raises(ValueError):
            await client.request("GET", "/Client")


@pytest.mark.asyncio
async def test_download_returns_raw_bytes_and_forwards_params() -> None:
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        seen_params["format"] = request.url.params.get("format", "")
        return httpx.Response(
            200,
            content=b"%PDF-1.7 invoice",
            headers={"content-type": "application/pdf"},
        )

    async with client_for(handler) as client:
        result = await client.download("/Invoice/5/export", params={"format": "pdf"})

    assert result == b"%PDF-1.7 invoice"
    assert seen_params == {"format": "pdf"}


@pytest.mark.asyncio
async def test_download_preserves_json_body_bytes_verbatim() -> None:
    raw = b'{ "id": 3 }\n'

    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    async with client_for(handler) as client:
        result = await client.download("/Attachments/3")

    assert result == raw


@pytest.mark.asyncio
async def test_download_returns_json_response_as_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(200, json={"id": 3})

    async with client_for(handler) as client:
        result = await client.download("/Attachments/3")

    assert isinstance(result, bytes)
    assert json.loads(result.decode()) == {"id": 3}


@pytest.mark.asyncio
async def test_binary_error_body_does_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(
            400,
            content=b"\x89PNG\r\n\x1a\n\x00\xff",
            headers={"content-type": "application/octet-stream"},
        )

    async with client_for(handler) as client:
        with pytest.raises(HaloCLIError) as excinfo:
            await client.request(
                "POST", "/Attachments", files={"file": ("a.png", b"x", "image/png")}
            )

    error = excinfo.value
    assert error.status_code == 400
    assert error.category == "validation"
    assert "(400)" in str(error)
    assert "binary body" in str(error)


@pytest.mark.asyncio
async def test_empty_error_body_does_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(404, headers={"content-type": "application/pdf"})

    async with client_for(handler) as client:
        with pytest.raises(HaloCLIError) as excinfo:
            await client.request("GET", "/Client/999")

    error = excinfo.value
    assert error.status_code == 404
    assert error.category == "not_found"
    assert "(404)" in str(error)


@pytest.mark.asyncio
async def test_json_error_body_is_still_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = token_if_needed(request)
        if token is not None:
            return token
        return httpx.Response(403, json={"error": "forbidden"})

    async with client_for(handler) as client:
        with pytest.raises(HaloCLIError) as excinfo:
            await client.request("GET", "/Client")

    error = excinfo.value
    assert error.status_code == 403
    assert error.category == "permission"
    assert error.response_body is not None
    assert "forbidden" in error.response_body
