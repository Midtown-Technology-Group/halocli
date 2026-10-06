"""Pure tests for scripts/openapi_methods.py (spec -> Halo method rows)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "openapi_methods", REPO / "scripts" / "openapi_methods.py"
)
assert _spec is not None and _spec.loader is not None
om = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(om)

OAS3 = {
    "openapi": "3.0.0",
    "info": {"title": "Demo API", "version": "2.1"},
    "servers": [{"url": "https://api.demo.example/v2/"}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/widgets": {
            "get": {"operationId": "listWidgets"},
            "post": {"operationId": "createWidget"},
            "head": {"operationId": "headWidgets"},
        },
        "/widgets/{id}": {
            "get": {"operationId": "getWidget"},
            "delete": {},
        },
    },
}

SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy", "version": "1"},
    "host": "api.legacy.example",
    "basePath": "/api",
    "schemes": ["https"],
    "paths": {"/ping": {"get": {"parameters": [{"name": "q", "in": "query"}]}}},
}


def test_rows_shape_and_counts() -> None:
    rows, stats = om.spec_to_methods(OAS3)
    assert stats["operations"] == 5
    names = [r["name"] for r in rows]
    assert "listWidgets" in names and "createWidget" in names and "getWidget" in names
    # HEAD skipped (Halo verb enum has no HEAD)
    assert "headWidgets" not in names
    assert stats["skipped_verb"] == 1
    assert stats["unsupported_verbs"] == ["HEAD"]
    # delete with no operationId -> synthesized + templated path kept
    # (templated counts OPERATIONS: get + delete on /widgets/{id})
    assert any(r["path"] == "/widgets/{id}" and r["method"] == "DELETE" for r in rows)
    assert stats["synthesized_names"] == 1
    assert stats["templated"] == 2
    for r in rows:
        assert set(r) >= {"name", "path", "method"}
        assert r["method"] in ("GET", "POST", "PUT", "DELETE", "PATCH")


def test_base_url_both_styles() -> None:
    assert om.spec_base_url(OAS3) == "https://api.demo.example/v2"
    assert om.spec_base_url(SWAGGER2) == "https://api.legacy.example/api"


def test_skip_templated_and_filters() -> None:
    rows, stats = om.spec_to_methods(OAS3, skip_templated=True)
    assert all("{" not in r["path"] for r in rows)
    assert stats["skipped_templated"] == 2
    rows, stats = om.spec_to_methods(OAS3, include=r"^GET /widgets$")
    assert [r["name"] for r in rows] == ["listWidgets"]
    rows, stats = om.spec_to_methods(OAS3, exclude="widget")
    assert rows == []
    assert stats["skipped_filter"] == 5


def test_query_param_caveat_counted() -> None:
    _rows, stats = om.spec_to_methods(SWAGGER2)
    assert stats["with_query_params"] == 1


def test_duplicate_names_deduped() -> None:
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/a": {"get": {"operationId": "same"}},
            "/b": {"get": {"operationId": "same"}},
        },
    }
    rows, stats = om.spec_to_methods(spec)
    names = [r["name"] for r in rows]
    assert len(set(names)) == 2  # second becomes same_get
    assert stats["duplicate_names"] == ["same"]


def test_bind_map_sets_phase() -> None:
    rows, _stats = om.spec_to_methods(OAS3, bind_map={"fetch_widgets": "listWidgets"})
    row = next(r for r in rows if r["name"] == "listWidgets")
    assert row["bind_phase"] == "fetch_widgets"
    assert "bind_phase" not in next(r for r in rows if r["name"] == "createWidget")


def test_security_schemes_reported() -> None:
    _rows, stats = om.spec_to_methods(OAS3)
    assert stats["security_schemes"] == ["bearer"]


def test_integration_yaml_shape() -> None:
    import yaml

    doc = yaml.safe_load(om.integration_yaml("Vendor X", "https://api.x.example"))
    entry = next(iter(doc["integrations"].values()))
    assert entry["name"] == "Vendor X"
    assert entry["config_schema"][0]["key"] == "base_url"
    assert "api.x.example" in entry["config_schema"][0]["description"]


def test_load_spec_rejects_non_openapi(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"paths": {}}), encoding="utf-8")
    try:
        om.load_spec(p)
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "not an OpenAPI" in str(e)


def test_load_spec_yaml(tmp_path: Path) -> None:
    import yaml

    p = tmp_path / "spec.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.0",
                "info": {"title": "Y", "version": "1"},
                "paths": {"/x": {"get": {"operationId": "getX"}}},
            }
        ),
        encoding="utf-8",
    )
    doc = om.load_spec(p)
    assert "/x" in doc["paths"]


def test_main_cli_end_to_end(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    import yaml

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(OAS3), encoding="utf-8")
    out = tmp_path / "methods.yaml"
    integration = tmp_path / "integration.yaml"
    bindmap = tmp_path / "binds.json"
    bindmap.write_text(json.dumps({"fetch_widgets": "listWidgets"}), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "openapi_methods.py",
            str(spec_path),
            "--out",
            str(out),
            "--bind-map",
            str(bindmap),
            "--skip-templated",
            "--emit-integration",
            "Vendor Demo",
            "--integration-out",
            str(integration),
        ],
    )
    assert om.main() == 0
    rows = yaml.safe_load(out.read_text(encoding="utf-8"))["methods"]
    names = [r["name"] for r in rows]
    assert "listWidgets" in names and "getWidget" not in names  # templated skipped
    bound = next(r for r in rows if r["name"] == "listWidgets")
    assert bound["bind_phase"] == "fetch_widgets"
    int_doc = yaml.safe_load(integration.read_text(encoding="utf-8"))
    entry = next(iter(int_doc["integrations"].values()))
    assert entry["name"] == "Vendor Demo"
    printed = capsys.readouterr().out
    assert "emitted" in printed and "wrote" in printed
    assert "auth: schemes" in printed


def test_main_requires_integration_out(tmp_path: Path, monkeypatch: Any) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(OAS3), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["openapi_methods.py", str(spec_path), "--emit-integration", "X"],
    )
    try:
        om.main()
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "--integration-out" in str(e)
