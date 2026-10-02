"""Code-mode MCP server for HaloCLI.

Instead of exposing one MCP tool per HaloPSA operation (32 resources x verbs = massive
context bloat), this server exposes a tiny three-tool catalog:

* ``halo_search``   - discover resources/endpoints (registry + vendored OpenAPI spec)
* ``halo_execute``  - generic, schema-bounded executor with explicit write guardrails
* ``halo_resources`` - dump the full resource catalog when search misses

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout, stdlib only. Nothing but
protocol messages is ever written to stdout; all diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Any, Awaitable, Callable, TextIO

from halocli.client import HaloClient
from halocli.config import load_profile
from halocli.errors import HaloCLIError
from halocli.resources import RESOURCES, HaloResource, ResourceOperation

JSONRPC_VERSION = "2.0"
LATEST_PROTOCOL_VERSION = "2025-06-18"
# MCP protocol versions this server can speak. An unrecognized client request is
# answered with LATEST_PROTOCOL_VERSION so the client can decide to disconnect.
SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset(
    {
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
    }
)
SERVER_NAME = "halocli"

MAX_RESPONSE_CHARS = 40_000
# The resource catalog is metadata for agent discovery, not tenant data: it
# gets its own, much larger allowance so the dump stays complete as the
# registry grows (180 resources serialized to ~51k chars - past the data cap).
MAX_CATALOG_CHARS = 262_144
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

ToolHandler = Callable[[dict[str, Any]], Awaitable[tuple[dict[str, Any], bool]]]


def _load_schema_search() -> Callable[..., Any] | None:
    """Import the vendored OpenAPI operation search helper if it has landed yet."""
    try:
        from halocli.schema import search_operations
    except ImportError:
        return None
    return search_operations


def _detect_version() -> str:
    try:
        return _package_version("halocli")
    except PackageNotFoundError:
        return "0.0.0"


schema_search_operations: Callable[..., Any] | None = _load_schema_search()
SERVER_VERSION = _detect_version()

_SERVER_INSTRUCTIONS = (
    "Code-mode HaloPSA access with a tiny tool catalog. Workflow: (1) halo_search or "
    "halo_resources to discover endpoints, (2) halo_execute to run one REST call. "
    "Non-GET methods require apply: true; responses over 40k chars come back truncated."
)

_SEARCH_DESCRIPTION = (
    "Step 1 of the discover-then-execute workflow: find a HaloPSA resource or API "
    "operation, then run it with halo_execute. This server intentionally exposes only "
    "three tools instead of one tool per Halo operation, so discovery happens here.\n"
    "\n"
    "Searches two sources and returns ranked JSON results: (1) the HaloCLI resource "
    "registry - all first-class resources with their names, aliases, endpoint paths, "
    "capabilities, table columns and declared nested operations; (2) the vendored "
    "HaloPSA OpenAPI specification, "
    "when that module is available. Ranking prefers exact name matches, then aliases, "
    "then endpoint, column and nested-operation (name/path/summary) matches.\n"
    "\n"
    "Arguments:\n"
    "  query (required): free-text terms, case-insensitive. Examples: 'tickets', "
    "'quote', '/Invoice', 'client contacts', 'status_name'.\n"
    "  limit (optional): maximum results to return; default 10, capped at 50.\n"
    "\n"
    "Result shape: {ok, query, count, results: [{name, endpoint, aliases, "
    "capabilities, table_fields, verbs, operations, score, source}], hint}. Each "
    "result's endpoint is exactly what you pass as 'path' to halo_execute: it is "
    "relative to the API root, so never add '/api' or the tenant host. Results whose "
    "nested operations matched the query also carry 'matched_operations' (the "
    "matching operation names). Resources may declare nested operations - entries of "
    "{name, method, path, write} such as invoices 'pdf' -> POST /Invoice/PDF/{id} - "
    "and those are executable via halo_execute too: pass the operation's path as "
    "'path' (substituting any {id} placeholder) and its method as 'method'; write "
    "operations still require apply: true. If search finds nothing, call "
    "halo_resources to enumerate the full catalog."
)

_EXECUTE_DESCRIPTION = (
    "Step 2 of the discover-then-execute workflow: run one raw HaloPSA REST call "
    "through a generic, schema-bounded executor. Discover the endpoint first with "
    "halo_search (or halo_resources), then pass it here.\n"
    "\n"
    "Arguments:\n"
    "  method (required): GET, POST, PUT, PATCH or DELETE, case-insensitive "
    "(enum-validated).\n"
    "  path (required): REST path relative to the API root, e.g. '/Tickets', "
    "'/Tickets/123', '/Agent/me'. Do not include the tenant host or the '/api' "
    "prefix.\n"
    "  params (optional): query parameters as an object of string values, e.g. "
    "{'take': '50', 'search': 'urgent'}.\n"
    "  body (optional): JSON request body object; needed by most POST/PUT/PATCH "
    "calls.\n"
    "  profile (optional): HaloCLI profile name, default 'default'.\n"
    "  apply (optional): boolean, default false. Explicit write confirmation.\n"
    "\n"
    "Guardrails, enforced server-side:\n"
    "  - GET requests run immediately; apply is not needed.\n"
    "  - POST/PUT/PATCH/DELETE require apply: true. Without it you receive a "
    "structured refusal ({ok: false, refused: true, category: 'guardrail', error, "
    "hint}) and NO request is sent (no client is even created). Re-issue the "
    "identical call with apply: true to confirm the write.\n"
    "  - Responses are bounded to ~40,000 characters: larger payloads come back as "
    "{truncated: true, total_chars, preview} where preview is the first 40,000 chars, "
    "so huge collections cannot flood your context. Narrow the query with params "
    "(filters/paging) and re-run instead of fetching everything.\n"
    "\n"
    "Results: success is {ok: true, method, path, params, data}. Failures are returned "
    "as structured data with isError: true (this server never crashes on API errors): "
    "{ok: false, category, status_code, error} where category is one of auth, "
    "permission, validation, rate_limit, not_found, server, timeout, unknown."
)

_RESOURCES_DESCRIPTION = (
    f"Dump the complete HaloCLI resource catalog: all {len(RESOURCES)} first-class "
    "HaloPSA resources with name, endpoint, aliases, table_fields (columns that make "
    "good table output), capabilities, verbs and any declared nested operations. "
    "Takes no arguments; call it when halo_search misses or when you want to "
    "enumerate everything this server knows.\n"
    "\n"
    "Result shape: {ok, count, resources: [{name, endpoint, aliases, table_fields, "
    "capabilities, verbs, operations}], hint}. Pass an entry's endpoint as 'path' to "
    "halo_execute (no '/api' prefix, no tenant host). 'operations' is a list of "
    "{name, method, path, write} entries (empty when the resource declares none); "
    "each is executable via halo_execute by passing its path as 'path' (substituting "
    "any {id} placeholder) and its method as 'method', with writes still requiring "
    "apply: true."
)

# MCP tool annotations. Hosts read these: Codex's `writes` approval mode prompts
# only for tools "that aren't marked read-only", so halo_search/halo_resources
# must advertise readOnlyHint or every discovery call prompts alongside writes.
_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "halo_search",
        "description": _SEARCH_DESCRIPTION,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search terms, e.g. 'tickets' or '/Invoice'.",
                },
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_SEARCH_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_SEARCH_LIMIT,
                    "description": "Maximum results to return (default 10, max 50).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "halo_execute",
        "description": _EXECUTE_DESCRIPTION,
        # Not read-only: with apply:true this issues POST/PUT/PATCH/DELETE, and a
        # DELETE can destroy records (destructiveHint defaults to true, stated for
        # clarity). openWorld because results come from the live Halo tenant.
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": list(ALLOWED_METHODS),
                    "description": "HTTP method; case-insensitive, enum-validated.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "REST path relative to the API root, e.g. '/Tickets'. "
                        "No tenant host, no '/api' prefix."
                    ),
                },
                "params": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Query parameters as string values, e.g. {'take': '50'}.",
                },
                "body": {
                    "type": "object",
                    "description": "JSON request body for POST/PUT/PATCH calls.",
                },
                "profile": {
                    "type": "string",
                    "default": "default",
                    "description": "HaloCLI profile name (default: 'default').",
                },
                "apply": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Write confirmation. Must be true for POST/PUT/PATCH/DELETE; "
                        "GET runs without it."
                    ),
                },
            },
            "required": ["method", "path"],
        },
    },
    {
        "name": "halo_resources",
        "description": _RESOURCES_DESCRIPTION,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _capability_summary(resource: HaloResource) -> str:
    read = "list/get" if resource.supports_get else "list"
    # Declared operations are first-class commands too. Without this, a resource
    # whose only write is an operation (reports clone) or an otherwise
    # read-only resource carrying POST operations (invoices pdf/void) would be
    # advertised as "writes only via raw", telling an agent a real subcommand
    # does not exist.
    op_writes = sorted({op.name for op in resource.operations if op.write})
    if resource.supports_write:
        write = []
        if resource.supports_create:
            write.append("create")
        if resource.supports_update:
            write.append("update")
        if resource.supports_delete:
            write.append("delete")
        write.extend(op_writes)
        return f"{read} via GET; {'/'.join(write)} via POST/PUT/PATCH/DELETE (apply:true)"
    if op_writes:
        return f"{read} via GET; {'/'.join(op_writes)} via POST (apply:true)"
    return f"{read} via GET; writes only via raw with apply:true"


def _verbs(resource: HaloResource) -> list[str]:
    # GET is always available: every resource exposes `list` (the collection
    # route). supports_get only gates the *item* route, which _capability_summary
    # reports as "list/get" vs "list" - an agent reading verbs must still be
    # allowed to GET the collection (e.g. expenses: GET /Expense works).
    verbs = ["GET"]
    if resource.supports_write:
        verbs.append("POST")
    if resource.supports_delete:
        verbs.append("DELETE")
    # Declared operations may use methods the write metadata does not (reports
    # clone is a POST on a resource with no create/update endpoints).
    for op in resource.operations:
        method = op.method.upper()
        if method not in verbs:
            verbs.append(method)
    return verbs


def _operation_entry(operation: ResourceOperation) -> dict[str, Any]:
    return {
        "name": operation.name,
        "method": operation.method,
        "path": operation.path,
        "write": operation.write,
    }


def _resource_entry(resource: HaloResource) -> dict[str, Any]:
    return {
        "name": resource.name,
        "endpoint": resource.endpoint,
        "aliases": list(resource.aliases),
        "table_fields": list(resource.table_fields),
        "capabilities": _capability_summary(resource),
        "verbs": _verbs(resource),
        # Declared nested operations (e.g. invoices 'pdf' -> POST /Invoice/PDF/{id});
        # empty for resources that have none.
        "operations": [_operation_entry(op) for op in resource.operations],
    }


def _op_specific_segments(resource: HaloResource, operation: ResourceOperation) -> list[str]:
    """Operation path segments after stripping the resource endpoint's own prefix.

    ``/Invoice/{id}/void`` on the ``invoices`` resource (endpoint ``/Invoice``) yields
    ``['{id}', 'void']`` so operation scoring reflects op-specific routes instead of
    re-scoring the endpoint the resource already matched on.
    """
    segments = [segment for segment in operation.path.lower().split("/") if segment]
    base = [segment for segment in resource.endpoint.lower().split("/") if segment]
    if segments[: len(base)] == base:
        return segments[len(base) :]
    return segments


def _score_operations(resource: HaloResource, terms: list[str]) -> tuple[int, list[str]]:
    """Score declared nested operations against the query terms.

    Mirrors the resource-level style: exact match > prefix > substring, with the
    same trailing-'s' stem handling; path segments and summaries score lower. Returns
    the points earned plus the names of the operations that matched anything (so
    callers can surface ``matched_operations`` without changing entry shapes for
    queries that only hit resource-level terms).
    """
    points_total = 0
    matched: list[str] = []
    for operation in resource.operations:
        op_name = operation.name.lower()
        segments = _op_specific_segments(resource, operation)
        summary = operation.summary.lower()
        points = 0
        for term in terms:
            stem = term[:-1] if term.endswith("s") and len(term) > 3 else term
            if op_name == term or op_name == stem:
                points += 70
            elif op_name.startswith(term) or (stem != term and op_name.startswith(stem)):
                points += 50
            elif term in op_name or stem in op_name:
                points += 40
            elif any(term == segment or stem == segment for segment in segments):
                points += 35
            elif any(term in segment or stem in segment for segment in segments):
                points += 25
            elif term in summary or stem in summary:
                points += 10
        if points:
            points_total += points
            matched.append(operation.name)
    return points_total, matched


# --------------------------------------------------------------------------------------
# Discovery: resource registry + vendored OpenAPI operation search
# --------------------------------------------------------------------------------------
def _search_registry(query: str, limit: int) -> list[dict[str, Any]]:
    terms = [term for term in re.split(r"\s+", query.strip().lower()) if term]
    if not terms:
        return []
    scored: list[tuple[int, str, HaloResource, list[str]]] = []
    for resource in RESOURCES:
        name = resource.name.lower()
        endpoint = resource.endpoint.lower()
        aliases = [alias.lower() for alias in resource.aliases]
        fields = [field.lower() for field in resource.table_fields]
        summary = _capability_summary(resource).lower()
        score = 0
        for term in terms:
            stem = term[:-1] if term.endswith("s") and len(term) > 3 else term
            if name == term:
                score += 100
            elif term in name or stem in name:
                score += 60
            if term in aliases:
                score += 80
            elif any(term in alias or alias in term for alias in aliases):
                score += 40
            if term in endpoint or stem in endpoint:
                score += 30
            if any(term in field for field in fields):
                score += 20
            if term in summary:
                score += 10
        op_points, matched_ops = _score_operations(resource, terms)
        score += op_points
        if score > 0:
            scored.append((score, resource.name, resource, matched_ops))
    scored.sort(key=lambda item: (-item[0], item[1]))
    results: list[dict[str, Any]] = []
    for score, _, resource, matched_ops in scored[:limit]:
        entry = _resource_entry(resource)
        entry["score"] = score
        entry["source"] = "registry"
        if matched_ops:
            # Present only when operation terms matched, so entry shapes for
            # resource-level-only queries stay exactly as before.
            entry["matched_operations"] = matched_ops
        results.append(entry)
    return results


def _normalize_schema_results(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("results", "operations", "matches", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            entry = dict(item)
            entry.setdefault("source", "openapi")
            items.append(entry)
        elif isinstance(item, str):
            items.append({"operation": item, "source": "openapi"})
    return items


def _search_schema(query: str, limit: int) -> list[dict[str, Any]]:
    if schema_search_operations is None or limit <= 0:
        return []
    try:
        try:
            raw = schema_search_operations(query, limit=limit)
        except TypeError:
            raw = schema_search_operations(query)
    except Exception as exc:  # schema module is best-effort discovery
        print(f"halocli-mcp: schema search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []
    return _normalize_schema_results(raw)[:limit]


# --------------------------------------------------------------------------------------
# Tool handlers
# --------------------------------------------------------------------------------------
def _validation_failure(message: str) -> tuple[dict[str, Any], bool]:
    return {"ok": False, "category": "validation", "status_code": None, "error": message}, True


def _refuse_write(method: str, path: str) -> tuple[dict[str, Any], bool]:
    payload = {
        "ok": False,
        "refused": True,
        "category": "guardrail",
        "method": method,
        "path": path,
        "error": (
            f"{method} {path} can modify HaloPSA data and was NOT executed: non-GET "
            "requests require apply: true."
        ),
        "hint": (
            "Re-issue this exact halo_execute call with apply: true to confirm. "
            "GET requests never require apply."
        ),
    }
    return payload, True


def _halo_failure(exc: HaloCLIError) -> tuple[dict[str, Any], bool]:
    payload: dict[str, Any] = {
        "ok": False,
        "category": exc.category,
        "status_code": exc.status_code,
        "error": str(exc),
    }
    return payload, True


def _failure(exc: BaseException) -> tuple[dict[str, Any], bool]:
    if isinstance(exc, HaloCLIError):
        return _halo_failure(exc)
    category = "validation" if isinstance(exc, ValueError) else "unknown"
    payload = {
        "ok": False,
        "category": category,
        "status_code": None,
        "error": f"{type(exc).__name__}: {exc}",
    }
    return payload, True


def _coerce_limit(raw: Any) -> int:
    if raw is None:
        limit = DEFAULT_SEARCH_LIMIT
    elif isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("'limit' must be an integer between 1 and 50")
    elif isinstance(raw, (int, float)):
        limit = int(raw)
    else:
        try:
            limit = int(raw.strip())
        except ValueError as exc:
            raise ValueError("'limit' must be an integer between 1 and 50") from exc
    return max(1, min(limit, MAX_SEARCH_LIMIT))


def _normalize_params(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("'params' must be an object of string values, e.g. {'take': '50'}")
    params: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("'params' keys must be strings")
        if isinstance(value, str):
            params[key] = value
        elif isinstance(value, bool):
            params[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            params[key] = str(value)
        else:
            raise ValueError(
                f"'params[{key}]' must be a string value; nested objects and arrays "
                "are not allowed"
            )
    return params


def search_catalog(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Rank registry + vendored-OpenAPI matches for `query`.

    Public entry point shared by the ``halo_search`` tool and ``halocli search``.
    ``limit`` is clamped to [1, MAX_SEARCH_LIMIT]; registry matches rank first.
    """
    bounded = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    results = _search_registry(query, bounded)
    results.extend(_search_schema(query, bounded - len(results)))
    return results


async def _tool_halo_search(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _validation_failure("'query' is required and must be a non-empty string")
    try:
        limit = _coerce_limit(args.get("limit"))
    except ValueError as exc:
        return _validation_failure(str(exc))
    results = search_catalog(query, limit)
    payload = {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
        "hint": (
            "Pass a result's endpoint as `path` to halo_execute (GET needs no apply; "
            "writes need apply: true). Declared operations work the same way: pass the "
            "operation's path as `path` and its method as `method` (writes still need "
            "apply: true). Call halo_resources if search missed."
        ),
    }
    return payload, False


async def _tool_halo_resources(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    entries = [_resource_entry(resource) for resource in RESOURCES]
    payload = {
        "ok": True,
        "count": len(entries),
        "resources": entries,
        "hint": (
            "Pass an entry's endpoint as `path` to halo_execute, or a declared "
            "operation's path as `path` plus its `method`; use halo_search for "
            "keyword lookup across names, aliases, endpoints, operations and OpenAPI."
        ),
    }
    return payload, False


def _client_for(profile_name: str) -> Any:
    """Build the HaloClient used by halo_execute. Tests monkeypatch this factory."""
    profile = load_profile(profile_name)
    return HaloClient(profile, profile_name=profile_name)


async def _close_client(client: Any) -> None:
    aexit = getattr(client, "__aexit__", None)
    if callable(aexit):
        await aexit(None, None, None)


async def _call_halo(
    profile_name: str,
    method: str,
    path: str,
    params: dict[str, str] | None,
    body: dict[str, Any] | None,
) -> Any:
    client = _client_for(profile_name)
    try:
        return await client.raw(method, path, params=params, body=body)
    finally:
        await _close_client(client)


async def _tool_halo_execute(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    raw_method = args.get("method")
    if not isinstance(raw_method, str) or not raw_method.strip():
        return _validation_failure(
            "'method' is required and must be one of: " + ", ".join(ALLOWED_METHODS)
        )
    method = raw_method.strip().upper()
    if method not in ALLOWED_METHODS:
        return _validation_failure(
            f"Method '{method}' is not allowed; use one of: " + ", ".join(ALLOWED_METHODS)
        )
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return _validation_failure("'path' is required, e.g. '/Tickets' or '/Tickets/123'")
    path = path.strip()
    # Guardrail: refuse before any client is built, so no network work happens.
    if method in WRITE_METHODS and args.get("apply", False) is not True:
        return _refuse_write(method, path)
    profile = args.get("profile", "default")
    if not isinstance(profile, str) or not profile.strip():
        return _validation_failure("'profile' must be a non-empty string")
    try:
        params = _normalize_params(args.get("params"))
    except ValueError as exc:
        return _validation_failure(str(exc))
    body = args.get("body")
    if body is not None and not isinstance(body, dict):
        return _validation_failure("'body' must be a JSON object")
    try:
        data = await _call_halo(profile.strip(), method, path, params, body)
    except HaloCLIError as exc:
        return _halo_failure(exc)
    except Exception as exc:
        return _failure(exc)
    payload = {"ok": True, "method": method, "path": path, "params": params, "data": data}
    return payload, False


_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "halo_search": _tool_halo_search,
    "halo_execute": _tool_halo_execute,
    "halo_resources": _tool_halo_resources,
}


# --------------------------------------------------------------------------------------
# MCP result shaping
# --------------------------------------------------------------------------------------
def _serialize_bounded(payload: Any, *, max_chars: int = MAX_RESPONSE_CHARS) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    wrapper = {
        "truncated": True,
        "total_chars": len(text),
        "preview": text[:max_chars],
    }
    return json.dumps(wrapper, ensure_ascii=False)


def _tool_result(
    payload: dict[str, Any], *, is_error: bool, max_chars: int = MAX_RESPONSE_CHARS
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": _serialize_bounded(payload, max_chars=max_chars)}],
    }
    if is_error:
        result["isError"] = True
    return result


# Per-tool response ceilings; anything not listed uses MAX_RESPONSE_CHARS.
_TOOL_CHAR_CAPS: dict[str, int] = {"halo_resources": MAX_CATALOG_CHARS}


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        payload, is_error = asyncio.run(_TOOL_HANDLERS[name](arguments))
    except Exception as exc:
        print(f"halocli-mcp: tool {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        payload, is_error = _failure(exc)
    return _tool_result(
        payload, is_error=is_error, max_chars=_TOOL_CHAR_CAPS.get(name, MAX_RESPONSE_CHARS)
    )


def _initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        protocol = requested
    else:
        protocol = LATEST_PROTOCOL_VERSION
    return {
        "protocolVersion": protocol,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": _SERVER_INSTRUCTIONS,
    }


# --------------------------------------------------------------------------------------
# JSON-RPC 2.0 message handling
# --------------------------------------------------------------------------------------
def _error_response(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": {"code": code, "message": message}}


def _result_response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def _handle_tools_call(msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _error_response(msg_id, INVALID_PARAMS, "tools/call requires a string 'name'")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error_response(msg_id, INVALID_PARAMS, "'arguments' must be an object")
    if name not in _TOOL_HANDLERS:
        return _error_response(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
    return _result_response(msg_id, _call_tool(name, arguments))


def _handle_message(message: dict[str, Any], is_notification: bool) -> dict[str, Any] | None:
    method = message["method"]
    msg_id = message.get("id")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_notification:
            return None
        return _error_response(msg_id, INVALID_PARAMS, "'params' must be an object")

    if is_notification:
        # Notifications (e.g. notifications/initialized) are acknowledged silently:
        # no side effects, no response.
        return None

    if method == "initialize":
        return _result_response(msg_id, _initialize_result(params))
    if method == "ping":
        return _result_response(msg_id, {})
    if method == "tools/list":
        return _result_response(msg_id, {"tools": _TOOL_DEFINITIONS})
    if method == "tools/call":
        return _handle_tools_call(msg_id, params)
    return _error_response(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def _handle_line(raw: str) -> str | None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        print("halocli-mcp: parse error on inbound JSON-RPC line", file=sys.stderr)
        return json.dumps(_error_response(None, PARSE_ERROR, "Parse error: invalid JSON"))
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        msg_id = message.get("id") if isinstance(message, dict) and "id" in message else None
        return json.dumps(_error_response(msg_id, INVALID_REQUEST, "Invalid Request"))
    response = _handle_message(message, is_notification="id" not in message)
    if response is None:
        return None
    return json.dumps(response, ensure_ascii=False)


def main(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    """Serve newline-delimited JSON-RPC 2.0 on stdin/stdout until EOF."""
    stdin = input_stream if input_stream is not None else sys.stdin
    stdout = output_stream if output_stream is not None else sys.stdout
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        response = _handle_line(stripped)
        if response is None:
            continue
        stdout.write(response + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
