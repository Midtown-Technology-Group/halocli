#!/usr/bin/env python3
"""Render Halo custom-integration METHOD rows from a vendor OpenAPI spec.

One row per operation -> the ``methods:`` YAML shape ``convert_methods``
already consumes (``--methods`` -> POST /CustomIntegrationMethod cascade,
per-method ``_test`` on --apply):

    methods:
      - name: getV1Account        # operationId (or {verb}_{path} synthesized)
        path: /v1/account         # kept verbatim, {templated} included
        method: GET               # Halo verb enum (HEAD/OPTIONS skipped)
        bind_phase: list_alerts   # only from --bind-map {phase: method}

Usage:
    python scripts/openapi_methods.py specs/huntress.openapi.json --out huntress_methods.yaml
    python scripts/openapi_methods.py spec.yaml --out m.yaml --skip-templated --include "^GET "
    python scripts/openapi_methods.py spec.yaml --emit-integration "Vendor X" --integration-out int.yaml
    python scripts/openapi_methods.py spec.yaml --out m.yaml --bind-map binds.json

Notes reported (not guessed): unsupported verbs skipped, operations
carrying query params (bake required ones into the path yourself),
templated paths (path params are unpinned on the trial - create errors
are recorded per method by --apply, never silently), security schemes
(configure auth on the INTEGRATION row - the integration-out hint gives
the base URL the spec declares).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HALO_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH")
SPEC_VERBS = ("get", "post", "put", "delete", "patch", "head", "options")
UNSUPP_VERBS = ("head", "options")


def load_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        doc = yaml.safe_load(text)
    else:
        doc = json.loads(text)
    if not isinstance(doc, dict) or not doc.get("paths"):
        raise SystemExit(f"{path}: not an OpenAPI/Swagger document (no paths)")
    return doc


def spec_base_url(spec: dict) -> str | None:
    """Base URL hint: OAS3 servers[] first, Swagger2 schemes+host+basePath."""
    servers = spec.get("servers") or []
    if servers and servers[0].get("url"):
        return str(servers[0]["url"]).rstrip("/")
    if spec.get("host"):
        scheme = (spec.get("schemes") or ["https"])[0]
        base = str(spec.get("basePath") or "").rstrip("/")
        return f"{scheme}://{spec['host']}{base}"
    return None


def spec_to_methods(
    spec: dict,
    bind_map: dict[str, str] | None = None,
    include: str | None = None,
    exclude: str | None = None,
    skip_templated: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    """Operations -> method rows + a stats dict (counts, skips, caveats)."""
    bind_map = bind_map or {}
    inc = re.compile(include) if include else None
    exc = re.compile(exclude) if exclude else None
    stats: dict[str, Any] = {
        "operations": 0,
        "emitted": 0,
        "skipped_verb": 0,
        "skipped_templated": 0,
        "skipped_filter": 0,
        "templated": 0,
        "with_query_params": 0,
        "synthesized_names": 0,
        "security": sorted(spec.get("security") or []),
        "security_schemes": sorted(
            (
                spec.get("securityDefinitions")
                or spec.get("components", {}).get("securitySchemes")
                or {}
            )
        ),
        "base_url": spec_base_url(spec),
        "version": (spec.get("info") or {}).get("version"),
        "title": (spec.get("info") or {}).get("title"),
        "unsupported_verbs": set(),
        "duplicate_names": [],
    }
    rows: list[dict] = []
    seen: set[str] = set()
    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        for verb in SPEC_VERBS:
            if verb not in item:
                continue
            stats["operations"] += 1
            op = item[verb] or {}
            key = f"{verb.upper()} {path}"
            if inc and not inc.search(key):
                stats["skipped_filter"] += 1
                continue
            if exc and exc.search(key):
                stats["skipped_filter"] += 1
                continue
            if verb in UNSUPP_VERBS or verb.upper() not in HALO_VERBS:
                stats["skipped_verb"] += 1
                stats["unsupported_verbs"].add(verb.upper())
                continue
            if "{" in path:
                stats["templated"] += 1
                if skip_templated:
                    stats["skipped_templated"] += 1
                    continue
            if any(p.get("in") == "query" for p in op.get("parameters") or []):
                stats["with_query_params"] += 1
            name = str(op.get("operationId") or "").strip()
            if not name:
                slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")
                name = f"{verb}_{slug}"
                stats["synthesized_names"] += 1
            if name in seen:
                stats["duplicate_names"].append(name)
                name = f"{name}_{verb}"
            seen.add(name)
            row: dict = {"name": name, "path": path, "method": verb.upper()}
            # phase-bridge: bind_map is {phase_label: method_name}
            for phase, mname in bind_map.items():
                if mname == name:
                    row["bind_phase"] = phase
            rows.append(row)
            stats["emitted"] += 1
    if isinstance(stats["unsupported_verbs"], set):
        stats["unsupported_verbs"] = sorted(stats["unsupported_verbs"])
    return rows, stats


def integration_yaml(name: str, base_url: str) -> str:
    """Integration entry (config_schema base_url -> resourcebaseurl)."""
    import yaml

    entry = {
        "integrations": {
            "00000000-0000-4000-8000-0000000f0001": {
                "id": "00000000-0000-4000-8000-0000000f0001",
                "name": name,
                "config_schema": [
                    {
                        "key": "base_url",
                        "type": "string",
                        "required": True,
                        "description": f"Base URL from the spec ({base_url})",
                    }
                ],
            }
        }
    }
    return yaml.safe_dump(entry, sort_keys=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path, help="OpenAPI/Swagger JSON or YAML")
    ap.add_argument("--out", type=Path, help="methods YAML output")
    ap.add_argument("--bind-map", type=Path, help="{phase_label: method_name} JSON")
    ap.add_argument("--include", help="regex kept against 'VERB /path'")
    ap.add_argument("--exclude", help="regex dropped from 'VERB /path'")
    ap.add_argument("--skip-templated", action="store_true", help="drop /{param} paths")
    ap.add_argument("--emit-integration", metavar="NAME", help="also emit an integration YAML")
    ap.add_argument("--integration-out", type=Path, help="integration YAML output path")
    args = ap.parse_args()

    import yaml

    spec = load_spec(args.spec)
    bind_map = json.loads(args.bind_map.read_text(encoding="utf-8")) if args.bind_map else None
    rows, stats = spec_to_methods(
        spec,
        bind_map,
        include=args.include,
        exclude=args.exclude,
        skip_templated=args.skip_templated,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            yaml.safe_dump({"methods": rows}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    if args.emit_integration:
        if not stats["base_url"]:
            raise SystemExit("--emit-integration needs a spec servers/host URL")
        if not args.integration_out:
            raise SystemExit("--emit-integration requires --integration-out")
        args.integration_out.write_text(
            integration_yaml(args.emit_integration, stats["base_url"]), encoding="utf-8"
        )

    print(f"spec: {stats['title']} {stats['version']} | base URL: {stats['base_url']}")
    print(
        f"operations {stats['operations']} -> emitted {stats['emitted']}"
        f" (skipped: {stats['skipped_verb']} unsupported verb, "
        f"{stats['skipped_templated']} templated, {stats['skipped_filter']} filtered)"
    )
    print(
        f"caveats: {stats['templated']} templated paths kept, "
        f"{stats['with_query_params']} ops carry query params, "
        f"{len(stats['duplicate_names'])} duplicate names deduped"
    )
    if stats["security_schemes"] or stats["security"]:
        print(
            f"auth: schemes {stats['security_schemes']} - configure on the "
            "INTEGRATION row (authorizationtype/headers), not per method"
        )
    if args.out:
        print(f"wrote {args.out} ({len(rows)} rows)")
    if args.integration_out:
        print(f"wrote {args.integration_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
