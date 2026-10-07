"""Drive scripts/criteria_labels_probe.py (the criteria sweep) with fakes.

The sweep is read-only (its live result -0 membership-style criteria in
20 trial runbooks - is cited in membership_evidence.json's probe
docstring); these tests pin the classifier and the sweep loop offline.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "criteria_sweep", REPO / "scripts" / "criteria_labels_probe.py"
)
assert _spec is not None
assert _spec.loader is not None
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def test_interesting_classifier() -> None:
    known = {"type": 0, "value_type": "string"}
    assert sweep.interesting(dict(known)) is False
    assert sweep.interesting({"type": 23}) is True  # membership op
    assert sweep.interesting({"type": 0, "value_type": "multiselect"}) is True
    assert sweep.interesting({"type": 0, "value_lookup": ["a"]}) is True
    assert sweep.interesting({"type": 0, "value_int": [1, 2]}) is True


class _FakeHalo:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeReq":
        return _FakeReq()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeReq:
    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert method == "GET"
        if path == "/Webhook":
            return [{"id": "g1"}]
        if path == "/Webhook/g1":
            return {
                "name": "Probe Runbook",
                "steps": [
                    {
                        "name": "cond",
                        "step_conditions": [
                            {"type": 23, "fieldname": "<<a>>", "value_string": "x"},
                            {"type": 0, "fieldname": "<<b>>", "value_string": "y"},
                        ],
                    }
                ],
            }
        raise AssertionError(f"unexpected {path}")


def test_sweep_loop_with_fake_client(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(sweep, "HaloClient", _FakeHalo)
    monkeypatch.setattr(
        sweep,
        "load_profile",
        lambda _n: type("P", (), {"tenant_url": "https://clidev.trial.usehalo.com"})(),
    )
    assert asyncio.run(sweep.main()) == 0
    out = capsys.readouterr().out
    compact = out.replace(" ", "")
    assert "scanned1runbooks,1interestingcriteria" in compact
    assert '"type":23' in compact
