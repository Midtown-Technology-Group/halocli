from __future__ import annotations

from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, Field


class DiscoveryStatus(StrEnum):
    INTERACTIVE_SUPPORTED = "interactive_supported"
    CLIENT_CREDENTIALS_ONLY = "client_credentials_only"
    UNKNOWN = "unknown"
    ERROR = "error"


class DiscoveryProbe(BaseModel):
    endpoint: str
    status_code: int | None = None
    ok: bool = False
    signal: str | None = None
    error: str | None = None


class DiscoveryResult(BaseModel):
    tenant_url: str
    status: DiscoveryStatus
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    grant_types_supported: list[str] = Field(default_factory=list)
    checked_endpoints: list[DiscoveryProbe] = Field(default_factory=list)
    recommendation: str


async def discover_auth(
    tenant_url: str,
    *,
    http: httpx.AsyncClient | None = None,
) -> DiscoveryResult:
    base_url = _normalize_tenant_url(tenant_url)
    checked: list[DiscoveryProbe] = []
    own_client = http is None
    client = http or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        metadata = await _probe_metadata(client, base_url, checked)
        if metadata is not None:
            authorization_endpoint = _str_or_none(metadata.get("authorization_endpoint"))
            token_endpoint = _str_or_none(metadata.get("token_endpoint"))
            grants = _string_list(metadata.get("grant_types_supported"))
            if authorization_endpoint and token_endpoint and _supports_interactive(grants):
                return DiscoveryResult(
                    tenant_url=base_url,
                    status=DiscoveryStatus.INTERACTIVE_SUPPORTED,
                    authorization_endpoint=authorization_endpoint,
                    token_endpoint=token_endpoint,
                    grant_types_supported=grants,
                    checked_endpoints=checked,
                    recommendation=(
                        "This Halo instance appears to expose authorization-code style "
                        "OAuth endpoints. Interactive login can be tested for this profile."
                    ),
                )

        token_signal = await _probe_token_endpoint(client, base_url, checked)
        if token_signal:
            return DiscoveryResult(
                tenant_url=base_url,
                status=DiscoveryStatus.CLIENT_CREDENTIALS_ONLY,
                token_endpoint=f"{base_url}/auth/token",
                checked_endpoints=checked,
                recommendation=(
                    "Discovery found a Halo token endpoint but no confirmed browser "
                    "authorization endpoint. Use client-credentials for automation."
                ),
            )

        return DiscoveryResult(
            tenant_url=base_url,
            status=DiscoveryStatus.UNKNOWN,
            checked_endpoints=checked,
            recommendation=(
                "Discovery did not find enough auth metadata to safely enable interactive "
                "login. Keep using client-credentials, or inspect Halo/Entra configuration."
            ),
        )
    except httpx.HTTPError as exc:
        checked.append(DiscoveryProbe(endpoint=base_url, error=str(exc), signal="http_error"))
        return DiscoveryResult(
            tenant_url=base_url,
            status=DiscoveryStatus.ERROR,
            checked_endpoints=checked,
            recommendation=f"Discovery failed before auth support could be classified: {exc}",
        )
    finally:
        if own_client:
            await client.aclose()


async def _probe_metadata(
    client: httpx.AsyncClient,
    base_url: str,
    checked: list[DiscoveryProbe],
) -> dict[str, Any] | None:
    for path in (
        "/.well-known/openid-configuration",
        "/auth/.well-known/openid-configuration",
    ):
        url = f"{base_url}{path}"
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            checked.append(DiscoveryProbe(endpoint=url, error=str(exc), signal="metadata_error"))
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                checked.append(
                    DiscoveryProbe(
                        endpoint=url,
                        status_code=response.status_code,
                        signal="metadata_not_json",
                    )
                )
                continue
            checked.append(
                DiscoveryProbe(
                    endpoint=url,
                    status_code=response.status_code,
                    ok=True,
                    signal="openid_metadata",
                )
            )
            return payload if isinstance(payload, dict) else None
        checked.append(
            DiscoveryProbe(
                endpoint=url,
                status_code=response.status_code,
                signal="metadata_absent",
            )
        )
    return None


async def _probe_token_endpoint(
    client: httpx.AsyncClient,
    base_url: str,
    checked: list[DiscoveryProbe],
) -> bool:
    url = f"{base_url}/auth/token"
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        checked.append(DiscoveryProbe(endpoint=url, error=str(exc), signal="token_error"))
        return False
    signal = "token_endpoint_signal" if response.status_code in {400, 401, 405} else "token_probe"
    checked.append(
        DiscoveryProbe(
            endpoint=url,
            status_code=response.status_code,
            ok=response.status_code in {400, 401, 405},
            signal=signal,
        )
    )
    return response.status_code in {400, 401, 405}


def _normalize_tenant_url(tenant_url: str) -> str:
    base = tenant_url.rstrip("/")
    if base.lower().endswith("/api"):
        return base[:-4]
    return base


def _supports_interactive(grants: list[str]) -> bool:
    return not grants or "authorization_code" in grants or "device_code" in grants


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
