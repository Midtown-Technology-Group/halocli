"""Tests for the coverage oracle (spec vs registry diff)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from halocli import coverage
from halocli.resources import RESOURCES, HaloResource

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_classify_exact_and_by_id() -> None:
    kind, resource = coverage.classify_path("/Tickets")
    assert kind == "exact"
    assert resource is not None
    assert resource.name == "tickets"

    kind, resource = coverage.classify_path("/Tickets/{id}")
    assert kind == "by_id"
    assert resource is not None
    assert resource.name == "tickets"


def test_classify_nested_under_curated_endpoint() -> None:
    # Deeper than /Tickets → curable by extending the tickets resource.
    kind, resource = coverage.classify_path("/Tickets/{id}/Actions")
    assert kind == "nested"
    assert resource is not None
    assert resource.name == "tickets"


def test_classify_prefix_collision_is_uncurated() -> None:
    # /Client must NOT swallow /ClientContract-style paths (prefix trap).
    kind, resource = coverage.classify_path("/ClientContract")
    assert kind == "uncurated"
    assert resource is None


def test_classify_unknown_root_is_uncurated() -> None:
    kind, resource = coverage.classify_path("/SatisfactionScore/{id}")
    assert kind == "uncurated"
    assert resource is None


def test_iter_operations_counts_methods() -> None:
    spec = {
        "paths": {
            "/A": {"get": {}, "post": {}, "parameters": []},
            "/B": {"parameters": []},
        }
    }
    ops = sorted(coverage._iter_operations(spec))
    assert ops == [("GET", "/A"), ("POST", "/A")]


def test_write_mismatch_detects_missing_post() -> None:
    spec = {"paths": {"/ClientContract": {"post": {}}, "/NoDelete/{id}": {"get": {}}}}
    resources = (
        HaloResource(
            "ccs",
            "/ClientContract",
            create_endpoint="/ClientContract",
            update_endpoint="/NoDelete",  # no POST in spec → mismatch
            supports_delete=True,  # no /NoDelete/{id} DELETE → mismatch
        ),
    )
    mismatches = coverage.find_write_mismatches(spec, resources)
    paths = {m["path"]: m for m in mismatches}
    assert "/NoDelete" in paths
    assert paths["/NoDelete"]["method"] == "POST"
    assert "ccs.update_endpoint" in paths["/NoDelete"]["declared_by"]
    # supports_delete checks DELETE {resource.endpoint}/{id} (what writes.py calls).
    assert "/ClientContract/{id}" in paths
    assert paths["/ClientContract/{id}"]["method"] == "DELETE"
    assert "ccs.supports_delete" in paths["/ClientContract/{id}"]["declared_by"]
    # /ClientContract has POST → no mismatch entry for it.
    assert "/ClientContract" not in paths


def test_write_mismatches_empty_for_real_registry() -> None:
    """Regression gate for the /Contract bug: every declared write must exist."""
    from halocli.schema import load_spec

    spec = load_spec()
    assert spec is not None
    assert coverage.find_write_mismatches(spec, RESOURCES) == []


def test_read_mismatch_detects_undocumented_endpoint() -> None:
    spec = {"paths": {"/Known": {"get": {}}}}
    resources = (
        HaloResource("known", "/Known"),
        HaloResource("ghost", "/Ghost"),
    )
    mismatches = coverage.find_read_mismatches(spec, resources)
    assert len(mismatches) == 1
    assert mismatches[0]["path"] == "/Ghost"
    assert mismatches[0]["resource"] == "ghost"


def test_read_mismatch_flags_contract_on_real_spec() -> None:
    """Registry endpoints the spec does not document (oracle findings).

    These are the current, verified state of the registry-vs-spec diff:
    - contracts reads /Contract (spec has only ClientContract/SupplierContract)
    - opportunities reads /Opportunity (zero opportunity paths in the spec)
    - projects reads /Project (spec documents /Projects, plural)
    Runtime verification of these three is a follow-up; the oracle's job is to flag.
    """
    from halocli.schema import load_spec

    spec = load_spec()
    assert spec is not None
    mismatches = coverage.find_read_mismatches(spec, RESOURCES)
    flagged = {m["resource"] for m in mismatches}
    assert flagged == {"contracts", "opportunities", "projects"}


def test_build_report_structure_on_real_spec() -> None:
    report = coverage.build_report(top=10)
    assert report["ok"] is True

    spec = report["spec"]
    cov = report["coverage"]
    # Kinds partition the spec's paths exactly.
    kind_paths = sum(k["paths"] for k in cov["by_kind"].values())
    kind_ops = sum(k["operations"] for k in cov["by_kind"].values())
    assert kind_paths == spec["paths"]
    assert kind_ops == spec["operations"]
    assert cov["first_class_paths"] >= len(RESOURCES)
    assert 0.0 <= cov["operations_pct"] <= 100.0
    # first_class == exact + by_id
    assert cov["first_class_paths"] == (
        cov["by_kind"]["exact"]["paths"] + cov["by_kind"]["by_id"]["paths"]
    )

    assert report["candidates_total"] >= len(report["candidates"])
    assert len(report["candidates"]) <= 10
    # Ranked: curated roots before uncurated, then operation count desc.
    flags = [c["curated_root"] for c in report["candidates"]]
    assert flags == sorted(flags, reverse=True)
    for left, right in zip(report["candidates"], report["candidates"][1:]):
        if left["curated_root"] == right["curated_root"]:
            assert left["operations"] >= right["operations"]


def test_build_report_candidates_have_samples_and_relations() -> None:
    report = coverage.build_report(top=5)
    for candidate in report["candidates"]:
        assert candidate["root"]
        assert candidate["operations"] >= 1
        assert candidate["sample_paths"], candidate["root"]
        assert isinstance(candidate["related_resources"], list)


def test_build_report_degrades_without_spec(tmp_path, monkeypatch) -> None:
    from halocli import schema

    monkeypatch.setattr(schema, "SPEC_PATH", str(tmp_path / "missing.json"))
    schema.clear_cache()
    report = coverage.build_report()
    assert report["ok"] is False
    assert report["category"] == "spec"
    schema.clear_cache()


def test_script_runs_and_prints_valid_json() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "coverage_report.py"), "--top", "3"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert len(payload["candidates"]) <= 3


def test_script_check_passes_on_real_registry() -> None:
    """--check is the CI gate: it must pass while the registry is spec-clean."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "coverage_report.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
