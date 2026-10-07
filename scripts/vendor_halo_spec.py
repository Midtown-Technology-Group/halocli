#!/usr/bin/env python3
"""Vendor the HaloPSA REST API v2 OpenAPI specification into the repository.

Downloads the platform-published OpenAPI 3.0.1 spec, trims it for size, and writes it to
``src/halocli/spec/halo_openapi.json``. Re-runnable whenever the upstream spec changes.

Source (tenant-agnostic, served unauthenticated by every Halo instance):
    https://dtcdev.halopsa.com/api/swagger/v2/swagger.json
Mirrors (byte-identical), e.g.:
    https://midtowntg.halopsa.com/api/swagger/v2/swagger.json
Discovered via the DTC Inc. HaloPSA API Reference:
    https://kb.dtctoday.com/books/halopsa-api-reference/page/halopsa-rest-api-v2-swagger-spec

Usage:
    python scripts/vendor_halo_spec.py                     # download + trim + enrich + write
    python scripts/vendor_halo_spec.py --source raw.json   # trim a local raw copy (offline)
    python scripts/vendor_halo_spec.py --no-overlay        # skip curated prose enrichment
    python scripts/vendor_halo_spec.py --url https://host/api/swagger/v2/swagger.json

Enrichment (both steps are deterministic and re-runnable):

* missing ``operationId`` values are synthesized as ``{method}_{path}``
  (e.g. ``get_invoice_pdf_id``) — upstream IDs are never overwritten;
* ``halo_overlay.json`` (committed next to the spec) fills ``summary`` /
  ``description`` for the operations HaloCLI surfaces. Fill-if-missing semantics:
  upstream text is never overwritten, so spec refreshes keep upstream improvements.
  When the registry grows, the surfaced-operations test fails until the overlay
  is extended — edit the overlay JSON directly.

The script is idempotent: re-running it against an unchanged upstream spec produces the same
output apart from the ``vendored_at`` timestamp in ``_meta``.

No third-party dependencies; Python 3.10+ stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _safe_path(p: str | Path) -> Path:
    """Canonicalize a CLI-supplied path before touching the disk (S8707)."""
    return Path(p).expanduser().resolve()


def _safe_url(url: str) -> str:
    """Validate a CLI-supplied fetch URL before requesting it (S8703).

    Spec downloaders are https-only: no scheme tricks, no userinfo.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        raise ValueError(f"refusing non-https spec URL: {url!r}")
    return url


def _vendor_safe_paths(
    source_file: Path | None, out_path: Path, overlay_path: Path | None
) -> tuple[Path | None, Path, Path | None]:
    """Canonicalize every CLI-supplied path before any disk I/O (S8707)."""
    safe_source = _safe_path(source_file) if source_file is not None else None
    safe_out = _safe_path(out_path)
    safe_overlay = _safe_path(overlay_path) if overlay_path is not None else None
    return safe_source, safe_out, safe_overlay


DEFAULT_URL = "https://dtcdev.halopsa.com/api/swagger/v2/swagger.json"
MIRROR_URLS = ["https://midtowntg.halopsa.com/api/swagger/v2/swagger.json"]
DISCOVERED_VIA = (
    "https://kb.dtctoday.com/books/halopsa-api-reference/page/halopsa-rest-api-v2-swagger-spec"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"
DEFAULT_OVERLAY = REPO_ROOT / "src" / "halocli" / "spec" / "halo_overlay.json"

MAX_DESCRIPTION = 200
DROP_KEYS = frozenset({"example", "examples"})
EMPTY_LIST_KEYS = frozenset({"parameters", "tags", "required", "enum"})
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SCHEMA_REF_PREFIX = "#/components/schemas/"

USER_AGENT = "halocli-vendor-script (+https://github.com/Midtown-Technology-Group/halocli)"


def fetch_spec(url: str) -> tuple[dict[str, Any], bytes]:
    """Download and parse the raw OpenAPI document from *url*; returns (spec, raw bytes)."""
    url = _safe_url(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    spec = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(spec, dict) or "paths" not in spec:
        raise ValueError(f"downloaded document from {url} is not an OpenAPI spec (no 'paths')")
    return spec, payload


def _clip(text: str) -> str:
    """Collapse whitespace and truncate *text* to MAX_DESCRIPTION characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_DESCRIPTION:
        return collapsed
    return collapsed[: MAX_DESCRIPTION - 1].rstrip() + "\u2026"


def trim(node: Any, *, property_names: bool = False) -> Any:
    """Recursively strip x- extensions/examples and clip long descriptions.

    ``property_names`` marks the inside of a ``properties`` map: its KEYS are field names,
    not schema keywords, so they are never dropped or rewritten (a field named ``example``
    or ``x-foo`` must survive, even though it would be dropped anywhere else - and its name
    may still appear in the enclosing ``required`` list). Only the values of such keys are
    trimmed, with the normal keyword rules re-applied to them as schemas.
    """
    if isinstance(node, dict):
        trimmed: dict[str, Any] = {}
        for key, value in node.items():
            if not property_names and (key.startswith("x-") or key in DROP_KEYS):
                continue
            if isinstance(value, str) and not property_names and key in ("description", "summary"):
                value = _clip(value)
                if not value:
                    continue
            elif isinstance(value, (dict, list)):
                # A properties map's VALUES are schemas -> trim them with keyword rules.
                value = trim(value, property_names=key == "properties" and not property_names)
                if (
                    isinstance(value, list)
                    and not value
                    and not property_names
                    and key in EMPTY_LIST_KEYS
                ):
                    continue
            trimmed[key] = value
        return trimmed
    if isinstance(node, list):
        return [trim(item) for item in node]
    return node


def _refs_in(node: Any) -> set[str]:
    """Collect every ``$ref`` string contained in *node*."""
    refs: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.add(ref)
        for value in node.values():
            refs.update(_refs_in(value))
    elif isinstance(node, list):
        for item in node:
            refs.update(_refs_in(item))
    return refs


def prune_components(spec: dict[str, Any]) -> tuple[int, int]:
    """Drop component schemas not reachable from ``paths`` (transitively)."""
    components = spec.get("components")
    if not isinstance(components, dict):
        return 0, 0
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return 0, 0

    keep: set[str] = set()
    queue: deque[str] = deque(_refs_in(spec.get("paths", {})))
    while queue:
        ref = queue.popleft()
        if not ref.startswith(SCHEMA_REF_PREFIX):
            continue
        name = ref[len(SCHEMA_REF_PREFIX) :]
        if name in keep or name not in schemas:
            continue
        keep.add(name)
        queue.extend(_refs_in(schemas[name]))

    before = len(schemas)
    components["schemas"] = {name: node for name, node in schemas.items() if name in keep}
    return before, len(keep)


def _count_operations(spec: dict[str, Any]) -> int:
    total = 0
    for item in spec.get("paths", {}).values():
        if isinstance(item, dict):
            total += sum(1 for method in item if method in HTTP_METHODS)
    return total


def synthesize_operation_ids(spec: dict[str, Any]) -> tuple[int, int]:
    """Fill missing ``operationId`` values with a deterministic ``{method}_{path}`` rule.

    Halo's spec ships almost no operationIds (3/1455), but typed consumers
    (Forge, Fern, OpenAPI tooling) key everything on them — a spec without IDs
    resolves to ~nothing (measured: 3/1455 in Forge). The rule is mechanical and
    stable: ``GET /Invoice/PDF/{id}`` -> ``get_invoice_pdf_id``. Upstream IDs are
    never overwritten; collisions get a ``_2`` suffix in stable iteration order.

    Returns ``(synthesized, upstream_kept)``.
    """
    taken = {
        op["operationId"]
        for item in spec.get("paths", {}).values()
        if isinstance(item, dict)
        for method, op in item.items()
        if method in HTTP_METHODS and isinstance(op, dict) and op.get("operationId")
    }
    upstream = len(taken)
    synthesized = 0
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue
            if op.get("operationId"):
                continue
            base = re.sub(r"[^0-9A-Za-z]+", "_", f"{method}_{path}").strip("_").lower()
            candidate = base or f"{method}_op"
            if candidate in taken:
                suffix = 2
                while f"{base}_{suffix}" in taken:
                    suffix += 1
                candidate = f"{base}_{suffix}"
            taken.add(candidate)
            op["operationId"] = candidate
            synthesized += 1
    return synthesized, upstream


# Overlay targets we support -- a deliberately constrained JSONPath subset
# (stdlib-only vendor script, no JSONPath dependency). Three shapes:
#
#   $.paths['<path>'].<method>.summary|description
#   $.paths['<path>'].<method>.parameters['<name>'].description
#   $.components.schemas.<Schema>.properties.<prop>.description
#
# The emitted targets are valid JSONPath, so external runners (e.g. Forge's
# applyForgeOverlays) can apply the same file -- tests pin that claim against
# jsonpath-ng rather than trusting it. Parameters are addressed by a JSONPath
# filter on name (not a positional index, and not the pseudo-path
# parameters['name'], which jsonpath-ng parses but resolves to zero nodes --
# an external runner would silently skip the action while applying the rest).
# The compound form parameters[?@.name=='x' && @.in=='y'] is deliberately NOT
# supported: jsonpath-ng rejects it at parse time, so supporting it here would
# reintroduce the split-brain this avoids.
_OVERLAY_TARGET = re.compile(
    r"^\$\.paths\['(?P<path>[^']+)'\]\.(?P<method>[a-z]+)\.(?P<field>summary|description)$"
)
_OVERLAY_PARAM_TARGET = re.compile(
    r"^\$\.paths\['(?P<path>[^']+)'\]\.(?P<method>[a-z]+)"
    r"\.parameters\[\?@\.name=='(?P<name>[^']+)'\]\.description$"
)
_OVERLAY_SCHEMA_TARGET = re.compile(
    r"^\$\.components\.schemas\.(?P<schema>[A-Za-z0-9_]+)"
    r"\.properties\.(?P<prop>[A-Za-z0-9_]+)\.description$"
)


def _overlay_operation(spec: dict[str, Any], path: str, method: str, target: str) -> dict[str, Any]:
    item = spec.get("paths", {}).get(path)
    operation = item.get(method) if isinstance(item, dict) else None
    if not isinstance(operation, dict):
        raise ValueError(f"overlay target path/method not in spec: {target!r}")
    return operation


def resolve_overlay_target(spec: dict[str, Any], target: Any) -> tuple[dict[str, Any], str]:
    """Locate the ``(container, field)`` an overlay target points at.

    Split out of apply_overlay so tests can assert that every target in the
    overlay resolves against the committed spec *without* mutating it --
    schema.load_spec() caches process-wide, so a mutating check would leak
    into later tests.
    """
    text = str(target)

    match = _OVERLAY_TARGET.match(text)
    if match:
        return _overlay_operation(spec, match["path"], match["method"], text), match["field"]

    match = _OVERLAY_PARAM_TARGET.match(text)
    if match:
        operation = _overlay_operation(spec, match["path"], match["method"], text)
        parameters = operation.get("parameters")
        if not isinstance(parameters, list):
            raise ValueError(f"overlay target operation declares no parameters: {text!r}")
        for parameter in parameters:
            if isinstance(parameter, dict) and parameter.get("name") == match["name"]:
                return parameter, "description"
        raise ValueError(f"overlay target parameter not in spec: {text!r}")

    match = _OVERLAY_SCHEMA_TARGET.match(text)
    if match:
        schema = spec.get("components", {}).get("schemas", {}).get(match["schema"])
        properties = schema.get("properties") if isinstance(schema, dict) else None
        prop = properties.get(match["prop"]) if isinstance(properties, dict) else None
        if not isinstance(prop, dict):
            raise ValueError(f"overlay target schema property not in spec: {text!r}")
        return prop, "description"

    raise ValueError(
        "unsupported overlay target (expected $.paths['...'].<method>.summary|description, "
        "$.paths['...'].<method>.parameters[?@.name=='<name>'].description, or "
        "$.components.schemas.<Schema>.properties.<prop>.description): "
        f"{text!r}"
    )


def apply_overlay(spec: dict[str, Any], overlay: dict[str, Any]) -> tuple[int, int]:
    """Apply curated prose from an OpenAPI Overlay document to the spec.

    Supports three target shapes (see resolve_overlay_target), each taking a
    string ``update``. Semantics are **fill-if-missing**: an action never
    overwrites text the upstream spec already provides, so spec refreshes keep
    upstream improvements (documented in the overlay's info.description — a
    standard overlay runner would overwrite instead).

    Returns ``(filled, already_present)``. Raises ValueError when a target
    names something the spec does not contain, or has a shape we do not
    support (loud, rather than silently skipping prose).
    """
    if overlay.get("overlay") not in ("1.0.0", "1.1.0"):
        raise ValueError(f"unsupported overlay version: {overlay.get('overlay')!r}")
    actions = overlay.get("actions")
    if not isinstance(actions, list):
        raise ValueError("overlay has no 'actions' list")

    filled = already_present = 0
    for i, action in enumerate(actions):
        if not isinstance(action, dict) or "update" not in action:
            raise ValueError(f"overlay action #{i} needs a target and an update")
        target = action.get("target")
        update = action["update"]
        if not isinstance(update, str) or not update.strip():
            raise ValueError(f"overlay action #{i} has a non-text update")
        container, field = resolve_overlay_target(spec, target)
        current = container.get(field)
        if isinstance(current, str) and current.strip():
            already_present += 1
            continue
        container[field] = update
        filled += 1
    return filled, already_present


def vendor(
    url: str,
    source_file: Path | None,
    out_path: Path,
    overlay_path: Path | None = DEFAULT_OVERLAY,
) -> dict[str, Any]:
    """Fetch (or read), trim, enrich, and write the vendored spec. Returns the written spec."""
    # canonicalize every CLI-supplied path before any disk I/O (S8707)
    source_file, out_path, overlay_path = _vendor_safe_paths(source_file, out_path, overlay_path)
    raw_bytes: bytes | None
    if source_file is not None:
        raw_bytes = source_file.read_bytes()
        source_label = f"file:{source_file}"
        spec = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(spec, dict) or "paths" not in spec:
            raise ValueError(f"{source_file} is not an OpenAPI spec (no 'paths')")
    else:
        spec, raw_bytes = fetch_spec(url)
        source_label = url

    spec = trim(spec)
    schemas_before, schemas_after = prune_components(spec)
    operation_ids_synthesized, operation_ids_upstream = synthesize_operation_ids(spec)

    overlay_stats: dict[str, Any] | None = None
    if overlay_path is not None:
        if not overlay_path.is_file():
            raise ValueError(
                f"overlay not found: {overlay_path} "
                "(committed next to the spec; pass --no-overlay to skip)"
            )
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        if not isinstance(overlay, dict):
            raise ValueError(f"{overlay_path} is not an overlay document")
        filled, already_present = apply_overlay(spec, overlay)
        overlay_stats = {
            "file": overlay_path.name,
            "version": overlay.get("overlay"),
            "actions": len(overlay.get("actions") or []),
            "filled": filled,
            "already_present": already_present,
        }

    path_count = len(spec.get("paths", {}))
    operation_count = _count_operations(spec)
    missing_descriptions = sum(
        1
        for item in spec.get("paths", {}).values()
        if isinstance(item, dict)
        for method, op in item.items()
        if method in HTTP_METHODS
        and isinstance(op, dict)
        and not (op.get("description") or "").strip()
    )
    meta = {
        "$schema_source": source_label,
        "origin": "official",
        "api": "HaloPSA REST API v2",
        "openapi": spec.get("openapi"),
        "discovered_via": DISCOVERED_VIA,
        "mirror_urls": MIRROR_URLS,
        "vendored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vendor_script": "scripts/vendor_halo_spec.py",
        "raw_bytes": len(raw_bytes) if raw_bytes is not None else None,
        "path_count": path_count,
        "operation_count": operation_count,
        "schema_count_before": schemas_before,
        "schema_count_after": schemas_after,
        "operation_ids_synthesized": operation_ids_synthesized,
        "operation_ids_upstream": operation_ids_upstream,
        "overlay": overlay_stats,
        "missing_descriptions": missing_descriptions,
        "trim_notes": [
            "x-* vendor extensions removed",
            "example/examples removed",
            "descriptions/summaries collapsed and clipped to 200 chars",
            "component schemas unreachable from paths removed (transitive)",
            "missing operationIds synthesized as {method}_{path} (upstream IDs kept)",
            "curated overlay fills summary/description for surfaced operations "
            "(fill-if-missing: never overwrites upstream text)",
        ],
    }
    spec["_meta"] = meta
    spec["$schema_source"] = source_label

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(spec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    out_path.write_bytes(payload)  # write_bytes: no newline translation, byte-exact output
    meta["trimmed_bytes"] = len(payload)  # reported by main(); written file keeps the pre-dump copy
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="OpenAPI spec URL to download")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="trim a local raw spec file instead of downloading (offline mode)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output path")
    parser.add_argument(
        "--overlay",
        type=Path,
        default=DEFAULT_OVERLAY,
        help="curated overlay document (fill-if-missing prose for surfaced operations)",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="skip overlay application (spec only; surfaced operations may lack prose)",
    )
    args = parser.parse_args(argv)

    try:
        spec = vendor(
            args.url,
            args.source,
            args.out,
            overlay_path=None if args.no_overlay else args.overlay,
        )
    except (OSError, ValueError) as exc:
        print(f"error: failed to vendor HaloPSA spec: {exc}", file=sys.stderr)
        print(
            "hint: retry later, or trim a cached raw copy with: "
            "python scripts/vendor_halo_spec.py --source raw_swagger.json",
            file=sys.stderr,
        )
        return 1

    meta = spec["_meta"]
    overlay = meta.get("overlay")
    overlay_note = (
        f"  overlay: {overlay['file']} — {overlay['filled']} filled, "
        f"{overlay['already_present']} already present\n"
        if overlay
        else "  overlay: skipped (--no-overlay)\n"
    )
    print(f"wrote {args.out}")
    print(
        f"  source: {meta['$schema_source']}\n"
        f"  paths: {meta['path_count']}  operations: {meta['operation_count']}  "
        f"schemas: {meta['schema_count_before']} -> {meta['schema_count_after']}\n"
        f"  operationIds: {meta['operation_ids_synthesized']} synthesized, "
        f"{meta['operation_ids_upstream']} upstream\n"
        f"{overlay_note}"
        f"  missing descriptions: {meta['missing_descriptions']}\n"
        f"  size: {meta['trimmed_bytes']:,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
