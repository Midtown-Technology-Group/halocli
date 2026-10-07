"""Unit tests for scripts/bifrost_convert.py (CLI/apply plumbing).

The live round-trips are proven by the trial probes and committed
evidence (chain_orchestration_evidence.json); these tests pin the
deterministic parts with fakes: dependency ordering (_create_order),
multi-workflow build (build_outputs), name-target resolution
(_create_runbook), and the aa24 chain-run proof (_chain_started_runs).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bifrost_convert.py"
_spec = importlib.util.spec_from_file_location("bifrost_convert_cli", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bifrost"


def _args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = dict(
        integrations=None,
        integrations_json=None,
        integration=None,
        methods=None,
        methods_integration=None,
        extract_module=None,
        phase_bindings=None,
        workflow_file=None,
        workflows_yaml=None,
        workflow=None,
        function=None,
        triggers=None,
        trigger_filter=[],
        ticket_guard=[],
    )
    base.update(over)
    return argparse.Namespace(**base)


# --- _create_order (dependency-ordered creates) --------------------------


def _wf_file(tmp_path: Path, name: str, chains: dict) -> Path:
    p = tmp_path / f"runbook__{name}.json"
    p.write_text(json.dumps({"name": name, "_chains": chains}), encoding="utf-8")
    return p


def test_create_order_puts_chain_targets_first(tmp_path: Path) -> None:
    files = {
        "integration:z": tmp_path / "int.json",  # never read (non-runbook)
        "runbook:parent": _wf_file(tmp_path, "parent", {"1": "child"}),
        "runbook:child": _wf_file(tmp_path, "child", {}),
    }
    assert cli._create_order(files) == [
        "integration:z",
        "runbook:child",
        "runbook:parent",
    ]


def test_create_order_ignores_missing_and_broken_targets(tmp_path: Path) -> None:
    broken = tmp_path / "runbook__broken.json"
    broken.write_text("{not json", encoding="utf-8")
    files = {
        "runbook:parent": _wf_file(tmp_path, "parent", {"1": "ghost", "2": "broken"}),
        "runbook:broken": broken,
    }
    order = cli._create_order(files)
    assert order[-1] == "runbook:parent"  # ghost dep dropped, no crash
    assert len(order) == 2


def test_create_order_survives_cycles(tmp_path: Path) -> None:
    files = {
        "runbook:a": _wf_file(tmp_path, "a", {"1": "b"}),
        "runbook:b": _wf_file(tmp_path, "b", {"1": "a"}),
    }
    order = cli._create_order(files)
    assert sorted(order) == ["runbook:a", "runbook:b"]  # no hang, both present


# --- build_outputs (multi-workflow conversion) ---------------------------


def test_build_outputs_multi_workflow_without_function(tmp_path: Path) -> None:
    args = _args(workflow_file=str(FIXTURE / "chain_parent_child.py"))
    out = cli.build_outputs(args, tmp_path / "out")
    names = [w["name"] for w in out["report"]["workflows"]]
    assert names == ["chain_fixture_child", "chain_fixture_parent"]
    assert all(w["converted"] for w in out["report"]["workflows"])
    parent = json.loads(
        (tmp_path / "out" / "runbook__chain_fixture_parent.json").read_text(encoding="utf-8")
    )
    assert parent["_chains"]  # aa24 name target rides the payload sidecar
    files = {k: Path(v) for k, v in out["files"].items()}
    # the sidecar is what _create_order consumes: child before parent
    assert cli._create_order(files) == [
        "runbook:chain_fixture_child",
        "runbook:chain_fixture_parent",
    ]


def test_build_outputs_single_function_still_selects_one(tmp_path: Path) -> None:
    args = _args(
        workflow_file=str(FIXTURE / "chain_parent_child.py"),
        function="chain_fixture_child",
    )
    out = cli.build_outputs(args, tmp_path / "out")
    assert [w["name"] for w in out["report"]["workflows"]] == ["chain_fixture_child"]


def test_build_outputs_reports_undecorated_file(tmp_path: Path) -> None:
    src = tmp_path / "plain.py"
    src.write_text("def not_a_workflow(client):\n    return {}\n", encoding="utf-8")
    out = cli.build_outputs(_args(workflow_file=str(src)), tmp_path / "out")
    entry = out["report"]["workflows"][0]
    assert entry["converted"] is False
    assert any("no @workflow" in n for n in entry["notes"])


# --- _create_runbook (chain name -> id resolution) ------------------------

CHILD_ID = "bbbbbbbb-2222-4333-8444-555555555555"


class _WebhookClient:
    """POST /Webhook creates (echoes the doc), GET reads it back."""

    def __init__(self) -> None:
        self.created: dict = {}

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        if method == "POST" and path == "/Webhook":
            self.created = (json_body or [{}])[0]
            return [{"id": "aaaaaaaa-1111-4111-8111-111111111111"}]
        if method == "GET" and path.startswith("/Webhook/"):
            return {
                "type": 1,
                "steps": self.created.get("steps"),
                "input_variables": self.created.get("input_variables"),
                "runbook_start_type": self.created.get("runbook_start_type"),
                "group_id": self.created.get("group_id"),
            }
        raise AssertionError(f"unexpected {method} {path}")


def _chain_payload() -> dict:
    return {
        "name": "WB Parent",
        "type": 1,
        "steps": [
            {"step_id": 1, "name": "prepare", "steptype": 2, "actions": []},
            {
                "step_id": 2,
                "name": "chain_fixture_child",
                "steptype": 2,
                "auto_action": 24,
                "start_new_runbook_id": None,
                "actions": [],
            },
        ],
        "input_variables": [],
        "_chains": {"2": "chain_fixture_child"},
    }


def test_create_runbook_patches_chain_target_before_post() -> None:
    client = _WebhookClient()
    out = asyncio.run(
        cli._create_runbook(
            client,
            _chain_payload(),
            "parent",
            {},
            {},
            {"chain_fixture_child": CHILD_ID},
        )
    )
    assert out["id"]  # created
    assert out["chains"] == {"chain_fixture_child": CHILD_ID}
    step = next(s for s in client.created["steps"] if s.get("auto_action") == 24)
    assert step["start_new_runbook_id"] == CHILD_ID  # patched BEFORE the POST
    assert "_chains" not in client.created  # sidecar stripped from the body


def test_create_runbook_warns_when_target_missing() -> None:
    client = _WebhookClient()
    out = asyncio.run(cli._create_runbook(client, _chain_payload(), "parent", {}, {}, {}))
    assert out["chains"] == {"chain_fixture_child": None}
    assert any("chains into" in w for w in out.get("binding_warnings", []))
    step = next(s for s in client.created["steps"] if s.get("auto_action") == 24)
    assert step["start_new_runbook_id"] is None  # honest unbound, never a guess


# --- _chain_started_runs (the aa24 proof) ---------------------------------


class _RunlogClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def request(
        self, method: str, path: str, params: dict | None = None, timeout: float | None = None
    ) -> Any:
        assert method == "GET"
        assert path == "/Automation"
        return self.rows


def test_chain_started_runs_picks_row_newer_than_baseline() -> None:
    client = _RunlogClient(
        [
            {"id": 2606, "runbook_id": CHILD_ID, "status": 2},  # direct fire
            {"id": 2608, "runbook_id": CHILD_ID, "status": 2, "steps_executed": 1},
        ]
    )
    out = asyncio.run(cli._chain_started_runs(client, {"child": CHILD_ID}, {"child": 2606}))
    assert out["child"]["id"] == 2608
    assert out["child"]["baseline_run_id"] == 2606


def test_chain_started_runs_reports_missing_baseline() -> None:
    out = asyncio.run(cli._chain_started_runs(_RunlogClient([]), {"child": "g"}, {}))
    assert "error" in out["child"]
