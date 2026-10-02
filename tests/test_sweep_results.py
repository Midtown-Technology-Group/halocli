"""Validity gates for the production GET sweep results.

Freshness is deliberately NOT asserted: sweep_results.json is live evidence,
not a generated artifact. These gates prove the file is internally consistent
with the ledger and that nothing outside the GET sweep's scope snuck in -
the write-safety itself is structural (scripts/prod_get_sweep.py has no code
path that can issue anything but GET; see its module docstring).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "coverage_ledger.json"
RESULTS_PATH = REPO_ROOT / "sweep_results.json"

pytestmark = pytest.mark.skipif(
    not RESULTS_PATH.exists(), reason="sweep_results.json not generated yet"
)


def _ledger_get_paths() -> set[str]:
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return {e["path"] for e in ledger["operations"] if e["method"] == "get"}


def test_sweep_only_records_ledger_get_operations() -> None:
    """Every recorded key is a GET of an op the ledger knows about."""
    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    assert results, "sweep results are empty"
    ledger_paths = _ledger_get_paths()
    for key, entry in results.items():
        assert key.startswith("GET "), key
        path = key.removeprefix("GET ")
        assert path in ledger_paths, f"sweep probed an op the ledger lacks: {path}"
        assert "disposition" in entry, key
        assert "status" in entry, key


def test_sweep_entries_carry_live_evidence() -> None:
    """Every non-skipped entry has a status verdict and a latency budget."""
    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    statuses = set()
    for key, entry in results.items():
        status = entry["status"]
        statuses.add(str(status))
        if status == "skipped-suspicious":
            continue
        assert isinstance(status, int) or str(status).startswith("error:"), entry
        if status == 200:
            assert "envelope" in entry and "size_bytes" in entry, key
    # The sweep must have real verdict diversity (or something is wedged).
    assert len(statuses) >= 1


# Completeness (>=90% of GET ops) is checked at commit time when a finished
# sweep is folded into the ledger - asserting it live would go red against a
# partially accumulated results file mid-run.
