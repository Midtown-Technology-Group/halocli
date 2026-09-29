from __future__ import annotations

import re
from typing import Any, Literal


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
    if category == "unknown" and _is_timeout(exc):
        # Timeouts (httpx/httpcore/asyncio) carry no message, so text matching
        # on str(exc) always misses them and the caller sees an empty "unknown".
        category = "timeout"
    message = f"HaloPSA {category} error"
    if status_code is not None:
        message += f" ({status_code})"
    if endpoint:
        message += f" on {endpoint}"
    detail = body or str(exc) or type(exc).__name__
    if detail:
        message += f": {detail[:300]}"
    return HaloCLIError(
        message,
        category=category,
        status_code=status_code,
        response_body=body,
        retry_after=retry_after,
        endpoint=endpoint,
    )


def _is_timeout(exc: BaseException) -> bool:
    """Timeout exceptions frequently stringify to ``""`` (httpx.ReadTimeout()).

    Match on the class name as well as the built-in so a message-less timeout
    still classifies as ``timeout`` rather than ``unknown``.
    """
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def diagnose_permission_failure(error: HaloCLIError) -> str:
    if error.category != "permission":
        return ""
    endpoint = (error.endpoint or "").lower()
    if "/client" in endpoint:
        return (
            "Halo returned 403 for Client access. Check application scopes, the "
            "login-as API-only agent, the agent role, and Halo UI feature-access "
            "permissions for that API-only agent. Run `halocli auth whoami "
            "--check /Client` (substitute the endpoint you called) to see the "
            "granted scope and whether it is reachable."
        )
    return (
        "Halo returned 403. Check application scopes, login-as agent, agent role, "
        "and endpoint-specific feature-access permissions. Run `halocli auth "
        "whoami --check /Client` (substitute the endpoint you called) to see the "
        "granted scope and whether it is reachable."
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
    value = _safe_attr(exc, "status_code")
    if value is None:
        response = _safe_attr(exc, "response")
        value = _safe_attr(response, "status_code") if response is not None else None
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
        text = _as_text(_safe_attr(exc, attr))
        if text:
            return text
    response = _safe_attr(exc, "response")
    if response is not None:
        text = _as_text(_safe_attr(response, "text"))
        if text:
            return text
    return None


def _safe_attr(obj: object, name: str) -> Any:
    """Read an attribute without letting a raising property or unreadable body blow up."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _as_text(value: Any) -> str | None:
    """Coerce an error body (str, bytes or anything else) into a printable string."""
    if value is None or value == "":
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    try:
        text = str(value)
    except Exception:
        return None
    return text or None


def _retry_after(exc: BaseException) -> float | None:
    value = _safe_attr(exc, "retry_after")
    if value is None:
        response = _safe_attr(exc, "response")
        headers = _safe_attr(response, "headers") if response is not None else None
        get = getattr(headers, "get", None)
        value = get("Retry-After") if callable(get) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
