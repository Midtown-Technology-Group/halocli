"""Offline contracts for the trial probes, never proof of live Halo behavior.

Exercise the real orchestration with a strict in-memory requester. The fake only
supplies recorded-style responses. Assertions pin the outgoing graphs, polling
identity, evidence classification and cleanup rather than claiming engine parity.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NAMES = [
    "runbook_filter_probe",
    "runbook_ticket_guard_probe",
    "runbook_chain_probe",
    "runbook_event_probe",
    "runbook_event_ladder",
    "runbook_api_route_probe",
    "runbook_chain_followup",
    "runbook_chain_s6b",
]


def load_probe(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrialResponses:
    """Strict endpoint fixture with explicit success, absence and failure modes."""

    def __init__(self, *, wrapped: bool = False, runs: bool = True) -> None:
        self.wrapped = wrapped
        self.runs = runs
        self.docs: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.created: list[tuple[str, dict]] = []
        self.logs: list[dict] = []
        self.fail: tuple[str, str] | None = None
        self.failed = False
        self.hide_list = False
        self.counter = 100
        self.docs["/Webhook"] = {
            "template": {
                "id": "template",
                "name": "unrelated existing runbook",
                "events": [{"id": 7, "eventno": 39, "name": "Closed", "type": -2}],
            }
        }

    async def __aenter__(self) -> TrialResponses:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def rows(self, rows: list) -> Any:
        return {"items": deepcopy(rows)} if self.wrapped else deepcopy(rows)

    def add_run(self, wid: str, *, executed: int = 3, status: int = 2) -> None:
        if self.runs:
            self.logs.append(
                {
                    "id": 5000 + len(self.logs),
                    "runbook_id": wid,
                    "runbook_name": self.docs["/Webhook"][wid]["name"],
                    "status": status,
                    "error": "" if status == 2 else "Failed result reached",
                    "steps_executed": executed,
                    "runbook_step": 4,
                    "iteration": 3,
                }
            )

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, path, deepcopy(json_body)))
        if self.fail == (method, path) and not self.failed:
            self.failed = True
            raise RuntimeError("injected transport failure")
        if method == "GET" and path == "/NotificationLog":
            return self.rows([])
        if method == "POST" and path == "/Actions":
            assert json_body[0]["ticket_id"] in self.docs["/Tickets"]
            return [{"id": 9000}]
        if method == "GET" and path == "/FieldInfo":
            return self.rows([{"id": 42, "name": "reportedby"}])
        if method == "GET" and path == "/Automation":
            return self.rows([] if self.hide_list else self.logs)
        if method == "GET" and path.startswith("/Automation/"):
            rid = int(path.rsplit("/", 1)[1])
            return deepcopy(next((r for r in self.logs if r["id"] == rid), {"id": rid}))
        if method == "POST" and path.startswith("/Automation/"):
            wid = path.rsplit("/", 1)[1]
            doc = self.docs["/Webhook"][wid]
            self.add_run(wid)
            target = doc["steps"][0].get("start_new_runbook_id")
            if target:
                self.add_run(target)
            return {}
        if method == "POST" and path in (
            "/Webhook",
            "/Notification",
            "/Tickets",
            "/CustomIntegration",
            "/CustomIntegrationMethod",
        ):
            assert isinstance(json_body, list) and len(json_body) == 1
            doc = deepcopy(json_body[0])
            self.counter += 1
            identity = str(doc.get("id") or self.counter)
            doc["id"] = identity
            self.docs.setdefault(path, {})[identity] = doc
            self.created.append((path, deepcopy(doc)))
            if path == "/Tickets":
                for binding in self.docs.get("/Notification", {}).values():
                    wid = str(binding["webhook_id"])
                    sender = doc.get("reportedby")
                    conditions = binding.get("conditions", [])
                    if conditions and sender != conditions[0]["value_string"]:
                        continue
                    self.add_run(wid, executed=1 if sender == "noreply@voicemail.goto.com" else 3)
            return self.rows([doc]) if not self.wrapped else doc
        if method == "GET" and path in self.docs:
            return self.rows(list(self.docs[path].values()))
        endpoint, _, identity = path.rpartition("/")
        if method == "GET" and identity in self.docs.get(endpoint, {}):
            return deepcopy(self.docs[endpoint][identity])
        if method == "DELETE" and identity in self.docs.get(endpoint, {}):
            del self.docs[endpoint][identity]
            return {}
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def install_probe(monkeypatch: pytest.MonkeyPatch):
    def install(name: str, fake: TrialResponses, *, host: str = "offline.invalid") -> Any:
        module = load_probe(name)
        monkeypatch.setattr(sys, "argv", [f"{name}.py"])
        monkeypatch.setattr(
            "halocli.config.load_profile", lambda _: SimpleNamespace(tenant_url=f"https://{host}")
        )
        monkeypatch.setattr("halocli.client.HaloClient", lambda *a, **kw: fake)

        async def no_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
        return module

    return install


def run(module: Any, capsys: Any) -> dict:
    assert asyncio.run(module.main()) == 0
    return json.loads(capsys.readouterr().out)


def assert_clean(fake: TrialResponses) -> None:
    assert fake.docs["/Webhook"] == {"template": fake.docs["/Webhook"]["template"]}
    for endpoint in ("/Notification", "/Tickets", "/CustomIntegration", "/CustomIntegrationMethod"):
        assert not fake.docs.get(endpoint)
    assert not any(m == "DELETE" and p == "/Webhook/template" for m, p, _ in fake.calls)


@pytest.mark.parametrize("name", NAMES)
def test_production_refused_before_client_requests(name, install_probe):
    fake = TrialResponses()
    module = install_probe(name, fake, host="midtowntg.halopsa.com")
    with pytest.raises(SystemExit, match="production host"):
        asyncio.run(module.main())
    assert fake.calls == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_filter_payloads_verdict_and_cleanup(wrapped, install_probe, capsys, tmp_path, monkeypatch):
    fake = TrialResponses(wrapped=wrapped)
    module = install_probe("runbook_filter_probe", fake)
    save = tmp_path / "filter.json"
    monkeypatch.setattr(sys, "argv", ["probe", "--save", str(save)])
    out = run(module, capsys)
    assert json.loads(save.read_text()) == out
    assert out["verdict"].startswith("PROVEN:")
    assert out["blocked_leg"]["run"] == "no run observed (correct)"
    assert out["allowed_leg"]["run"]["status"] == 2
    assert out["reportedby_fieldid"] == 42
    for key, field in [("V30_empty", "<<label>>"), ("Vint_10", "<<limit>>")]:
        assert out[key]["persisted_criterion"]["fieldname"] == field
        assert out[key]["persisted_criterion"]["tablename"] == "runbookvariable"
    binding = next(d for p, d in fake.created if p == "/Notification")
    assert binding["filter_type"] == 2
    assert binding["conditions"][0]["tablename"] == "faults"
    assert binding["conditions"][0]["value_string"] == module.SENDER
    assert binding["conditions"][0]["fieldid"] == 42
    assert_clean(fake)


@pytest.mark.parametrize("runs", [False, True])
def test_ticket_guard_distinguishes_observed_paths(
    runs, install_probe, capsys, tmp_path, monkeypatch
):
    fake = TrialResponses(wrapped=True, runs=runs)
    module = install_probe("runbook_ticket_guard_probe", fake)
    save = tmp_path / "guard.json"
    monkeypatch.setattr(sys, "argv", ["probe", "--save", str(save)])
    out = run(module, capsys)
    assert json.loads(save.read_text()) == out
    assert out["both_status2"] is runs
    assert out["paths_differ"] is runs
    assert out["verdict"].startswith("PROVEN:" if runs else "INCONCLUSIVE")
    doc = next(d for p, d in fake.created if p == "/Webhook")
    guard = doc["steps"][0]
    assert guard["step_conditions"][0]["fieldname"] == "reportedby"
    assert guard["step_conditions"][0]["type"] == 1
    assert [a["end_step"] for a in guard["actions"]] == [2, 4]
    assert_clean(fake)


@pytest.mark.parametrize("runs", [False, True])
def test_chain_target_binding_and_missing_run_evidence(runs, install_probe, capsys):
    fake = TrialResponses(runs=runs)
    module = install_probe("runbook_chain_probe", fake)
    out = run(module, capsys)
    assert out["target_ran"] is runs
    assert bool(out["chain_runs"]) is runs
    docs = [d for p, d in fake.created if p == "/Webhook"]
    target, source = docs[:2]
    assert source["steps"][0]["start_new_runbook_id"] == target["id"]
    assert source["steps"][0]["auto_action"] == 24
    assert source["steps"][0]["actions"] == []
    assert set(out["value_legs"]) == {"", "x"}
    assert_clean(fake)


@pytest.mark.parametrize("wrapped", [False, True])
def test_event_binding_runlog_and_cleanup(wrapped, install_probe, capsys):
    fake = TrialResponses(wrapped=wrapped)
    module = install_probe("runbook_event_probe", fake)
    out = run(module, capsys)
    assert out["template_event"]["eventno"] == 39
    assert out["runlog"]["status"] == 2
    binding = next(d for p, d in fake.created if p == "/Notification")
    assert binding["eventno"] == 3
    assert binding["webhook_id"] == out["create"]["id"]
    assert out["cleanup"] == ["notification binding deleted", "runbook deleted", "ticket deleted"]
    assert_clean(fake)


@pytest.mark.parametrize("runs", [False, True])
def test_event_ladder_stops_only_after_observed_run(
    runs, install_probe, capsys, tmp_path, monkeypatch
):
    fake = TrialResponses(runs=runs)
    fake.docs["/Webhook"]["template"]["events"].append(
        {"id": 8, "eventno": 3, "guid": "must-not-upsert", "webhook_id": "template"}
    )
    module = install_probe("runbook_event_ladder", fake)
    dump = tmp_path / "runbooks.json"
    dump.write_text(json.dumps({"runbooks": [{"name": "AI Auto Triage", "id": "template"}]}))
    result = tmp_path / "result.json"
    monkeypatch.setattr(module, "DUMP", dump)
    monkeypatch.setattr(module, "RESULT", result)
    out = run(module, capsys)
    assert json.loads(result.read_text()) == out
    assert bool(out["L1"]["runs"]) is runs
    assert ("L2" in out) is not runs
    assert "L3_action" in out
    if not runs:
        assert out["L2"]["runs"] == []
        binding = [d for p, d in fake.created if p == "/Notification"][-1]
        assert binding["guid"] is None
        assert binding["id"] != "8"
        assert binding["webhook_id"] == out["runbook"]
    assert_clean(fake)


@pytest.mark.parametrize("runs", [False, True])
def test_api_route_builds_method_edges_and_reverse_cleanup(runs, install_probe, capsys):
    fake = TrialResponses(runs=runs)
    module = install_probe("runbook_api_route_probe", fake)
    out = run(module, capsys)
    assert set(out["cases"]) == {"self_instanceinfo", "external_example"}
    for rec in out["cases"].values():
        assert rec["fire"] == "ok"
        assert rec["cleanup_runbook"] == rec["cleanup_integration"] == "deleted"
        assert (isinstance(rec["runlog"], dict)) is runs
        if not runs:
            assert rec["runlog"] == "not found"
    docs = [d for p, d in fake.created if p == "/Webhook"]
    for doc in docs:
        first = doc["steps"][0]
        assert first["auto_action"] == 6
        assert [a["end_step"] for a in first["actions"]] == [2, 4]
    deletes = [p.rsplit("/", 1)[0] for m, p, _ in fake.calls if m == "DELETE"]
    assert deletes == ["/Webhook", "/CustomIntegrationMethod", "/CustomIntegration"] * 2
    assert_clean(fake)


@pytest.mark.parametrize(
    "name,key", [("runbook_chain_followup", "s6"), ("runbook_chain_s6b", "s6b")]
)
@pytest.mark.parametrize("runs", [False, True])
def test_iteration_probe_records_absence_and_cleans_up(name, key, runs, install_probe, capsys):
    fake = TrialResponses(wrapped=True, runs=runs)
    module = install_probe(name, fake)
    out = run(module, capsys)
    assert out[key]["fire"] == "ok"
    assert out[key]["cleanup"] == "deleted"
    assert isinstance(out[key]["runlog"], dict) is runs
    doc = next(d for p, d in fake.created if p == "/Webhook")
    begin, _, advance, _ = doc["steps"]
    assert begin["auto_action"] == 12
    assert advance["auto_action"] == 13
    assert advance["actions"][1]["end_step"] == -98
    if key == "s6b":
        assert begin["message"] == advance["message"] == "<<items>>"
        assert json.loads(doc["input_variables"][0]["value"]) == ["a", "b", "c"]
        assert len(out["matrix_rows_by_id"]) == 6
    assert_clean(fake)


@pytest.mark.parametrize(
    "name", ["runbook_event_probe", "runbook_chain_followup", "runbook_chain_s6b"]
)
def test_create_failure_is_evidence_not_false_success(name, install_probe, capsys):
    fake = TrialResponses()
    fake.fail = ("POST", "/Webhook")
    module = install_probe(name, fake)
    out = run(module, capsys)
    key = {
        "runbook_event_probe": "create",
        "runbook_chain_followup": "s6",
        "runbook_chain_s6b": "s6b",
    }[name]
    assert "injected transport failure" in str(out[key])
    assert not any(m == "POST" and p.startswith("/Automation/") for m, p, _ in fake.calls)
    assert_clean(fake)
