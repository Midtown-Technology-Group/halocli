"""Code-mode MCP server for HaloCLI.

Instead of exposing one MCP tool per HaloPSA operation (32 resources x verbs = massive
context bloat), this server exposes a tiny five-tool catalog:

* ``halo_search``   - discover resources/endpoints (registry + vendored OpenAPI spec)
* ``halo_execute``  - generic, schema-bounded executor with explicit write guardrails
* ``halo_batch``    - bounded parallel GET fan-out in one call (read-only)
* ``halo_sql``      - SELECT-only query against the local offline mirror (never Halo)
* ``halo_resources`` - dump the full resource catalog when search misses

``halo_batch`` and ``halo_sql`` exist for codemode-style scripts: they collapse N
round trips (or a fetch-then-filter loop) into one call whose result the script can
filter before the model sees it. Writes stay single-call via ``halo_execute`` so the
apply guardrail is never batch-amplified.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout, stdlib only. Nothing but
protocol messages is ever written to stdout; all diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Awaitable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any, Callable, TextIO

from halocli.client import HaloClient
from halocli.config import load_profile
from halocli.errors import HaloCLIError
from halocli.mirror import DEFAULT_SYNC_LIMIT  # noqa: F401  (documents the mirror default)
from halocli.mirror import MirrorMissingError, default_mirror_path, open_existing
from halocli.resources import RESOURCES, HaloResource, ResourceOperation
from halocli.sqlguard import SQLGuardError, validate_select_only

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
# Batch fan-out: a script should collapse many small reads, not download a tenant.
DEFAULT_BATCH_CONCURRENCY = 5
MAX_BATCH_CONCURRENCY = 10
MAX_BATCH_CALLS = 25
MAX_BATCH_ITEM_CHARS = 6_000
# Local mirror SQL: bounded like `halocli sql`, which defaults to 500 rows.
DEFAULT_SQL_LIMIT = 500
MAX_SQL_LIMIT = 5_000
ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

ToolHandler = Callable[
    [dict[str, Any]], Awaitable[tuple[dict[str, Any], bool]] | tuple[dict[str, Any], bool]
]


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


@dataclass(frozen=True)
class ServerOptions:
    """Process-wide defaults set by ``halocli serve`` (per-call args still win)."""

    profile: str = "default"
    db_path: str | None = None


_OPTIONS = ServerOptions()

_SERVER_INSTRUCTIONS = (
    "Code-mode HaloPSA access with a tiny tool catalog. Workflow: (1) halo_search or "
    "halo_resources to discover endpoints, (2) halo_execute to run one REST call, or "
    "halo_batch to run up to 25 GETs in one call. halo_sql answers cross-resource "
    "questions from the local offline mirror (never the tenant). Non-GET methods "
    "require apply: true; responses over 40k chars come back truncated."
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

_SQL_DESCRIPTION = (
    "Query the local offline mirror of HaloPSA data with one read-only SELECT. This "
    "never reaches the tenant: it reads the SQLite file built by `halocli sync`, so "
    "cross-resource questions (status x priority x agent, aging buckets, joins "
    "between tickets and clients) answer instantly instead of paging the API.\n"
    "\n"
    "Arguments:\n"
    "  query (required): a single SELECT statement. The mirror exposes one view per "
    "synced resource (SELECT status_name, COUNT(*) AS n FROM tickets GROUP BY 1) plus "
    "the raw JSON in `mirror_rows`. The guard is conservative: one statement, SELECT "
    "only, no semicolons or comment tricks.\n"
    "  limit (optional): row ceiling, default 500, max 5000; truncation is reported, "
    "never implied.\n"
    "  db (optional): mirror database path; defaults to the standard mirror location "
    "(overridable process-wide with `halocli serve --db`).\n"
    "\n"
    "Result shape: {ok, db, query, columns, row_count, truncated, items: [...]}. When "
    "the mirror file is missing the call fails with category 'not_found' and a hint to "
    "run `halocli sync`. Mirror state, including per-resource truncation, is recorded "
    "in the mirror_state table - consult it before trusting a total."
)

_BATCH_DESCRIPTION = (
    "Run up to 25 GET requests against HaloPSA in one call and return every result in "
    "input order. This is the codemode workhorse: fetch many independent records "
    "(for example the actions on 40 ticket ids) without 40 separate tool calls, then "
    "filter or aggregate in the script so only the digest reaches the model.\n"
    "\n"
    "Arguments:\n"
    "  calls (required): array of {method, path, params?, body?}. method defaults to "
    "GET and must be GET - batch is read-only by design, so a write guardrail can "
    "never be amplified across many records. Use halo_execute with apply: true for a "
    "single write, or the `halocli bulk` JSONL driver (preview-all-then-apply) for a "
    "reviewed write plan.\n"
    "  concurrency (optional): parallel in-flight requests, default 5, max 10. Halo's "
    "fair-use limit is 700 requests per rolling 5 minutes; keep batches modest.\n"
    "  profile (optional): HaloCLI profile name; defaults to the server default."
    "\n"
    "Results: {ok, count, concurrency, errors, results: [{index, method, path, ok, "
    "data | category, status_code, error}]}. Failures are isolated per call: one bad "
    "id does not sink the batch, and a failing item is reported rather than thrown. "
    "Each item's payload is independently bounded (6k chars) so one huge record cannot "
    "crowd out the rest; oversized items come back as {truncated, total_chars, "
    "preview}."
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
        "name": "halo_batch",
        "description": _BATCH_DESCRIPTION,
        # Read-only by construction: only GET is accepted, so no confirmation flag
        # applies and every call in the batch is a pure read.
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH_CALLS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "enum": ["GET"],
                                "default": "GET",
                                "description": "Batch is read-only: GET only.",
                            },
                            "path": {
                                "type": "string",
                                "description": "REST path relative to the API root, e.g. '/Tickets/123'.",
                            },
                            "params": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": "Query parameters as string values.",
                            },
                        },
                        "required": ["path"],
                    },
                    "description": f"1-{MAX_BATCH_CALLS} GET operations, each {{path, params?}}.",
                },
                "concurrency": {
                    "type": "integer",
                    "default": DEFAULT_BATCH_CONCURRENCY,
                    "minimum": 1,
                    "maximum": MAX_BATCH_CONCURRENCY,
                    "description": "Parallel in-flight requests (default 5, max 10).",
                },
                "profile": {
                    "type": "string",
                    "description": "HaloCLI profile name; defaults to the server default.",
                },
            },
            "required": ["calls"],
        },
    },
    {
        "name": "halo_sql",
        "description": _SQL_DESCRIPTION,
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
                    "description": "One SELECT statement against the local mirror.",
                },
                "limit": {
                    "type": "integer",
                    "default": DEFAULT_SQL_LIMIT,
                    "minimum": 1,
                    "maximum": MAX_SQL_LIMIT,
                    "description": f"Row ceiling (default {DEFAULT_SQL_LIMIT}, max {MAX_SQL_LIMIT}).",
                },
                "db": {
                    "type": "string",
                    "description": "Mirror database path; defaults to the standard location.",
                },
            },
            "required": ["query"],
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
                f"'params[{key}]' must be a string value; nested objects and arrays are not allowed"
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


def _tool_halo_search(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
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


def _tool_halo_resources(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
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
    profile = args.get("profile") or _OPTIONS.profile
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


def _coerce_int(raw: Any, *, default: int, low: int, high: int, name: str) -> int:
    """Clamp a caller-supplied integer option, refusing nonsense with a clear message."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"'{name}' must be an integer between {low} and {high}")
    try:
        value = int(raw.strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{name}' must be an integer between {low} and {high}") from exc
    return max(low, min(value, high))


def _bound_item(data: Any, *, max_chars: int = MAX_BATCH_ITEM_CHARS) -> Any:
    """Bound one batch item so a single huge record cannot crowd out its siblings."""
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return data
    return {"truncated": True, "total_chars": len(text), "preview": text[:max_chars]}


def _tool_halo_sql(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _validation_failure(
            "'query' is required: one SELECT statement against the local mirror"
        )
    try:
        limit = _coerce_int(
            args.get("limit"),
            default=DEFAULT_SQL_LIMIT,
            low=1,
            high=MAX_SQL_LIMIT,
            name="limit",
        )
    except ValueError as exc:
        return _validation_failure(str(exc))
    db = args.get("db") or _OPTIONS.db_path
    if db is not None and (not isinstance(db, str) or not db.strip()):
        return _validation_failure("'db' must be a non-empty path string")
    try:
        cleaned = validate_select_only(query)
    except SQLGuardError as exc:
        return _validation_failure(str(exc))
    mirror_path = Path(db) if db else default_mirror_path()
    try:
        conn = open_existing(mirror_path)
    except MirrorMissingError as exc:
        payload = {
            "ok": False,
            "category": "not_found",
            "status_code": None,
            "error": str(exc),
            "db": str(mirror_path),
            "hint": (
                "Build the mirror first: `halocli sync` (bounded, ops-relevant core) or "
                "`halocli sync --all-resources`. This tool never reads the live tenant."
            ),
        }
        return payload, True
    try:
        cursor = conn.execute(cleaned)
        columns = [column[0] for column in cursor.description] if cursor.description else []
        raw_rows = cursor.fetchmany(limit + 1)
    except Exception as exc:  # sqlite surfaces SQL errors as exceptions
        return _validation_failure(f"Mirror query failed: {exc}")
    finally:
        conn.close()
    truncated = len(raw_rows) > limit
    items = [dict(row) for row in raw_rows[:limit]]
    payload = {
        "ok": True,
        "db": str(mirror_path),
        "query": cleaned,
        "columns": columns,
        "row_count": len(items),
        "truncated": truncated,
        "items": items,
    }
    if truncated:
        payload["hint"] = (
            f"Showing the first {limit} rows. Raise 'limit' (max {MAX_SQL_LIMIT}) or "
            "narrow the query with WHERE/GROUP BY."
        )
    return payload, False


async def _tool_halo_batch(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    calls = args.get("calls")
    if not isinstance(calls, list) or not calls:
        return _validation_failure(
            "'calls' is required: a non-empty array of {path, params?} GET operations"
        )
    if len(calls) > MAX_BATCH_CALLS:
        return _validation_failure(
            f"Too many calls ({len(calls)}); the batch ceiling is {MAX_BATCH_CALLS}. "
            "Split the work into more than one batch."
        )
    try:
        concurrency = _coerce_int(
            args.get("concurrency"),
            default=DEFAULT_BATCH_CONCURRENCY,
            low=1,
            high=MAX_BATCH_CONCURRENCY,
            name="concurrency",
        )
    except ValueError as exc:
        return _validation_failure(str(exc))
    profile = args.get("profile") or _OPTIONS.profile
    if not isinstance(profile, str) or not profile.strip():
        return _validation_failure("'profile' must be a non-empty string")

    normalized: list[tuple[str, str, dict[str, str] | None]] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            return _validation_failure(f"calls[{index}] must be an object")
        method = str(call.get("method") or "GET").strip().upper()
        if method != "GET":
            return _validation_failure(
                f"calls[{index}]: halo_batch is read-only and accepts GET only (got {method}). "
                "Use halo_execute with apply: true for a single write, or `halocli bulk` "
                "for a previewed write plan."
            )
        path = call.get("path")
        if not isinstance(path, str) or not path.strip():
            return _validation_failure(f"calls[{index}]: 'path' is required")
        if "body" in call and call.get("body") is not None:
            return _validation_failure(f"calls[{index}]: GET requests do not take a body")
        try:
            params = _normalize_params(call.get("params"))
        except ValueError as exc:
            return _validation_failure(f"calls[{index}]: {exc}")
        normalized.append((method, path.strip(), params))

    client = _client_for(profile.strip())
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(
        index: int, method: str, path: str, params: dict[str, str] | None
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {"index": index, "method": method, "path": path}
        async with semaphore:
            try:
                data = await client.raw(method, path, params=params, body=None)
            except HaloCLIError as exc:
                entry.update(
                    ok=False,
                    category=exc.category,
                    status_code=exc.status_code,
                    error=str(exc),
                )
                return entry
            except Exception as exc:  # isolate: one bad call must not sink the batch
                entry.update(ok=False, category="unknown", error=f"{type(exc).__name__}: {exc}")
                return entry
        entry.update(ok=True, data=_bound_item(data))
        return entry

    try:
        results = await asyncio.gather(
            *(
                run_one(index, method, path, params)
                for index, (method, path, params) in enumerate(normalized)
            )
        )
    finally:
        await _close_client(client)
    failures = sum(1 for entry in results if not entry.get("ok"))
    payload = {
        "ok": failures == 0,
        "count": len(results),
        "concurrency": concurrency,
        "errors": failures,
        "results": results,
        "hint": (
            "Each item is bounded independently; filter or aggregate in the script so "
            "only the digest reaches the model. Halo's fair-use limit is 700 requests "
            f"per rolling 5 minutes - {MAX_BATCH_CALLS} calls per batch is deliberate."
        ),
    }
    return payload, False


_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "halo_search": _tool_halo_search,
    "halo_execute": _tool_halo_execute,
    "halo_batch": _tool_halo_batch,
    "halo_sql": _tool_halo_sql,
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


async def _await(result: Awaitable[tuple[dict[str, Any], bool]]) -> tuple[dict[str, Any], bool]:
    return await result


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        outcome = _TOOL_HANDLERS[name](arguments)
        payload, is_error = (
            asyncio.run(_await(outcome)) if isinstance(outcome, Awaitable) else outcome
        )
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
    # ensure_ascii keeps the wire ASCII-only: JSON escapes are valid for every
    # decoder, and a Windows console in cp1252 can otherwise crash the server on
    # the first non-ASCII character in tenant data (a ticket summary emoji is
    # enough). Proven by the stdio smoke test that surfaced this.
    return json.dumps(response, ensure_ascii=True)


def main(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    *,
    default_profile: str = "default",
    db_path: str | None = None,
) -> None:
    """Serve newline-delimited JSON-RPC 2.0 on stdin/stdout until EOF.

    ``default_profile`` and ``db_path`` come from ``halocli serve --profile/--db`` and
    act as process-wide defaults: an explicit ``profile``/``db`` argument on a tool call
    still wins, so one server can reach several profiles while pointing mirror queries
    at a chosen database.
    """
    global _OPTIONS
    _OPTIONS = ServerOptions(profile=default_profile, db_path=db_path)
    stdin = input_stream if input_stream is not None else sys.stdin
    stdout = output_stream if output_stream is not None else sys.stdout
    # MCP speaks UTF-8. Reconfigure the real streams so tenant data in any script
    # survives the round trip; test doubles (StringIO) have no reconfigure.
    for stream in (stdin, stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - stream already bound
                pass
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
