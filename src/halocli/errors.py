from __future__ import annotations

import re
from typing import Literal


ErrorCategory = Literal[
    "auth",
    "permission",
    "validation",
    "rate_limit",
    "not_found",
    "server",
    "timeout",
    "unknown",
]


class HaloCLIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = "unknown",
        status_code: int | None = None,
        response_body: str | None = None,
        retry_after: float | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after = retry_after
        self.endpoint = endpoint


def classify_error(exc: BaseException, *, endpoint: str | None = None) -> HaloCLIError:
    status_code = _status_code(exc)
    body = _body(exc)
    retry_after = _retry_after(exc)
    category = _category(status_code, str(exc))
    message = f"HaloPSA {category} error"
    if status_code is not None:
        message += f" ({status_code})"
    if endpoint:
        message += f" on {endpoint}"
    if body:
        message += f": {body[:300]}"
    return HaloCLIError(
        message,
        category=category,
        status_code=status_code,
        response_body=body,
        retry_after=retry_after,
        endpoint=endpoint,
    )


def diagnose_permission_failure(error: HaloCLIError) -> str:
    if error.category != "permission":
        return ""
    endpoint = (error.endpoint or "").lower()
    if "/client" in endpoint:
        return (
            "Halo returned 403 for Client access. Check application scopes, the "
            "login-as API-only agent, the agent role, and Halo UI feature-access "
            "permissions for that API-only agent."
        )
    return (
        "Halo returned 403. Check application scopes, login-as agent, agent role, "
        "and endpoint-specific feature-access permissions."
    )


def _category(status_code: int | None, text: str) -> ErrorCategory:
    if status_code == 400:
        return "validation"
    if status_code == 401:
        return "auth"
    if status_code == 403:
        return "permission"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limit"
    if status_code is not None and status_code >= 500:
        return "server"
    if "timeout" in text.lower():
        return "timeout"
    return "unknown"


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None and getattr(exc, "response", None) is not None:
        value = getattr(exc.response, "status_code", None)
    if value is None:
        match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), flags=re.IGNORECASE)
        if match:
            value = match.group(1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _body(exc: BaseException) -> str | None:
    for attr in ("response_body", "text"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            return str(value)
    if getattr(exc, "response", None) is not None:
        value = getattr(exc.response, "text", None)
        if value not in (None, ""):
            return str(value)
    return None


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None and getattr(exc, "response", None) is not None:
        value = (getattr(exc.response, "headers", {}) or {}).get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
