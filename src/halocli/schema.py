"""Offline lookup, validation, and search over the vendored HaloPSA OpenAPI spec.

The spec is vendored at ``src/halocli/spec/halo_openapi.json`` by
``scripts/vendor_halo_spec.py``. This module is dependency-free (stdlib ``json`` only) and
lazy: nothing touches the filesystem until a function below is first called, and even
``json`` itself is imported only when the spec is first loaded, so importing
``halocli.schema`` (or running ``halocli --version``) costs a few milliseconds at most.

Public API:
    SPEC_PATH           on-disk location of the vendored spec (patchable in tests)
    load_spec()         lazily load + cache the parsed spec; None if missing/corrupt
    spec_meta()         the spec's ``_meta`` block (source URL, counts); None if unavailable
    lookup_operation()  resolve method + concrete path to the spec operation object
    validate_request()  human-readable problems for a raw request body
    search_operations() offline fuzzy search over spec paths and operation summaries
    clear_cache()       drop cached state (used by tests)

Everything degrades gracefully: a missing or corrupt spec yields ``None`` / ``[]`` instead of
raising, so the CLI keeps working either way.

Matching and tokenizing are hand-rolled instead of ``re``-based to keep the module import
path free of slow stdlib modules (``re``/``pathlib``/``typing``/``urllib.parse``).
"""

from __future__ import annotations

import os

SPEC_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "spec", "halo_openapi.json"
)

_HTTP_METHODS: tuple[str, ...] = ("get", "post", "put", "patch", "delete", "head", "options")
_METHOD_ORDER: dict[str, int] = {name: index for index, name in enumerate(_HTTP_METHODS)}

# Caches. Failures are deliberately NOT cached so a restored spec file is picked up again.
_SPEC_CACHE: tuple[str, dict] | None = None
_TEMPLATE_CACHE: tuple[object, list] | None = None
_INDEX_CACHE: tuple[object, list] | None = None


class _Template:
    """A spec path template pre-compiled for concrete-path matching."""

    __slots__ = ("template", "pieces", "literal_segments", "literal_chars", "path_item")

    def __init__(
        self,
        template: str,
        pieces: tuple[tuple[bool, str], ...],
        literal_segments: int,
        literal_chars: int,
        path_item: dict,
    ) -> None:
        self.template = template
        self.pieces = pieces
        self.literal_segments = literal_segments
        self.literal_chars = literal_chars
        self.path_item = path_item


def clear_cache() -> None:
    """Drop the cached spec, template index, and search index (used by tests)."""
    global _SPEC_CACHE, _TEMPLATE_CACHE, _INDEX_CACHE
    _SPEC_CACHE = None
    _TEMPLATE_CACHE = None
    _INDEX_CACHE = None


def load_spec() -> dict | None:
    """Load the vendored spec from disk (first call only) and cache it.

    Returns ``None`` if the file is missing, unreadable, or not a valid OpenAPI document.
    """
    global _SPEC_CACHE
    key = str(SPEC_PATH)
    if _SPEC_CACHE is not None and _SPEC_CACHE[0] == key:
        return _SPEC_CACHE[1]
    import json

    try:
        with open(SPEC_PATH, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        return None
    _SPEC_CACHE = (key, spec)
    return spec


def spec_meta() -> dict | None:
    """Return the vendored spec's ``_meta`` block (source URL, counts), or ``None``."""
    spec = load_spec()
    if spec is None:
        return None
    meta = spec.get("_meta")
    return dict(meta) if isinstance(meta, dict) else None


# --------------------------------------------------------------------------------------------
# small hand-rolled helpers (no `re` on the import path)
# --------------------------------------------------------------------------------------------


def _strip_url_path(text: str) -> str:
    """Return just the path component of a URL-ish string (``scheme://host/p`` -> ``/p``)."""
    rest = text.split("://", 1)[1]
    slash = rest.find("/")
    return rest[slash:] if slash != -1 else "/"


def _has_placeholder(segment: str) -> bool:
    return "{" in segment and "}" in segment


def _literal_char_count(segment: str) -> int:
    """Number of literal (non-``{placeholder}``) characters in a template segment."""
    total = 0
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "{":
            close = segment.find("}", index)
            if close != -1:
                index = close + 1
                continue
            total += len(segment) - index
            break
        total += 1
        index += 1
    return total


def _segment_pieces(segment: str) -> tuple[tuple[bool, str], ...]:
    """Split a template segment into ordered ``(is_placeholder, literal)`` pieces.

    Literals are lowercased; placeholders carry an empty payload. For example
    ``"{id}&{seq}"`` -> ``((True, ""), (False, "&"), (True, "")).``
    """
    pieces: list[tuple[bool, str]] = []
    index = 0
    length = len(segment)
    while index < length:
        if segment[index] == "{":
            close = segment.find("}", index)
            if close != -1:
                pieces.append((True, ""))
                index = close + 1
                continue
            pieces.append((False, segment[index:].lower()))
            break
        next_placeholder = segment.find("{", index)
        if next_placeholder == -1:
            pieces.append((False, segment[index:].lower()))
            break
        pieces.append((False, segment[index:next_placeholder].lower()))
        index = next_placeholder
    return tuple(pieces)


def _match_segment(
    pieces: tuple[tuple[bool, str], ...],
    text: str,
    piece_index: int = 0,
    text_index: int = 0,
) -> bool:
    """Match a concrete segment against template pieces (placeholders backtrack)."""
    if piece_index == len(pieces):
        return text_index == len(text)
    is_placeholder, literal = pieces[piece_index]
    if not is_placeholder:
        end = text_index + len(literal)
        if end <= len(text) and text[text_index:end].lower() == literal:
            return _match_segment(pieces, text, piece_index + 1, end)
        return False
    for end in range(text_index + 1, len(text) + 1):
        if _match_segment(pieces, text, piece_index + 1, end):
            return True
    return False


def _split_tokens(text: str) -> list[str]:
    """Split lowered text into ``[a-z0-9]+`` tokens (the fuzzy-search alphabet)."""
    tokens: list[str] = []
    current = ""
    for char in text.lower():
        if char.isascii() and char.isalnum():
            current += char
        elif current:
            tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    return tokens


# --------------------------------------------------------------------------------------------
# path normalization and template matching
# --------------------------------------------------------------------------------------------


def _normalize_path(spec: dict, path: object) -> str:
    """Strip URLs/query strings/trailing slashes and the server base (e.g. ``/api``)."""
    text = path if isinstance(path, str) else str(path)
    text = text.strip()
    if not text:
        return "/"
    for separator in ("?", "#"):
        index = text.find(separator)
        if index != -1:
            text = text[:index]
    if "://" in text:
        text = _strip_url_path(text)
    if not text.startswith("/"):
        text = "/" + text
    for server in spec.get("servers") or []:
        if not isinstance(server, dict):
            continue
        base = str(server.get("url") or "")
        if "://" in base:
            base = _strip_url_path(base)
        base = base.split("{", 1)[0].rstrip("/")
        if base and base != "/" and (text == base or text.startswith(base + "/")):
            text = text[len(base) :] or "/"
            break
    if len(text) > 1:
        text = text.rstrip("/")
    return text or "/"


def _build_templates(spec: dict) -> list[_Template]:
    """Pre-compile every spec path template, most-literal (most specific) first."""
    templates: list[_Template] = []
    for template, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        segments = tuple(seg for seg in template.split("/") if seg)
        pieces = tuple(_segment_pieces(seg) for seg in segments)
        literal_segments = 0
        literal_chars = 0
        for seg in segments:
            if _has_placeholder(seg):
                literal_chars += _literal_char_count(seg)
            else:
                literal_segments += 1
                literal_chars += len(seg)
        templates.append(_Template(template, pieces, literal_segments, literal_chars, item))
    templates.sort(key=lambda tpl: (-tpl.literal_segments, -tpl.literal_chars, tpl.template))
    return templates


def _templates(spec: dict) -> list[_Template]:
    """Cached, spec-identity-keyed template list."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None and _TEMPLATE_CACHE[0] is spec:
        return _TEMPLATE_CACHE[1]
    built = _build_templates(spec)
    _TEMPLATE_CACHE = (spec, built)
    return built


def _match_path(spec: dict, path: str) -> tuple[str, dict] | None:
    """Match a normalized concrete path against the spec's path templates.

    Returns ``(template, path_item)`` choosing the most specific template (most literal
    segments wins, so ``/Tickets/zapier`` beats ``/Tickets/{id}``), or ``None``. ``path`` is
    expected to be normalized already; see :func:`_normalize_path`.
    """
    item = spec.get("paths", {}).get(path)
    if isinstance(item, dict):
        return path, item
    segments = [seg for seg in path.split("/") if seg]
    count = len(segments)
    for tpl in _templates(spec):
        if len(tpl.pieces) != count:
            continue
        if all(_match_segment(pieces, seg) for pieces, seg in zip(tpl.pieces, segments)):
            return tpl.template, tpl.path_item
    return None


# --------------------------------------------------------------------------------------------
# public lookup / validation
# --------------------------------------------------------------------------------------------


def lookup_operation(method: str, path: str) -> dict | None:
    """Resolve a concrete request path to the spec's operation object.

    Handles exact paths (``/Tickets``), templated paths (``/Tickets/{id}``), nested paths
    (``/IntegrationData/Get/SalesMailbox/{id}``), mixed segments
    (``/TicketApproval/{id}&{seq}``), the ``/api`` server prefix, query strings, full URLs,
    and trailing slashes. Returns ``None`` for unknown paths, unknown methods, or an
    unavailable spec.
    """
    spec = load_spec()
    if spec is None:
        return None
    matched = _match_path(spec, _normalize_path(spec, path))
    if matched is None:
        return None
    _, item = matched
    operation = item.get(str(method).strip().lower())
    return operation if isinstance(operation, dict) else None


def validate_request(method: str, path: str, body: object) -> list[str]:
    """Return human-readable problems for a raw request, or ``[]`` if the spec has no opinion.

    Problems cover: unknown endpoints (a single ``"unknown endpoint: ..."`` problem - the
    caller decides whether that is fatal), missing required request-body properties, and
    unknown request-body properties (``"warning: ..."`` text). If the spec file is missing or
    corrupt this returns ``[]`` (no opinion) instead of raising.
    """
    spec = load_spec()
    if spec is None:
        return []
    method_key = str(method).strip().lower()
    matched = _match_path(spec, _normalize_path(spec, path))
    if matched is None:
        return [f"unknown endpoint: {method_key.upper()} {path} (no matching path in spec)"]
    template, item = matched
    operation = item.get(method_key)
    if not isinstance(operation, dict):
        known = ", ".join(name.upper() for name in _HTTP_METHODS if name in item)
        return [
            f"unknown endpoint: {method_key.upper()} {path} "
            f"(spec has {template} with methods: {known})"
        ]

    schema, body_required = _request_schema(operation)
    if body is None:
        if body_required:
            return [f"request body is required for {method_key.upper()} {template}"]
        return []
    if schema is None:
        return []

    resolved = _resolve_ref(spec, schema)
    if resolved.get("type") == "array" or isinstance(resolved.get("items"), dict):
        if not isinstance(body, list):
            return []
        items_schema = resolved.get("items")
        problems: list[str] = []
        for index, element in enumerate(body):
            if isinstance(element, dict):
                problems.extend(
                    f"body[{index}]: {problem}"
                    for problem in _body_problems(spec, items_schema, element)
                )
        return problems
    if isinstance(body, dict):
        return _body_problems(spec, resolved, body)
    return []


# --------------------------------------------------------------------------------------------
# request-body schema helpers
# --------------------------------------------------------------------------------------------


def _request_schema(operation: dict) -> tuple[object, bool]:
    """Return ``(json_schema, required)`` for the operation's request body."""
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return None, False
    required = request_body.get("required") is True
    content = request_body.get("content")
    if not isinstance(content, dict):
        return None, required
    media: dict | None = None
    for key in ("application/json", "text/json", "application/*+json"):
        candidate = content.get(key)
        if isinstance(candidate, dict):
            media = candidate
            break
    if media is None:
        for candidate in content.values():
            if isinstance(candidate, dict):
                media = candidate
                break
    if media is None:
        return None, required
    return media.get("schema"), required


def _resolve_ref(spec: dict, node: object, limit: int = 16) -> dict:
    """Follow internal ``$ref`` chains; returns ``{}`` when unresolvable."""
    current = node
    for _ in range(limit):
        if not isinstance(current, dict):
            return {}
        ref = current.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return current
        target: object = spec
        for part in ref[2:].split("/"):
            if not isinstance(target, dict):
                target = None
                break
            target = target.get(part)
        if not isinstance(target, dict):
            return {}
        current = target
    return {}


def _object_shape(spec: dict, schema: object, depth: int = 0) -> tuple[object, list[str]]:
    """Flatten a schema to ``(properties, required)`` across ``allOf``/``oneOf``/``anyOf``.

    ``required`` is taken from the schema itself and ``allOf`` branches only (union branches
    are alternative shapes, so their required lists are not demanded).
    """
    if depth > 8:
        return None, []
    resolved = _resolve_ref(spec, schema)
    if not resolved:
        return None, []
    raw_properties = resolved.get("properties")
    properties = dict(raw_properties) if isinstance(raw_properties, dict) else None
    raw_required = resolved.get("required")
    required = (
        [name for name in raw_required if isinstance(name, str)]
        if isinstance(raw_required, list)
        else []
    )
    for key in ("allOf", "oneOf", "anyOf"):
        branches = resolved.get(key)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            branch_properties, branch_required = _object_shape(spec, branch, depth + 1)
            if branch_properties:
                if properties is None:
                    properties = {}
                properties.update(branch_properties)
            if key == "allOf":
                for name in branch_required:
                    if name not in required:
                        required.append(name)
    return properties, required


def _body_problems(spec: dict, schema: object, body: dict) -> list[str]:
    """Missing-required and unknown-property problems for one JSON object body."""
    problems: list[str] = []
    properties, required = _object_shape(spec, schema)
    for name in required:
        if name not in body:
            problems.append(f"missing required request-body property: {name}")
    if properties:
        resolved = _resolve_ref(spec, schema)
        if resolved.get("additionalProperties", True) is not True:
            for name in body:
                if name not in properties:
                    problems.append(f"warning: unknown request-body property: {name}")
    return problems


# --------------------------------------------------------------------------------------------
# offline search
# --------------------------------------------------------------------------------------------


def _build_index(spec: dict) -> list[dict]:
    """Flatten the spec into one searchable entry per operation (cached)."""
    entries: list[dict] = []
    for template, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        segments = tuple(seg.lower() for seg in template.split("/") if seg)
        literal = tuple(seg for seg in segments if not _has_placeholder(seg))
        for method, operation in item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            summary = operation.get("summary")
            summary = summary if isinstance(summary, str) else ""
            operation_id = operation.get("operationId")
            operation_id = operation_id if isinstance(operation_id, str) else ""
            raw_tags = operation.get("tags")
            tags = (
                [tag for tag in raw_tags if isinstance(tag, str)]
                if isinstance(raw_tags, list)
                else []
            )
            tokens = set(_split_tokens(summary))
            tokens.update(_split_tokens(operation_id))
            tokens.update(tag.lower() for tag in tags)
            tokens.discard("")
            entries.append(
                {
                    "method": method.upper(),
                    "path": template,
                    "summary": summary,
                    "operationId": operation_id,
                    "tags": tags,
                    "_segments": segments,
                    "_literal": literal,
                    "_tokens": frozenset(tokens),
                    "_haystack": " ".join(
                        (
                            template.lower(),
                            summary.lower(),
                            operation_id.lower(),
                            " ".join(tag.lower() for tag in tags),
                            method,
                        )
                    ),
                }
            )
    return entries


def _index(spec: dict) -> list[dict]:
    """Cached, spec-identity-keyed operation index."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and _INDEX_CACHE[0] is spec:
        return _INDEX_CACHE[1]
    built = _build_index(spec)
    _INDEX_CACHE = (spec, built)
    return built


def _score_token(token: str, entry: dict) -> int:
    """Score one query token against one operation. 0 means "no match"."""
    literal = entry["_literal"]
    if token in literal:
        return 10  # exact path segment match ranks highest
    if len(token) >= 2 and (
        any(seg.startswith(token) for seg in literal)
        or any(len(seg) >= 3 and token.startswith(seg) for seg in literal)
    ):
        return 6
    if token in entry["_tokens"] or token == entry["method"].lower():
        return 5
    if any(token in seg for seg in entry["_segments"]) or any(
        known.startswith(token) for known in entry["_tokens"]
    ):
        return 4
    if token in entry["summary"].lower():
        return 3
    if token in entry["_haystack"]:
        return 2
    return 0


def search_operations(query: str, limit: int = 20) -> list[dict]:
    """Offline fuzzy-ish search over spec paths, summaries, tags, and operation IDs.

    Every whitespace/punctuation-separated query token must match (exact segment > segment
    prefix > summary/tag token > substring). Results are ranked with exact segment matches
    first, then specificity, and truncated to *limit*. Returns ``[]`` for no match, an empty
    query, ``limit <= 0``, or an unavailable spec.
    """
    spec = load_spec()
    if spec is None or limit <= 0:
        return []
    tokens = _split_tokens(str(query))
    if not tokens:
        return []

    ranked: list[tuple[int, int, int, str, int, dict]] = []
    for entry in _index(spec):
        total = 0
        exact = 0
        for token in tokens:
            score = _score_token(token, entry)
            if score <= 0:
                total = -1
                break
            total += score
            if score == 10:
                exact += 1
        if total < 0:
            continue
        rank = _METHOD_ORDER.get(entry["method"].lower(), len(_METHOD_ORDER))
        ranked.append((-total, -exact, len(entry["path"]), entry["path"], rank, entry))
    ranked.sort(key=lambda row: row[:5])

    results: list[dict] = []
    for neg_total, _neg_exact, _length, _path, _rank, entry in ranked[:limit]:
        results.append(
            {
                "method": entry["method"],
                "path": entry["path"],
                "summary": entry["summary"],
                "operationId": entry["operationId"],
                "tags": list(entry["tags"]),
                "score": -neg_total,
            }
        )
    return results
