"""Gates for the coverage ledger (every spec operation explicitly classified).

The ledger is the enforcement spine of the endpoint-promotion effort: if a
spec re-vendor (or a genuinely new endpoint family) introduces segments the
human policy hasn't seen, generation emits UNCLASSIFIED notes and these tests
fail until coverage_policy.json is updated - completeness stops being aspirational.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from halocli import schema
from halocli.resources import RESOURCES

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "coverage_ledger.json"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_coverage_ledger.py"

DISPOSITIONS = {"first-class", "backlog", "deliberately-raw", "dormant", "junk"}
REASON_REQUIRED = {"deliberately-raw", "dormant", "junk"}

pytestmark = pytest.mark.skipif(
    not LEDGER_PATH.exists(), reason="coverage_ledger.json not generated"
)


@pytest.fixture(scope="module")
def ledger() -> dict:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def spec() -> dict:
    loaded = schema.load_spec()
    assert loaded is not None
    return loaded


def test_ledger_is_fresh() -> None:
    """The committed ledger must match a fresh generation (spec/policy/registry)."""
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ledger_covers_every_spec_operation(ledger: dict, spec: dict) -> None:
    """Set equality: every (path, method) in the spec, exactly once."""
    spec_ops = set()
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                spec_ops.add((path, method))

    ledger_ops = [(e["path"], e["method"]) for e in ledger["operations"]]
    assert len(ledger_ops) == len(set(ledger_ops)), "duplicate ledger entries"
    assert set(ledger_ops) == spec_ops, (
        f"missing={sorted(spec_ops - set(ledger_ops))[:10]} "
        f"stale={sorted(set(ledger_ops) - spec_ops)[:10]}"
    )
    assert ledger["_meta"]["operation_count"] == len(spec_ops)


def test_ledger_dispositions_are_sound(ledger: dict) -> None:
    """Enum validity, required reasons, first-class via mapping, no unclassified."""
    counts: dict[str, int] = {}
    for entry in ledger["operations"]:
        disp = entry["disposition"]
        assert disp in DISPOSITIONS, entry
        counts[disp] = counts.get(disp, 0) + 1

        assert "UNCLASSIFIED" not in entry["note"], entry
        if disp in REASON_REQUIRED:
            assert entry["note"].strip(), f"{disp} entry without a reason: {entry}"
        if disp == "first-class":
            assert entry["via"].startswith("halocli "), entry
        else:
            assert entry["via"] == "", entry

    assert counts == ledger["_meta"]["disposition_counts"]
    assert sum(counts.values()) == ledger["_meta"]["operation_count"]


def test_first_class_via_maps_to_real_commands(ledger: dict) -> None:
    """Every 'via' command must name a resource or operation that exists."""
    resource_names = {r.name for r in RESOURCES}
    operation_keys = {
        f"{r.name} {op.name}" for r in RESOURCES for op in r.operations
    }
    verbs = {"list", "get", "create", "update", "delete", "create/update"}
    for entry in ledger["operations"]:
        if entry["disposition"] != "first-class":
            continue
        tail = entry["via"].removeprefix("halocli ").split(" ", 1)
        assert len(tail) == 2, entry
        name, verb = tail
        assert name in resource_names, entry
        assert verb in verbs or f"{name} {verb}" in operation_keys, entry


def test_sweep_evidence_flips_are_in_the_ledger(ledger: dict) -> None:
    """The GET sweep's disposition flips are pinned as regression facts.

    Evidence: sweep_results.json (2026-10-02) - these routes answered 404/500
    at collection level while their segments stayed healthy.

    Exception: /Holiday/{id} was initially flipped dormant (detail probe
    crashed with 500) but its COLLECTION is live with 492 rows, so phase-2
    batch 2 promoted the segment - the detail quirk is documented in the
    resource comment instead of demoting the whole entity.
    """
    by_key = {(e["method"], e["path"]): e for e in ledger["operations"]}
    for path in (
        "/Users/me",
        "/Users/onbehalf",
        "/Appointment/Booking",
        "/DashboardLinks/FilterValues",
        "/Feedback/FeedbackMessage",
        "/TaskMonitorEvent",
    ):
        entry = by_key[("get", path)]
        assert entry["disposition"] == "dormant", entry
        assert "live" in entry["note"] or "sweep" in entry["note"], entry
    # enrichments keep their disposition but carry live evidence
    assert by_key[("get", "/Timesheet/mine")]["disposition"] == "backlog"
    assert "live403" in by_key[("get", "/Timesheet/mine")]["note"]
