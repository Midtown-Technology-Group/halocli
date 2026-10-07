from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from halocli.config import HaloProfile
from halocli.errors import HaloCLIError, classify_error
from halocli.models import TokenPayload
from halocli.resources import get_resource
from halocli.token_cache import KeyringTokenCache, TokenCache


def _require_http_url(url: str, what: str) -> str:
    """Validate a config-supplied endpoint before any request (S5144).

    Profile/auth URLs come from operator-set config; a forged or
    malformed value must never reach the HTTP layer.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        raise HaloCLIError(f"{what} must be a plain http(s) URL: {url!r}")
    return url


class HaloClient:
    def __init__(
        self,
        profile: HaloProfile,
        *,
        profile_name: str = "default",
        http: httpx.AsyncClient | None = None,
        file_token_cache: TokenCache | None = None,
    ) -> None:
        self.profile = profile
        # Prefer the name the profile resolved from (single-profile fallback
        # makes 'default' the wrong token-cache key).
        self.profile_name = getattr(profile, "profile_name", None) or profile_name
        self._http = http
        self._file_token_cache = file_token_cache
        self._token: str | None = None
        self._expires_at = 0.0

    async def __aenter__(self) -> "HaloClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.profile.timeout)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
        as_bytes: bool = False,
        # per-call timeout is intentional: forwarded only when set (timeout_kwargs below)
        timeout: float | None = None,  # NOSONAR
    ) -> Any:
        """Send an authenticated HaloPSA request.

        ``files``/``data`` are forwarded to httpx so callers can perform
        multipart uploads (attachments) and form posts; when either is supplied
        ``json_body`` is not sent. File-like payloads in ``files`` are buffered
        into ``bytes`` first, so a retried upload always resends identical bytes.

        Response contract for ``status < 300``:

        * ``as_bytes=True`` -> the raw ``bytes`` body, never parsed
        * empty body -> ``None``
        * declared JSON media type -> the parsed object (unchanged from earlier
          versions); an unparseable declared-JSON body still raises
        * declared non-JSON media type (text/plain, text/csv, octet-stream, pdf,
          zip, image, ...) -> the raw ``bytes`` body instead of raising
        * no ``Content-Type`` header -> best-effort JSON parse, falling back to
          the raw ``bytes`` body for endpoints that omit the header

        Callers distinguish the raw case with ``isinstance(result, bytes)``.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.profile.timeout)
        token = await self._access_token()
        url = self._url(path)
        headers = {"Authorization": f"Bearer {token}"}
        body_kwargs = _body_kwargs(json_body, _buffer_files(files), data)

        last_response: httpx.Response | None = None
        # Only forward timeout when the caller supplied one. Passing None is NOT
        # "use the client default" in httpx: it means connect/read/write/pool
        # all become None, i.e. no timeout at all, which would silently drop the
        # profile-wide timeout from every request that omits the argument.
        timeout_kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        # loop-boundary sanitizer (S6680): the retry count is a config
        # value - coerce and CLAMP it before it bounds any loop
        # (range(clamped + 1) keeps the original attempt semantics)
        retries = max(0, min(int(self.profile.max_retries), 20))
        for attempt in range(retries + 1):
            response = await self._http.request(
                method.upper(),
                url,
                params=params,
                headers=headers,
                # Report execution passes a larger timeout: it waits on a
                # multi-megabyte result set instead of the profile default.
                **timeout_kwargs,
                **body_kwargs,
            )
            last_response = response
            if response.status_code < 300:
                if as_bytes:
                    return response.content
                return _success_payload(response)
            if response.status_code == 401 and attempt == 0:
                self._token = None
                headers["Authorization"] = f"Bearer {await self._access_token()}"
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                await asyncio.sleep(self._retry_wait(response, attempt))
                continue
            break

        assert last_response is not None
        raise _response_error(last_response, endpoint=self._endpoint(path))

    async def list_resource(self, resource: str, **params: Any) -> Any:
        return await self.request("GET", get_resource(resource).endpoint, params=params)

    async def get_resource(self, resource: str, item_id: str | int) -> Any:
        resource_def = get_resource(resource)
        return await self.request("GET", f"{resource_def.endpoint}/{item_id}")

    async def raw(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        return await self.request(method, path, params=params, json_body=body)

    async def download(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        """GET ``path`` (file, attachment, export) and return the body as ``bytes``.

        The body is returned verbatim: it is never parsed, so even a JSON answer
        keeps its exact bytes (whitespace, newlines, checksum) unchanged.
        """
        return await self.request("GET", path, params=params, as_bytes=True)

    async def test_auth(self) -> Any:
        return await self.request("GET", "/Agent/me")

    async def _access_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        if self.profile.auth_mode == "halo_interactive":
            return await self._interactive_access_token()
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.profile.timeout)
        response = await self._http.post(
            _require_http_url(self.profile.auth_token_url, "auth token URL"),
            data={
                "grant_type": "client_credentials",
                "client_id": self.profile.client_id,
                "client_secret": self.profile.client_secret,
                "scope": self.profile.scope,
            },
        )
        if response.status_code >= 300:
            raise _response_error(response, endpoint="/auth/token")
        token = TokenPayload.model_validate(response.json())
        self._token = token.access_token
        self._expires_at = time.time() + token.expires_in
        return self._token

    async def _interactive_access_token(self) -> str:
        token_data = self._load_interactive_token()
        if not token_data:
            raise RuntimeError(
                f"No interactive token found for profile '{self.profile_name}'. "
                f"Run 'halocli auth login --profile {self.profile_name}' first."
            )
        expires_at = float(token_data.get("expires_at") or 0)
        access_token = token_data.get("access_token")
        if isinstance(access_token, str) and time.time() < expires_at - 60:
            self._token = access_token
            self._expires_at = expires_at
            return access_token
        refresh_token = token_data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError(
                f"Interactive token for profile '{self.profile_name}' is expired and has "
                "no refresh token. Run 'halocli auth login' again."
            )
        refreshed = await self._refresh_interactive_token(refresh_token)
        merged = {**token_data, **refreshed}
        self._save_interactive_token(merged)
        self._token = str(merged["access_token"])
        self._expires_at = float(merged["expires_at"])
        return self._token

    async def _refresh_interactive_token(self, refresh_token: str) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.profile.timeout)
        data = {
            "grant_type": "refresh_token",
            "client_id": self.profile.client_id,
            "refresh_token": refresh_token,
        }
        if self.profile.client_secret:
            data["client_secret"] = self.profile.client_secret
        response = await self._http.post(
            _require_http_url(
                self.profile.token_endpoint or self.profile.auth_token_url,
                "token endpoint URL",
            ),
            data=data,
        )
        if response.status_code >= 300:
            raise _response_error(response, endpoint="/auth/token")
        payload = TokenPayload.model_validate(response.json())
        token_data = payload.model_dump(exclude_none=True)
        token_data["expires_at"] = time.time() + payload.expires_in
        return token_data

    def _load_interactive_token(self) -> dict[str, Any] | None:
        if self._file_token_cache is not None:
            return self._file_token_cache.load(self.profile_name)
        return KeyringTokenCache().load(self.profile_name)

    def _save_interactive_token(self, token_data: dict[str, Any]) -> None:
        if self._file_token_cache is not None:
            self._file_token_cache.save(self.profile_name, token_data)
            return
        KeyringTokenCache().save(self.profile_name, token_data)

    def _url(self, path: str) -> str:
        clean_path = path.strip()
        if not clean_path.startswith("/"):
            clean_path = "/" + clean_path
        if clean_path.lower().startswith("/api/"):
            clean_path = clean_path[4:]
        # origin guard (S5144): no matter how the path is spelled, the
        # request URL must stay on the tenant origin - concat can never
        # re-target scheme/host, and we check it anyway
        url = f"{self.profile.api_base_url}{clean_path}"
        base = urlsplit(self.profile.api_base_url)
        got = urlsplit(url)
        if (got.scheme, got.netloc) != (base.scheme, base.netloc):
            raise HaloCLIError(f"path escapes the tenant origin: {path!r}")
        return url

    @staticmethod
    def _endpoint(path: str) -> str:
        clean_path = path if path.startswith("/") else f"/{path}"
        return clean_path if clean_path.lower().startswith("/api/") else f"/api{clean_path}"

    @staticmethod
    def _retry_wait(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return float(2**attempt)


def _body_kwargs(json_body: Any, files: Any, data: Any) -> dict[str, Any]:
    """Build the httpx body kwargs: multipart/form when files or data are given."""
    if files is not None or data is not None:
        return {"files": files, "data": data}
    return {"json": json_body}


def _buffer_files(files: Any) -> Any:
    """Read file-like payloads in ``files`` into ``bytes`` before the first attempt.

    httpx rewinds seekable files between attempts, but a non-seekable stream is
    read from its current position: a retry would upload an empty file and still
    look successful. Buffering once means every attempt sends identical bytes.
    """
    if isinstance(files, Mapping):
        return {name: _buffer_file_value(value) for name, value in files.items()}
    if isinstance(files, (list, tuple)):
        return [(name, _buffer_file_value(value)) for name, value in files]
    return files


def _buffer_file_value(value: Any) -> Any:
    """Rewrite one ``files`` entry so its payload is bytes rather than a stream."""
    if isinstance(value, tuple):
        # (filename, file[, content_type[, headers]]): buffer only the file part.
        if len(value) < 2 or not hasattr(value[1], "read"):
            return value
        content = _read_file_content(value[1])
        if content is None:
            return value
        return (value[0], content, *value[2:])
    if hasattr(value, "read"):
        content = _read_file_content(value)
        if content is None:
            return value
        # Mirror httpx, which derives the filename from the stream's name.
        filename = Path(str(getattr(value, "name", "upload"))).name
        return (filename, content)
    return value


def _read_file_content(fileobj: Any) -> bytes | None:
    """Full content of ``fileobj``, or ``None`` to leave the entry untouched."""
    if isinstance(fileobj, io.TextIOBase):
        # Text streams stay untouched so httpx keeps raising its usual TypeError.
        return None
    content = fileobj.read()
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return None


def _media_type(response: httpx.Response) -> str:
    raw = response.headers.get("content-type") or ""
    return raw.split(";")[0].strip().lower()


def _is_json_media_type(media_type: str) -> bool:
    return media_type.endswith(("/json", "+json"))


_BINARY_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-bzip2",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def _is_binary_media_type(media_type: str) -> bool:
    if media_type.startswith(("image/", "audio/", "video/", "font/", "multipart/")):
        return True
    return media_type in _BINARY_MEDIA_TYPES


def _success_payload(response: httpx.Response) -> Any:
    """Decode a successful (status < 300) body: ``None``, parsed JSON, or raw bytes."""
    if not response.content:
        return None
    media_type = _media_type(response)
    if _is_binary_media_type(media_type):
        return response.content
    if _is_json_media_type(media_type):
        # Declared JSON: an unparseable body keeps raising, as it always has.
        return response.json()
    if media_type:
        # Declared non-JSON (text/plain, text/csv, ...): raw bytes, never parsed.
        return response.content
    # No Content-Type header: sloppy endpoints - try JSON, else the raw bytes.
    try:
        return response.json()
    except ValueError:  # JSONDecodeError / UnicodeDecodeError are ValueError subclasses
        return response.content


def _response_error(response: httpx.Response, *, endpoint: str) -> HaloCLIError:
    snippet = _error_snippet(response)
    error = RuntimeError(f"HTTP {response.status_code}: {snippet}")
    # classify_error reads .response_body/.response off this RuntimeError.
    error.response_body = snippet  # type: ignore[attr-defined]
    error.response = response  # type: ignore[attr-defined]
    return classify_error(error, endpoint=endpoint)


def _error_snippet(response: httpx.Response, limit: int = 500) -> str:
    """Short, never-raising summary of an error body (JSON, text, binary or empty)."""
    if not response.content:
        return ""
    media_type = _media_type(response)
    if _is_binary_media_type(media_type):
        return f"<binary body: {len(response.content)} bytes, {media_type}>"
    try:
        return response.text[:limit]
    except Exception:  # pragma: no cover - httpx normally decodes with replacement
        return f"<unreadable body: {len(response.content)} bytes>"
