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
        if resource.supports_get:
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
        if op.get("operationId")
        in {"GetIntegrationCursor", "GetSeatGeekDetails", "GetUnamePresenceSubscription"}
    }
    assert upstream == {
        "GetIntegrationCursor",
        "GetSeatGeekDetails",
        "GetUnamePresenceSubscription",
    }

    checked = 0
    for method, path, op in _operations(spec):
        operation_id = op["operationId"]
        if operation_id in upstream:
            continue
        assert re.fullmatch(r"[a-z0-9_]+", operation_id), f"odd chars in {operation_id!r}"
        assert operation_id.startswith(f"{method}_"), (
            f"{operation_id} does not start with {method}_"
        )
        checked += 1
    assert checked > 1400  # nearly every operation is synthesized


def test_surfaced_operations_have_prose(spec: dict[str, Any]) -> None:
    """Registry growth must be accompanied by overlay prose.

    Every pair here is a route the registry promises (``supports_get`` gates the
    item route), so each must exist in the spec with id + summary + description —
    no exceptions. A missing pair means either the registry over-promises (fix
    the flag) or the overlay is stale (extend it).
    """
    incomplete: list[str] = []
    for path, method in surfaced_pairs():
        item = spec["paths"].get(path)
        op = item.get(method) if isinstance(item, dict) else None
        if not isinstance(op, dict):
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
        target = action["target"]
        # Supported shapes: $.paths[...].<method>.summary|description,
        # $.paths[...].<method>.parameters[?@.name=='x'].description, and
        # $.components.schemas.X.properties.y.description (all standard JSONPath;
        # test_overlay_targets_are_standard_jsonpath proves it).
        on_path = target.startswith("$.paths[") and target.endswith((".summary", ".description"))
        on_schema = target.startswith("$.components.schemas.") and target.endswith(".description")
        assert on_path or on_schema, f"unrecognised overlay target: {target}"
        assert isinstance(action["update"], str) and action["update"].strip()


def test_overlay_targets_exist_in_spec(spec: dict[str, Any], vendor_mod: Any) -> None:
    """A stale target (path, parameter or property renamed upstream) must fail
    loudly rather than silently no-op.

    Resolution is non-mutating on purpose: schema.load_spec() caches the spec
    process-wide, so applying the overlay here would leak into later tests.
    """
    overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    for action in overlay["actions"]:
        container, field = vendor_mod.resolve_overlay_target(spec, action["target"])
        assert isinstance(container, dict), action["target"]
        assert field in ("summary", "description"), action["target"]


def test_overlay_targets_are_standard_jsonpath(spec: dict[str, Any]) -> None:
    """Prove the overlay's portability claim instead of asserting it in a comment.

    vendor_halo_spec.py promises every target is valid JSONPath so external
    runners (Forge et al) can apply the same file. That claim was false for
    the parameter shape: ``parameters['loadreport']`` parses under jsonpath-ng
    but resolves to ZERO nodes, so an external runner would silently skip that
    action while applying the rest -- the worst failure mode, because the run
    still looks successful. Caught by CodeRabbit on PR #16.

    jsonpath-ng is imported directly rather than via importorskip: dropping the
    dev dependency must fail this test loudly, not skip it.
    """
    from jsonpath_ng.ext import parse

    overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    for action in overlay["actions"]:
        target = action["target"]
        matches = parse(target).find(spec)
        assert len(matches) == 1, (
            f"target {target!r} resolved to {len(matches)} nodes under standard "
            "JSONPath; an external overlay runner would skip or mis-apply it"
        )
        assert matches[0].value == action["update"], target


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
        vendor_mod.apply_overlay(
            mini, {"overlay": "1.0.0", "actions": [{"target": "$.components", "update": "x"}]}
        )
    with pytest.raises(ValueError, match="not in spec"):
        vendor_mod.apply_overlay(
            mini,
            {
                "overlay": "1.0.0",
                "actions": [{"target": "$.paths['/Nope'].get.summary", "update": "x"}],
            },
        )
    with pytest.raises(ValueError, match="overlay version"):
        vendor_mod.apply_overlay(mini, {"overlay": "9.9.9", "actions": []})


def test_apply_overlay_fills_parameter_and_property_descriptions(vendor_mod: Any) -> None:
    """The two shapes added for the report-interface findings: a query parameter
    on an operation, and a property inside a component schema."""
    mini = {
        "paths": {
            "/Report/{id}": {
                "get": {
                    "parameters": [
                        {"name": "loadreport", "in": "query", "schema": {"type": "boolean"}},
                        {
                            "name": "includedetails",
                            "in": "query",
                            "description": "upstream text",
                            "schema": {"type": "boolean"},
                        },
                    ]
                }
            }
        },
        "components": {
            "schemas": {
                "AnalyzerProfile": {
                    "properties": {
                        "sql": {"type": "string", "nullable": True},
                        "name": {"type": "string", "description": "upstream text"},
                    }
                }
            }
        },
    }
    doc = {
        "overlay": "1.0.0",
        "info": {"title": "t"},
        "actions": [
            {
                "target": "$.paths['/Report/{id}'].get.parameters[?@.name=='loadreport'].description",
                "update": "execute the report",
            },
            {
                "target": "$.paths['/Report/{id}'].get.parameters[?@.name=='includedetails'].description",
                "update": "must not overwrite upstream",
            },
            {
                "target": "$.components.schemas.AnalyzerProfile.properties.sql.description",
                "update": "raw T-SQL",
            },
            {
                "target": "$.components.schemas.AnalyzerProfile.properties.name.description",
                "update": "must not overwrite upstream",
            },
        ],
    }

    filled, already = vendor_mod.apply_overlay(mini, doc)

    assert filled == 2
    assert already == 2  # fill-if-missing applies to the new shapes too
    params = {p["name"]: p for p in mini["paths"]["/Report/{id}"]["get"]["parameters"]}
    assert params["loadreport"]["description"] == "execute the report"
    assert params["includedetails"]["description"] == "upstream text"
    props = mini["components"]["schemas"]["AnalyzerProfile"]["properties"]
    assert props["sql"]["description"] == "raw T-SQL"
    assert props["name"]["description"] == "upstream text"


def test_apply_overlay_rejects_unknown_parameter_and_property(vendor_mod: Any) -> None:
    """A parameter or property that no longer exists must fail loudly."""
    mini = {
        "paths": {"/Report/{id}": {"get": {"parameters": [{"name": "loadreport"}]}}},
        "components": {"schemas": {"AnalyzerProfile": {"properties": {"sql": {}}}}},
    }
    with pytest.raises(ValueError, match="parameter not in spec"):
        vendor_mod.apply_overlay(
            mini,
            {
                "overlay": "1.0.0",
                "actions": [
                    {
                        "target": "$.paths['/Report/{id}'].get.parameters[?@.name=='gone'].description",
                        "update": "x",
                    }
                ],
            },
        )
    with pytest.raises(ValueError, match="schema property not in spec"):
        vendor_mod.apply_overlay(
            mini,
            {
                "overlay": "1.0.0",
                "actions": [
                    {
                        "target": "$.components.schemas.AnalyzerProfile.properties.gone.description",
                        "update": "x",
                    }
                ],
            },
        )
    with pytest.raises(ValueError, match="schema property not in spec"):
        vendor_mod.apply_overlay(
            mini,
            {
                "overlay": "1.0.0",
                "actions": [
                    {
                        "target": "$.components.schemas.Missing.properties.x.description",
                        "update": "x",
                    }
                ],
            },
        )


def test_report_interface_prose_documented(spec: dict[str, Any]) -> None:
    """Prose learned by live-testing the report interface must stay in the spec.

    Each item cost a real round-trip against the tenant to discover: the array
    body and its two failure modes (400 object, 415 content type), loadreport
    being the execution path with a 50k row cap, and Halo wrapping report SQL
    in a derived table (so a trailing ORDER BY needs TOP/OFFSET).
    """
    post = spec["paths"]["/Report"]["post"]
    summary = post.get("summary") or ""
    description = post.get("description") or ""
    assert "array" in summary.lower(), "POST /Report summary should mention the array body"
    assert "JSON array" in description and "400" in description and "415" in description

    get_one = spec["paths"]["/Report/{id}"]["get"]
    loadreport = next(
        (p for p in get_one.get("parameters", []) if p.get("name") == "loadreport"),
        None,
    )
    assert loadreport is not None, "loadreport query parameter missing from spec"
    loadreport_desc = loadreport.get("description") or ""
    assert "report.rows" in loadreport_desc and "50,000" in loadreport_desc

    sql = spec["components"]["schemas"]["AnalyzerProfile"]["properties"]["sql"]
    sql_desc = sql.get("description") or ""
    assert "derived table" in sql_desc and "ORDER BY" in sql_desc

    # The array schema itself must stay intact -- the prose documents it, it
    # does not replace it.
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["type"] == "array"
    assert schema["items"]["$ref"] == "#/components/schemas/AnalyzerProfile"


def test_synthesize_operation_ids_rule(vendor_mod: Any) -> None:
    mini = {
        "paths": {
            "/Tickets": {
                "get": {"summary": "s"},
                "post": {"operationId": "UpstreamId", "summary": "s"},
            },
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
