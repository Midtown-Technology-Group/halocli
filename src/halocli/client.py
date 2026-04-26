from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from halocli.config import HaloProfile
from halocli.errors import HaloCLIError, classify_error
from halocli.models import TokenPayload
from halocli.resources import get_resource
from halocli.token_cache import KeyringTokenCache, TokenCache


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
        self.profile_name = profile_name
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
    ) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.profile.timeout)
        token = await self._access_token()
        url = self._url(path)
        headers = {"Authorization": f"Bearer {token}"}

        last_response: httpx.Response | None = None
        for attempt in range(self.profile.max_retries + 1):
            response = await self._http.request(
                method.upper(),
                url,
                params=params,
                json=json_body,
                headers=headers,
            )
            last_response = response
            if response.status_code < 300:
                return response.json() if response.content else None
            if response.status_code == 401 and attempt == 0:
                self._token = None
                headers["Authorization"] = f"Bearer {await self._access_token()}"
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.profile.max_retries:
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

    async def raw(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        return await self.request(method, path, params=params, json_body=body)

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
            self.profile.auth_token_url,
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
        response = await self._http.post(self.profile.token_endpoint or self.profile.auth_token_url, data=data)
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
        return f"{self.profile.api_base_url}{clean_path}"

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

def _response_error(response: httpx.Response, *, endpoint: str) -> HaloCLIError:
    error = RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    error.response = response  # type: ignore[attr-defined]
    return classify_error(error, endpoint=endpoint)
