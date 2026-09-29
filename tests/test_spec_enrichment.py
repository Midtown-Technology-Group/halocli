"""Spec enrichment contract: operationIds + curated overlay for surfaced operations.

The vendored Halo spec ships almost no operationIds (3/1455) and leaves many
summaries/descriptions empty, which breaks typed consumers (Forge, Fern,
OpenAPI tooling — measured: Forge resolved 3/1455 before enrichment). Two
enrichment layers make the spec generable:

* ``scripts/vendor_halo_spec.py`` synthesizes every missing ``operationId``
  deterministically as ``{method}_{path}`` (upstream IDs are never overwritten);
* ``src/halocli/spec/halo_overlay.json`` (OpenAPI Overlay 1.0.0) fills
  summary/description prose for the operations HaloCLI surfaces, applied with
  fill-if-missing semantics.

This file is the enforcement point: if the resource registry grows and the
overlay is not extended, ``test_surfaced_operations_have_prose`` fails.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from halocli import schema
from halocli.resources import RESOURCES

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_PATH = REPO_ROOT / "src" / "halocli" / "spec" / "halo_overlay.json"
VENDOR_SCRIPT = REPO_ROOT / "scripts" / "vendor_halo_spec.py"


def _load_vendor_module() -> Any:
    spec = importlib.util.spec_from_file_location("vendor_halo_spec", VENDOR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["vendor_halo_spec"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vendor_mod() -> Any:
    return _load_vendor_module()


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded = schema.load_spec()
    assert loaded is not None
    return loaded


def surfaced_pairs() -> list[tuple[str, str]]:
    """Every (path, method) reachable from the resource registry."""
    pairs: list[tuple[str, str]] = []
    for resource in RESOURCES:
        pairs.append((resource.endpoint, "get"))
        pairs.append((f"{resource.endpoint}/{{id}}", "get"))
        if resource.create_endpoint:
            pairs.append((resource.create_endpoint, "post"))
        if resource.update_endpoint and resource.update_endpoint != resource.create_endpoint:
            pairs.append((resource.update_endpoint, "post"))
        if resource.supports_delete:
            pairs.append((f"{resource.endpoint}/{{id}}", "delete"))
        for op in resource.operations:
            pairs.append((op.path, op.method.lower()))
    return list(dict.fromkeys(pairs))


# Mirror of schema._HTTP_METHODS (private there); the vendor script uses the
# same set plus ``trace`` — anything HTTP-shaped counts as an operation.
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method in _METHODS and isinstance(op, dict):
                out.append((method, path, op))
    return out


def test_every_operation_has_a_unique_operation_id(spec: dict[str, Any]) -> None:
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for method, path, op in _operations(spec):
        operation_id = op.get("operationId")
        if not operation_id:
            missing.append((method, path))
        elif operation_id in seen:
            pytest.fail(f"duplicate operationId {operation_id!r} for {method.upper()} {path}")
        else:
            seen.add(operation_id)
    assert not missing, f"operations missing operationId: {missing[:10]}"


def test_synthesized_operation_ids_follow_the_rule(spec: dict[str, Any]) -> None:
    """The rule is ``{method}_{path}`` sanitized to a lower snake identifier."""
    import re

    upstream = {
        op.get("operationId")
        for _method, _path, op in _operations(spec)
        if op.get("operationId") in {"GetIntegrationCursor", "GetSeatGeekDetails", "GetUnamePresenceSubscription"}
    }
    assert upstream == {"GetIntegrationCursor", "GetSeatGeekDetails", "GetUnamePresenceSubscription"}

    checked = 0
    for method, path, op in _operations(spec):
        operation_id = op["operationId"]
        if operation_id in upstream:
            continue
        assert re.fullmatch(r"[a-z0-9_]+", operation_id), f"odd chars in {operation_id!r}"
        assert operation_id.startswith(f"{method}_"), f"{operation_id} does not start with {method}_"
        checked += 1
    assert checked > 1400  # nearly every operation is synthesized


def test_surfaced_operations_have_prose(spec: dict[str, Any]) -> None:
    """Registry growth must be accompanied by overlay prose.

    Pairs absent from the spec entirely are tolerated only if the endpoint
    itself is also absent (``read_mismatches`` owns that invariant); the one
    known case is the ``{endpoint}/{id}`` get for expenses, which the Halo spec
    does not document.
    """
    absent_ok = {("/Expense/{id}", "get")}
    incomplete: list[str] = []
    for path, method in surfaced_pairs():
        item = spec["paths"].get(path)
        op = item.get(method) if isinstance(item, dict) else None
        if not isinstance(op, dict):
            if (path, method) not in absent_ok:
                incomplete.append(f"{method.upper()} {path} (missing from spec)")
            continue
        for field in ("operationId", "summary", "description"):
            if not str(op.get(field) or "").strip():
                incomplete.append(f"{method.upper()} {path} (no {field})")
    assert not incomplete, (
        "surfaced operations missing enrichment — extend src/halocli/spec/halo_overlay.json: "
        f"{incomplete}"
    )


def test_overlay_file_shape() -> None:
    overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    assert overlay["overlay"] in ("1.0.0", "1.1.0")
    assert overlay["info"]["title"]
    actions = overlay["actions"]
    assert isinstance(actions, list) and actions, "overlay must carry actions"
    for action in actions:
        assert action["target"].startswith("$.paths["), action["target"]
        assert action["target"].endswith((".summary", ".description")), action["target"]
        assert isinstance(action["update"], str) and action["update"].strip()


def test_overlay_targets_exist_in_spec(spec: dict[str, Any]) -> None:
    """A stale target (path renamed upstream) must fail loudly, not no-op."""
    overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    for action in overlay["actions"]:
        target = action["target"]
        # $.paths['/X'].get.summary
        path = target.split("['", 1)[1].split("']", 1)[0]
        method = target.rsplit(".", 2)[-2]
        item = spec["paths"].get(path)
        assert isinstance(item, dict), f"overlay target path not in spec: {path}"
        assert method in item, f"overlay target method not in spec: {method} {path}"


def test_apply_overlay_fill_if_missing(vendor_mod: Any) -> None:
    """Overlay text lands in empty fields and never overwrites existing prose."""
    target = "$.paths['/Tickets'].get.summary"
    doc = {
        "overlay": "1.0.0",
        "info": {"title": "t"},
        "actions": [
            {"target": target, "update": "curated summary"},
            {"target": "$.paths['/Client'].get.summary", "update": "should be skipped"},
        ],
    }
    mini = {
        "paths": {
            "/Tickets": {"get": {"summary": ""}},
            "/Client": {"get": {"summary": "List of Client"}},
        }
    }
    filled, already = vendor_mod.apply_overlay(mini, doc)
    assert filled == 1
    assert already == 1
    assert mini["paths"]["/Tickets"]["get"]["summary"] == "curated summary"
    assert mini["paths"]["/Client"]["get"]["summary"] == "List of Client"


def test_apply_overlay_rejects_bad_targets(vendor_mod: Any) -> None:
    mini = {"paths": {"/Tickets": {"get": {}}}}
    with pytest.raises(ValueError, match="unsupported overlay target"):
        vendor_mod.apply_overlay(mini, {"overlay": "1.0.0", "actions": [{"target": "$.components", "update": "x"}]})
    with pytest.raises(ValueError, match="not in spec"):
        vendor_mod.apply_overlay(mini, {"overlay": "1.0.0", "actions": [{"target": "$.paths['/Nope'].get.summary", "update": "x"}]})
    with pytest.raises(ValueError, match="overlay version"):
        vendor_mod.apply_overlay(mini, {"overlay": "9.9.9", "actions": []})


def test_synthesize_operation_ids_rule(vendor_mod: Any) -> None:
    mini = {
        "paths": {
            "/Tickets": {"get": {"summary": "s"}, "post": {"operationId": "UpstreamId", "summary": "s"}},
            "/Invoice/PDF/{id}": {"post": {"summary": "s"}},
        }
    }
    synthesized, upstream = vendor_mod.synthesize_operation_ids(mini)
    assert upstream == 1
    assert synthesized == 2
    assert mini["paths"]["/Tickets"]["get"]["operationId"] == "get_tickets"
    assert mini["paths"]["/Invoice/PDF/{id}"]["post"]["operationId"] == "post_invoice_pdf_id"
    # upstream id untouched
    assert mini["paths"]["/Tickets"]["post"]["operationId"] == "UpstreamId"
    # idempotent: second run adds nothing
    again, _ = vendor_mod.synthesize_operation_ids(mini)
    assert again == 0


def test_spec_meta_records_enrichment(spec: dict[str, Any]) -> None:
    meta = spec["_meta"]
    assert meta["operation_ids_synthesized"] >= 1452
    assert meta["operation_ids_upstream"] == 3
    overlay_meta = meta["overlay"]
    assert overlay_meta["file"] == "halo_overlay.json"
    # fill-if-missing: an action targeting prose upstream now provides lands in
    # already_present, so the sum (not filled alone) must account for all actions.
    assert overlay_meta["filled"] + overlay_meta["already_present"] == overlay_meta["actions"]
    assert meta["vendor_script"] == "scripts/vendor_halo_spec.py"


def test_search_surfaces_enriched_summaries() -> None:
    """The enrichment must be visible to the operator surface (halocli search)."""
    results = schema.search_operations("void")
    assert results, "expected void operation in search results"
    top = results[0]
    assert top["path"] == "/Invoice/{id}/void"
    assert top["summary"]
    assert top["operationId"]
