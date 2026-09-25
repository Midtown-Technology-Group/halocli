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
    python scripts/vendor_halo_spec.py                     # download + trim + write
    python scripts/vendor_halo_spec.py --source raw.json   # trim a local raw copy (offline)
    python scripts/vendor_halo_spec.py --url https://host/api/swagger/v2/swagger.json

The script is idempotent: re-running it against an unchanged upstream spec produces the same
output apart from the ``vendored_at`` timestamp in ``_meta``.

No third-party dependencies; Python 3.10+ stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://dtcdev.halopsa.com/api/swagger/v2/swagger.json"
MIRROR_URLS = ["https://midtowntg.halopsa.com/api/swagger/v2/swagger.json"]
DISCOVERED_VIA = (
    "https://kb.dtctoday.com/books/halopsa-api-reference/page/halopsa-rest-api-v2-swagger-spec"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"

MAX_DESCRIPTION = 200
DROP_KEYS = frozenset({"example", "examples"})
EMPTY_LIST_KEYS = frozenset({"parameters", "tags", "required", "enum"})
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SCHEMA_REF_PREFIX = "#/components/schemas/"

USER_AGENT = "halocli-vendor-script (+https://github.com/Midtown-Technology-Group/halocli)"


def fetch_spec(url: str) -> tuple[dict[str, Any], bytes]:
    """Download and parse the raw OpenAPI document from *url*; returns (spec, raw bytes)."""
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
            if (
                isinstance(value, str)
                and not property_names
                and key in ("description", "summary")
            ):
                value = _clip(value)
                if not value:
                    continue
            elif isinstance(value, (dict, list)):
                # A properties map's VALUES are schemas -> trim them with keyword rules.
                value = trim(
                    value, property_names=key == "properties" and not property_names
                )
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


def vendor(url: str, source_file: Path | None, out_path: Path) -> dict[str, Any]:
    """Fetch (or read), trim, and write the vendored spec. Returns the written spec."""
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

    path_count = len(spec.get("paths", {}))
    operation_count = _count_operations(spec)
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
        "trim_notes": [
            "x-* vendor extensions removed",
            "example/examples removed",
            "descriptions/summaries collapsed and clipped to 200 chars",
            "component schemas unreachable from paths removed (transitive)",
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
    args = parser.parse_args(argv)

    try:
        spec = vendor(args.url, args.source, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: failed to vendor HaloPSA spec: {exc}", file=sys.stderr)
        print(
            "hint: retry later, or trim a cached raw copy with: "
            "python scripts/vendor_halo_spec.py --source raw_swagger.json",
            file=sys.stderr,
        )
        return 1

    meta = spec["_meta"]
    print(f"wrote {args.out}")
    print(
        f"  source: {meta['$schema_source']}\n"
        f"  paths: {meta['path_count']}  operations: {meta['operation_count']}  "
        f"schemas: {meta['schema_count_before']} -> {meta['schema_count_after']}\n"
        f"  size: {meta['trimmed_bytes']:,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
