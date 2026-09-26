"""Coverage audit: vendored OpenAPI spec vs the curated resource registry.

Answers "which operator-relevant operations has HaloCLI not curated yet?" without
network access (it reads the vendored spec) and catches registry write metadata the
spec does not actually support — the `/Contract` case, where we shipped create/update
endpoints for a path that has no POST in the spec.

Path classification against the registry:

* ``exact``     — the registry `list` endpoint (`GET <endpoint>`)
* ``by_id``     — the registry `get` endpoint (`GET <endpoint>/{id}`)
* ``nested``    — deeper than a curated endpoint; curable by extending that resource
* ``uncurated`` — no registry resource owns this path's root segment
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from halocli.resources import RESOURCES, HaloResource
from halocli.schema import load_spec, spec_meta

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
PATH_KINDS = ("exact", "by_id", "nested", "uncurated")


def _iter_operations(spec: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield (METHOD, path) for every HTTP operation in the spec."""
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in HTTP_METHODS:
                yield method.upper(), path


def _endpoint(resource: HaloResource) -> str:
    return resource.endpoint.rstrip("/")


def _root(path: str) -> str:
    stripped = path.strip("/")
    return stripped.split("/", 1)[0] if stripped else ""


def classify_path(
    path: str,
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> tuple[str, HaloResource | None]:
    """Classify a spec path against the curated registry.

    Pure registry/-string comparison — spec membership is not consulted, so the
    result for a given path is stable across spec refreshes.
    """
    for resource in resources:
        endpoint = _endpoint(resource)
        if path in (endpoint, endpoint + "/"):
            return "exact", resource
        if path == endpoint + "/{id}":
            return "by_id", resource
    for resource in resources:
        # Trailing "/" in the prefix prevents /Client matching /ClientContract.
        if path.startswith(_endpoint(resource) + "/"):
            return "nested", resource
    return "uncurated", None


def _registry_roots(resources: tuple[HaloResource, ...]) -> dict[str, HaloResource]:
    roots: dict[str, HaloResource] = {}
    for resource in resources:
        root = _root(_endpoint(resource))
        if root:
            roots.setdefault(root.lower(), resource)
    return roots


def _related_resources(root: str, resources: tuple[HaloResource, ...]) -> list[str]:
    """Registry resources sharing a stem with `root` (e.g. TicketApproval ~ tickets)."""
    lowered = root.lower()
    hits: set[str] = set()
    for resource in resources:
        tokens = {
            resource.name.lower(),
            resource.name.lower().removesuffix("s"),
            _endpoint(resource).rsplit("/", 1)[-1].lower(),
        }
        if any(len(token) >= 3 and token in lowered for token in tokens):
            hits.add(resource.name)
    return sorted(hits)


def find_write_mismatches(
    spec: dict[str, Any],
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> list[dict[str, Any]]:
    """Return write metadata the spec does not support (the `/Contract` bug class).

    Checks every declared create/update endpoint has POST and every
    `supports_delete` resource has DELETE at `{endpoint}/{id}`, per the vendored spec.
    Empty list means the registry never promises a write the API can't take.
    """
    spec_paths = spec.get("paths", {})
    declared: dict[tuple[str, str], list[str]] = {}
    for resource in resources:
        for field_name, target in (
            ("create_endpoint", resource.create_endpoint),
            ("update_endpoint", resource.update_endpoint),
        ):
            if target:
                declared.setdefault((target, "POST"), []).append(
                    f"{resource.name}.{field_name}"
                )
        if resource.supports_delete:
            declared.setdefault((f"{_endpoint(resource)}/{{id}}", "DELETE"), []).append(
                f"{resource.name}.supports_delete"
            )

    mismatches: list[dict[str, Any]] = []
    for (path, method), fields in sorted(declared.items()):
        item = spec_paths.get(path)
        available = (
            sorted(m.lower() for m in item if m.lower() in HTTP_METHODS)
            if isinstance(item, dict)
            else []
        )
        if method.lower() not in available:
            mismatches.append(
                {
                    "path": path,
                    "method": method,
                    "declared_by": sorted(fields),
                    "spec_methods": available,
                }
            )
    return mismatches


def find_read_mismatches(
    spec: dict[str, Any],
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> list[dict[str, Any]]:
    """Return registry read endpoints the spec does not document at all.

    `classify_path` deliberately ignores spec membership (a registry endpoint stays
    classified `exact` across spec refreshes), so this is the companion check: it
    flags resources whose `endpoint` has no path in the spec — e.g. the contracts
    resource reads `/Contract`, which the official spec does not contain. Such reads
    may still work at runtime (undocumented endpoint) but the CLI promises more than
    the spec verifies.
    """
    spec_paths = spec.get("paths", {})
    mismatches: list[dict[str, Any]] = []
    for resource in resources:
        endpoint = _endpoint(resource)
        if endpoint not in spec_paths and f"{endpoint}/" not in spec_paths:
            mismatches.append(
                {
                    "path": endpoint,
                    "resource": resource.name,
                    "spec_path": f"{endpoint} not present in spec",
                }
            )
    return mismatches


def build_report(
    *,
    top: int = 25,
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> dict[str, Any]:
    """Build the coverage report as a JSON-ready dict.

    `top` limits the candidate list (roots ranked by: curated root first, then
    uncovered operation count); `candidates_total` is always the full count.
    """
    spec = load_spec()
    if spec is None:
        return {
            "ok": False,
            "category": "spec",
            "error": "vendored OpenAPI spec unavailable",
        }

    by_kind: dict[str, dict[str, int]] = {
        kind: {"paths": 0, "operations": 0} for kind in PATH_KINDS
    }
    registry_roots = _registry_roots(resources)
    uncovered: dict[str, dict[str, Any]] = {}
    total_paths = total_operations = 0
    first_class_paths = first_class_operations = 0

    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        methods = [m for m in item if m.lower() in HTTP_METHODS]
        if not methods:
            continue
        total_paths += 1
        total_operations += len(methods)

        kind, _ = classify_path(path, resources)
        by_kind[kind]["paths"] += 1
        by_kind[kind]["operations"] += len(methods)

        if kind in ("exact", "by_id"):
            first_class_paths += 1
            first_class_operations += len(methods)
            continue

        root = _root(path)
        if not root:
            continue
        entry = uncovered.setdefault(
            root,
            {
                "root": root,
                "curated_root": root.lower() in registry_roots,
                "operations": 0,
                "get_operations": 0,
                "sample_paths": [],
            },
        )
        entry["operations"] += len(methods)
        entry["get_operations"] += sum(1 for m in methods if m.lower() == "get")
        if len(entry["sample_paths"]) < 3:
            entry["sample_paths"].append(path)

    candidates = sorted(
        uncovered.values(),
        key=lambda c: (-int(c["curated_root"]), -c["operations"], c["root"]),
    )
    for candidate in candidates:
        candidate["related_resources"] = _related_resources(
            candidate["root"], resources
        )
    candidates_total = len(candidates)

    meta = spec_meta() or {}
    return {
        "ok": True,
        "spec_meta": {
            key: meta[key] for key in ("source", "origin", "vendored_at") if key in meta
        },
        "spec": {"paths": total_paths, "operations": total_operations},
        "registry": {"resources": len(resources)},
        "coverage": {
            "first_class_paths": first_class_paths,
            "first_class_operations": first_class_operations,
            "operations_pct": (
                round(100 * first_class_operations / total_operations, 1)
                if total_operations
                else 0.0
            ),
            "by_kind": by_kind,
        },
        "write_mismatches": find_write_mismatches(spec, resources),
        "read_mismatches": find_read_mismatches(spec, resources),
        "candidates_total": candidates_total,
        "candidates": candidates[: max(0, top)],
    }
