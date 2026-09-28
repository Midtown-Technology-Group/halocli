from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Protocol

from halocli.utils import normalize_halo_result

if TYPE_CHECKING:
    from halocli.resources import HaloResource


__all__ = [
    "WriteClient",
    "collect_warnings",
    "delete_resource",
    "execute_write",
    "preview_payload",
    "validate_write",
]


class WriteClient(Protocol):
    """The slice of `HaloClient` that writes need; tests can inject a fake."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any: ...


def validate_write(
    resource: HaloResource,
    payload: Mapping[str, Any],
    *,
    update: bool = False,
) -> list[str]:
    """Validate `payload` against the resource's write metadata.

    Pure function: no network access, no mutation. Returns a list of problem
    strings; `[]` means the payload is acceptable. Extra/unknown keys are never
    errors (Halo accepts many optional fields) — they are reported as warnings by
    `collect_warnings` instead. The one exception is a concrete `id` on a create
    payload: Halo's POST-with-id convention would update that record instead of
    creating one, so it is rejected here. `{"id": null}` / `{"id": ""}` count as
    absent.
    """
    if not isinstance(payload, Mapping):
        return [f"payload for {resource.name} must be a JSON object"]
    verb = "update" if update else "create"
    problems: list[str] = []
    endpoint = resource.update_endpoint if update else resource.create_endpoint
    if endpoint is None:
        problems.append(f"{resource.name} does not support {verb}")
    required = resource.required_update_fields if update else resource.required_create_fields
    for key in required:
        if _is_missing(payload, key):
            problems.append(f"missing required field '{key}' to {verb} {resource.name}")
    if not update and not _is_missing(payload, "id"):
        problems.append(
            "create payload must not include 'id'; Halo treats POST-with-id as an "
            "update — use the update command or remove 'id'"
        )
    return problems


def collect_warnings(resource: HaloResource, payload: Mapping[str, Any]) -> list[str]:
    """Report payload keys outside the resource's declared write shape.

    These are warnings, not errors: Halo accepts many optional fields that the
    registry does not track, so they are forwarded untouched.
    """
    if not isinstance(payload, Mapping):
        return []
    known = resource.effective_write_preview_fields
    warnings: list[str] = []
    for key in payload:
        if key not in known:
            warnings.append(
                f"field '{key}' is not part of the {resource.name} write shape; "
                "Halo will receive it as-is"
            )
    return warnings


def preview_payload(resource: HaloResource, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the COMPLETE payload that `execute_write` will transmit.

    The preview must never hide a field that apply will send. Declared
    `effective_write_preview_fields` keys come first (in their declared order) as
    an emphasis hint, followed by every remaining key in insertion order.
    """
    if not isinstance(payload, Mapping):
        return {}
    preview: dict[str, Any] = {}
    for key in resource.effective_write_preview_fields:
        if key in payload:
            preview[key] = payload[key]
    for key, value in payload.items():
        if key not in preview:
            preview[key] = value
    return preview


async def execute_write(
    client: WriteClient | None,
    resource: HaloResource,
    payload: Mapping[str, Any],
    *,
    update: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Create (or with `update=True`, update) a record on Halo.

    Dry run by default: with `apply=False` this performs ZERO network calls and
    returns a preview `{"ok", "apply", "resource", "method", "endpoint", "payload",
    "warnings"}` — `client` may be `None` on that path, so previews work without a
    configured profile. Validation problems are returned in `errors` before any network
    call, even when `apply=True`; `client` is required only when `apply=True`. With `apply=True` the payload is POSTed through the
    injected client and the normalised Halo response is returned as `result`.

    Halo's write endpoints (POST /Tickets, POST /Actions, ...) take an *array* of
    objects — POST creates when `id` is absent and updates when `id` is present — so
    the wire body is `[payload]`; the preview shows the object the caller supplied.
    """
    endpoint = resource.update_endpoint if update else resource.create_endpoint
    problems = validate_write(resource, payload, update=update)
    result: dict[str, Any] = {
        "ok": not problems,
        "apply": apply,
        "resource": resource.name,
        "method": "POST",
        "endpoint": endpoint,
        "payload": preview_payload(resource, payload),
        "warnings": collect_warnings(resource, payload),
    }
    if problems:
        result["errors"] = problems
        return result
    if not apply:
        return result
    if client is None:
        raise ValueError("apply=True requires a HaloClient")
    assert endpoint is not None  # validate_write reported no problems
    response = await client.request("POST", endpoint, json_body=[dict(payload)])
    result["result"] = normalize_halo_result(response)
    return result


async def delete_resource(
    client: WriteClient | None,
    resource: HaloResource,
    item_id: str | int,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Delete `{collection}/{item_id}`.

    Refuses when `resource.supports_delete` is False and, like `execute_write`, is a
    dry run unless `apply=True` (`client` may be `None` for dry runs).
    """
    result: dict[str, Any] = {
        "ok": False,
        "apply": apply,
        "resource": resource.name,
        "method": "DELETE",
        "endpoint": None,
        "warnings": [],
    }
    if not resource.supports_delete:
        result["errors"] = [f"{resource.name} does not support delete"]
        return result
    if item_id is None or (isinstance(item_id, str) and not item_id.strip()):
        result["errors"] = ["a non-empty item id is required to delete"]
        return result
    endpoint = f"{resource.endpoint}/{item_id}"
    result["endpoint"] = endpoint
    if not apply:
        result["ok"] = True
        return result
    if client is None:
        raise ValueError("apply=True requires a HaloClient")
    response = await client.request("DELETE", endpoint)
    result["ok"] = True
    result["result"] = normalize_halo_result(response)
    return result


def _is_missing(payload: Mapping[str, Any], key: str) -> bool:
    if key not in payload:
        return True
    value = payload[key]
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()
