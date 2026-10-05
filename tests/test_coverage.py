"""Tests for the coverage oracle (spec vs registry diff)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from halocli import coverage
from halocli.resources import RESOURCES, HaloResource, ResourceOperation

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


def test_classify_declared_operations() -> None:
    # Declared ResourceOperations are first-class (`halo invoices pdf …`).
    kind, resource = coverage.classify_path("/Invoice/PDF/{id}")
    assert kind == "operation"
    assert resource is not None
    assert resource.name == "invoices"

    kind, resource = coverage.classify_path("/Attachment/image")
    assert kind == "operation"
    assert resource is not None
    assert resource.name == "attachments"

    # A single trailing "/" is tolerated on either side of the declaration.
    kind, resource = coverage.classify_path("/Attachment/image/")
    assert kind == "operation"
    assert resource is not None
    assert resource.name == "attachments"


def test_classify_operation_does_not_steal_other_kinds() -> None:
    # {endpoint}/{id} wins over the operation check (checked first).
    kind, resource = coverage.classify_path("/Attachment/{id}")
    assert kind == "by_id"
    assert resource is not None
    assert resource.name == "attachments"

    # Undeclared nested paths stay nested…
    kind, resource = coverage.classify_path("/Tickets/{id}/Actions")
    assert kind == "nested"
    assert resource is not None
    assert resource.name == "tickets"

    # ...and a non-registry root stays uncurated. (ClientCache used to be
    # the example here until field evidence promoted it to a resource.)
    kind, resource = coverage.classify_path("/ChatProfile")
    assert kind == "uncurated"
    assert resource is None


def test_classify_prefix_collision_is_uncurated() -> None:
    # Real spec path: starts with /Address (a registry root) but is a
    # different resource entirely. The trailing-slash guard must keep it
    # out of `nested`-under-address. (ClientCache - the original fixture -
    # was promoted to first-class on dispatch-portal evidence.)
    kind, resource = coverage.classify_path("/Addressbook")
    assert kind == "uncurated"
    assert resource is None

    # /ClientContract is now the contracts endpoint itself (live-verified fix),
    # not a prefix casualty of /Client.
    kind, resource = coverage.classify_path("/ClientContract")
    assert kind == "exact"
    assert resource is not None
    assert resource.name == "contracts"


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


def test_operation_mismatch_detects_bad_path_and_method() -> None:
    spec = {"paths": {"/Known": {"get": {}}, "/Other": {"get": {}}}}
    resources = (
        HaloResource(
            "demo",
            "/Known",
            operations=(
                # Path absent from the spec entirely.
                ResourceOperation("ghost", "post", "/Missing"),
                # Path exists but has no POST.
                ResourceOperation("wrong-method", "post", "/Known"),
                # Declared and spec-verified → no entry.
                ResourceOperation("fine", "get", "/Other"),
            ),
        ),
    )
    mismatches = coverage.find_operation_mismatches(spec, resources)
    by_operation = {m["operation"]: m for m in mismatches}
    assert set(by_operation) == {"ghost", "wrong-method"}

    ghost = by_operation["ghost"]
    assert ghost["resource"] == "demo"
    assert ghost["path"] == "/Missing"
    assert ghost["method"] == "POST"
    assert "not present in spec" in ghost["problem"]

    wrong = by_operation["wrong-method"]
    assert wrong["path"] == "/Known"
    assert wrong["method"] == "POST"
    assert "no POST" in wrong["problem"]


def test_operation_mismatches_empty_for_real_registry() -> None:
    """Every declared ResourceOperation must be spec-verified (path AND method)."""
    from halocli.schema import load_spec

    spec = load_spec()
    assert spec is not None
    assert coverage.find_operation_mismatches(spec, RESOURCES) == []


def test_read_mismatch_detects_undocumented_endpoint() -> None:
    spec = {"paths": {"/Known": {"get": {}}, "/Known/{id}": {"get": {}}}}
    resources = (
        HaloResource("known", "/Known"),
        HaloResource("ghost", "/Ghost"),
    )
    mismatches = coverage.find_read_mismatches(spec, resources)
    assert len(mismatches) == 2  # /Ghost and /Ghost/{id}, both promised by supports_get
    assert mismatches[0]["path"] == "/Ghost"
    assert mismatches[0]["resource"] == "ghost"


def test_read_mismatch_detects_missing_item_route() -> None:
    """A `get` promise requires `{endpoint}/{id}` in the spec, not just the collection.

    This is the expenses class of bug: the collection reads fine but the item
    route does not exist, so the generated `get` subcommand could only 404.
    """
    spec = {"paths": {"/Known": {"get": {}}}}
    resources = (HaloResource("known", "/Known"),)
    mismatches = coverage.find_read_mismatches(spec, resources)
    assert [m["path"] for m in mismatches] == ["/Known/{id}"]
    assert mismatches[0]["resource"] == "known"


def test_read_mismatch_skips_item_route_when_get_unsupported() -> None:
    """supports_get=False means no item route is promised, so none is required."""
    spec = {"paths": {"/Known": {"get": {}}}}
    resources = (HaloResource("known", "/Known", supports_get=False),)
    assert coverage.find_read_mismatches(spec, resources) == []


def test_read_mismatch_flags_item_path_without_get_operation() -> None:
    """Path membership alone is not enough: a DELETE-only item path cannot serve
    the registered `get`, and spec validation would refuse it (CodeRabbit review
    on PR #10). The gate must check the operation, as find_write_mismatches does."""
    spec = {"paths": {"/Known": {"get": {}}, "/Known/{id}": {"delete": {}}}}
    resources = (HaloResource("known", "/Known"),)
    mismatches = coverage.find_read_mismatches(spec, resources)
    assert [m["path"] for m in mismatches] == ["/Known/{id}"]
    assert "no get operation" in mismatches[0]["spec_path"]


def test_read_mismatch_accepts_item_path_with_get_among_other_methods() -> None:
    """A busy item path (get + delete) satisfies the read promise."""
    spec = {"paths": {"/Known": {"get": {}}, "/Known/{id}": {"get": {}, "delete": {}}}}
    resources = (HaloResource("known", "/Known"),)
    assert coverage.find_read_mismatches(spec, resources) == []


def test_read_mismatch_trailing_slash_collection_still_counts() -> None:
    """Some spec spellings use a trailing slash for the collection route."""
    spec = {"paths": {"/Known/": {"get": {}}, "/Known/{id}": {"get": {}}}}
    resources = (HaloResource("known", "/Known"),)
    assert coverage.find_read_mismatches(spec, resources) == []


def test_read_mismatches_empty_after_live_verification() -> None:
    """Every registry read endpoint must be spec-documented.

    Regression gate for the three 404s found by the oracle and verified against
    the live tenant on 2026-09-28: contracts read /Contract (404),
    opportunities read /Opportunity (404), projects read /Project (404 — the
    spec documents /Projects, which returns 200). All three now point at
    spec-documented paths, so this list must stay empty; a spec refresh that
    drops a path we read fails the suite rather than shipping another 404.
    """
    from halocli.schema import load_spec

    spec = load_spec()
    assert spec is not None
    assert coverage.find_read_mismatches(spec, RESOURCES) == []


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
    # first_class == exact + by_id + operation (declared nested commands).
    assert cov["first_class_paths"] == (
        cov["by_kind"]["exact"]["paths"]
        + cov["by_kind"]["by_id"]["paths"]
        + cov["by_kind"]["operation"]["paths"]
    )
    # Same for operations: an operation path's declared methods all count here.
    assert cov["first_class_operations"] == (
        cov["by_kind"]["exact"]["operations"]
        + cov["by_kind"]["by_id"]["operations"]
        + cov["by_kind"]["operation"]["operations"]
    )
    # Registry promises nothing the spec cannot back — all gates empty.
    assert report["write_mismatches"] == []
    assert report["operation_mismatches"] == []

    assert report["candidates_total"] >= len(report["candidates"])
    assert len(report["candidates"]) <= 10
    # Ranked: curated roots before uncurated, then operation count desc.
    flags = [c["curated_root"] for c in report["candidates"]]
    assert flags == sorted(flags, reverse=True)
    for left, right in zip(report["candidates"], report["candidates"][1:]):
        if left["curated_root"] == right["curated_root"]:
            assert left["operations"] >= right["operations"]


def test_build_report_splits_partially_declared_operation(tmp_path, monkeypatch) -> None:
    """Path bucket vs operation bucket: 2 spec methods, 1 declared → split 1 + 1."""
    from halocli import schema

    fake_spec = {
        "paths": {
            "/Part/Thing": {"get": {}, "post": {}, "parameters": []},
            "/Solo": {"get": {}},
        }
    }
    spec_file = tmp_path / "fake_spec.json"
    spec_file.write_text(json.dumps(fake_spec), encoding="utf-8")
    monkeypatch.setattr(schema, "SPEC_PATH", str(spec_file))
    schema.clear_cache()
    resources = (
        HaloResource(
            "part",
            "/Part",
            operations=(ResourceOperation("thing", "get", "/Part/Thing"),),
        ),
    )
    report = coverage.build_report(resources=resources)
    schema.clear_cache()

    cov = report["coverage"]
    # The path is first-class as a whole…
    assert cov["by_kind"]["operation"]["paths"] == 1
    # …but only the declared GET is first-class; the undeclared POST is
    # genuinely not, so it lands in the `nested` *operation* bucket.
    assert cov["by_kind"]["operation"]["operations"] == 1
    assert cov["by_kind"]["nested"]["paths"] == 0
    assert cov["by_kind"]["nested"]["operations"] == 1
    assert cov["first_class_paths"] == 1
    assert cov["first_class_operations"] == 1
    # The declared method is spec-verified → no operation mismatch.
    assert report["operation_mismatches"] == []

    # The split still partitions both totals exactly.
    assert sum(k["paths"] for k in cov["by_kind"].values()) == report["spec"]["paths"]
    assert sum(k["paths"] for k in cov["by_kind"].values()) == 2
    assert (
        sum(k["operations"] for k in cov["by_kind"].values()) == report["spec"]["operations"] == 3
    )


def test_build_report_candidates_have_samples_and_relations() -> None:
    report = coverage.build_report(top=5)
    for candidate in report["candidates"]:
        assert candidate["root"]
        assert candidate["operations"] >= 1
        assert candidate["sample_paths"], candidate["root"]
        assert isinstance(candidate["related_resources"], list)


def test_build_report_candidates_drop_fully_declared_roots() -> None:
    """Roots whose nested paths are all declared operations leave the candidates.

    Tickets (7) and Invoice (5) had every nested spec path declared as a
    ResourceOperation, so they are first-class now and must not show up as
    curation work; `candidates_total` keeps counting the other roots.
    Attachment LEFT the dropped set again on the 2026-10-05 upstream
    re-vendor: dtcdev added POST /Attachment/MigrateToS3 (undeclared) -
    tracked for declaration in issue #84, deliberately not asserted here
    either way.
    """
    report = coverage.build_report(top=50)
    roots = {c["root"] for c in report["candidates"]}
    for root in ("Tickets", "Invoice"):
        assert root not in roots
    assert report["candidates_total"] > 50
    assert len(report["candidates"]) == 50


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
