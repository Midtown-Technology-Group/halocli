"""Coverage audit: vendored OpenAPI spec vs the curated resource registry.

Answers "which operator-relevant operations has HaloCLI not curated yet?" without
network access (it reads the vendored spec) and catches registry write metadata the
spec does not actually support — the `/Contract` case, where we shipped create/update
endpoints for a path that has no POST in the spec — plus declared `ResourceOperation`s
the spec does not document (`operation_mismatches`).

Path classification against the registry:

* ``exact``     — the registry `list` endpoint (`GET <endpoint>`)
* ``by_id``     — the registry `get` endpoint (`GET <endpoint>/{id}`)
* ``operation`` — declared verbatim as a ``ResourceOperation`` by the owning
                  resource (e.g. ``/Invoice/PDF/{id}`` → ``halo invoices pdf``);
                  first-class through that command
* ``nested``    — deeper than a curated endpoint; curable by extending that resource
* ``uncurated`` — no registry resource owns this path's root segment

An ``operation`` path splits across two counting buckets in ``build_report``: the
PATH counts once under ``by_kind["operation"]["paths"]`` (it is first-class), while
its spec METHODS divide — declared methods go to ``by_kind["operation"]["operations"]``
and any undeclared method goes to ``by_kind["nested"]["operations"]`` (it really is
not first-class). Both partition invariants still hold: by_kind paths sum to the
spec's path count and by_kind operations sum to the spec's operation count.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from halocli.resources import RESOURCES, HaloResource, ResourceOperation
from halocli.schema import load_spec, spec_meta

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
PATH_KINDS = ("exact", "by_id", "operation", "nested", "uncurated")


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


def _strip_trailing_slash(text: str) -> str:
    return text[:-1] if text.endswith("/") else text


def _matching_operations(
    path: str,
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> Iterator[tuple[HaloResource, ResourceOperation]]:
    """Yield (resource, operation) for every declared operation claiming this path.

    String equality against the declared spec path template, tolerating a single
    trailing "/" on either side (mirrors the `exact` endpoint match).
    """
    wanted = _strip_trailing_slash(path)
    for resource in resources:
        for operation in resource.operations:
            if _strip_trailing_slash(operation.path) == wanted:
                yield resource, operation


def classify_path(
    path: str,
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> tuple[str, HaloResource | None]:
    """Classify a spec path against the curated registry.

    Pure registry/string comparison — spec membership is not consulted, so the
    result for a given path is stable across spec refreshes. Declaration order
    matters: `exact`/`by_id` win first (a list/get endpoint is the primary shape),
    then a declared `ResourceOperation`, then the generic `nested` fallback.
    """
    for resource in resources:
        endpoint = _endpoint(resource)
        if path in (endpoint, endpoint + "/"):
            return "exact", resource
        if path == endpoint + "/{id}":
            return "by_id", resource
    for resource, _operation in _matching_operations(path, resources):
        return "operation", resource
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


def find_operation_mismatches(
    spec: dict[str, Any],
    resources: tuple[HaloResource, ...] = RESOURCES,
) -> list[dict[str, Any]]:
    """Return declared `ResourceOperation`s the spec does not support.

    Every declared operation promises ``<method> <path>`` as a first-class command;
    this flags any whose ``path`` is absent from the spec or whose ``method`` is not
    an operation on that path. Sibling of `find_write_mismatches` for the operation
    table: an empty list means all 22 declarations are spec-verified.
    """
    spec_paths = spec.get("paths", {})
    mismatches: list[dict[str, Any]] = []
    for resource in resources:
        for operation in resource.operations:
            path = _strip_trailing_slash(operation.path)
            item = spec_paths.get(path, spec_paths.get(path + "/"))
            available = (
                sorted(m.lower() for m in item if m.lower() in HTTP_METHODS)
                if isinstance(item, dict)
                else []
            )
            if not available:
                problem = f"path not present in spec: {path}"
            elif operation.method.lower() not in available:
                spec_methods = ", ".join(m.upper() for m in available)
                problem = (
                    f"spec has no {operation.method.upper()} on {path} "
                    f"(has: {spec_methods})"
                )
            else:
                continue
            mismatches.append(
                {
                    "resource": resource.name,
                    "operation": operation.name,
                    "method": operation.method.upper(),
                    "path": operation.path,
                    "problem": problem,
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

    The same promise applies to the item route: a resource with ``supports_get``
    exposes `GET {endpoint}/{id}`, so a path without a `get` operation is reported
    too (this is how a registry over-promise like expenses' `get` gets caught —
    Halo has no `/Expense/{id}` at all). Both checks require the `get` *operation*,
    not merely the path: a path carrying only DELETE does not satisfy a registered
    read, which is exactly what spec validation would then refuse.
    """
    spec_paths = spec.get("paths", {})

    def _get_status(path: str) -> str:
        """'', 'missing' (no such path), or 'no-get' (path exists without GET)."""
        item = spec_paths.get(path)
        if not isinstance(item, dict):
            return "missing"
        has_get = any(str(method).lower() == "get" for method in item)
        return "" if has_get else "no-get"

    mismatches: list[dict[str, Any]] = []
    for resource in resources:
        endpoint = _endpoint(resource)
        collection = _get_status(endpoint)
        if collection == "missing":
            collection = _get_status(f"{endpoint}/")  # spec may spell it with a slash
        if collection:
            mismatches.append(
                {
                    "path": endpoint,
                    "resource": resource.name,
                    "spec_path": (
                        f"{endpoint} not present in spec"
                        if collection == "missing"
                        else f"{endpoint} present in spec but has no get operation"
                    ),
                }
            )
        if resource.supports_get:
            item_status = _get_status(f"{endpoint}/{{id}}")
            if item_status:
                mismatches.append(
                    {
                        "path": f"{endpoint}/{{id}}",
                        "resource": resource.name,
                        "spec_path": (
                            f"{endpoint}/{{id}} not present in spec"
                            if item_status == "missing"
                            else f"{endpoint}/{{id}} present in spec but has no get operation"
                        ),
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
    `write_mismatches` and `operation_mismatches` are the spec-vs-registry gates:
    both must be empty for the registry to promise only things the spec supports.
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

        # PATH bucket vs OPERATION bucket: an `operation` path counts as ONE path
        # under by_kind["operation"] (it is first-class via its ResourceOperation),
        # but its spec METHODS split — declared methods count under
        # by_kind["operation"]["operations"] (and first_class), while any method we
        # did NOT declare is genuinely not first-class and goes to
        # by_kind["nested"]["operations"]. That is a deliberate split, not double
        # counting: both partition invariants (by_kind paths == spec paths,
        # by_kind operations == spec operations) still hold exactly.
        candidate_methods = methods
        if kind == "operation":
            declared = {
                op.method.lower()
                for _res, op in _matching_operations(path, resources)
            }
            covered = [m for m in methods if m.lower() in declared]
            candidate_methods = [m for m in methods if m.lower() not in declared]
            by_kind["operation"]["paths"] += 1
            by_kind["operation"]["operations"] += len(covered)
            first_class_paths += 1
            first_class_operations += len(covered)
            if not candidate_methods:
                continue
            by_kind["nested"]["operations"] += len(candidate_methods)
        else:
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
        entry["operations"] += len(candidate_methods)
        entry["get_operations"] += sum(
            1 for m in candidate_methods if m.lower() == "get"
        )
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
        "operation_mismatches": find_operation_mismatches(spec, resources),
        "read_mismatches": find_read_mismatches(spec, resources),
        "candidates_total": candidates_total,
        "candidates": candidates[: max(0, top)],
    }
