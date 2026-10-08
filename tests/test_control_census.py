"""Unit tests for the Control census tool (pure helpers + fake-driven loop)."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_classify_fields_buckets_families() -> None:
    module = importlib.import_module("control_census")
    props = {
        "teams_chat_profile": {"type": "string"},
        "teamsbot_disabled": {"type": "boolean"},
        "enableteamscall": {"type": "boolean"},  # contains 'team' -> teams family
        "chat_profile_id": {"type": "string"},
        "prinotify": {"type": "integer"},
        "azure_connection": {"type": "integer"},
        "_getintegrationdata": {"type": "boolean"},
        "summary": {"type": "string"},
    }
    census = module.classify_fields(props)
    assert census["total_properties"] == 8
    assert "teams_chat_profile" in census["families"]["teams"]
    assert "enableteamscall" in census["families"]["teams"]
    # families overlap by design: teams_chat_profile is in both buckets
    assert census["families"]["chat"] == ["chat_profile_id", "teams_chat_profile"]
    assert census["families"]["notification"] == ["prinotify"]
    assert census["families"]["azure/entra"] == ["azure_connection"]
    assert census["action_fields"] == ["_getintegrationdata"]
    assert census["type_histogram"]["string"] == 3  # incl. the non-family 'summary'


def test_probe_targets_exclude_action_fields() -> None:
    module = importlib.import_module("control_census")
    props = {"teams_a": {}, "_teams_action": {}, "plain": {}}
    assert module.probe_targets(props, limit=10) == ["teams_a"]


def test_sentinel_is_distinct_and_type_plausible() -> None:
    module = importlib.import_module("control_census")
    assert module.sentinel_for(None) == "__census_probe__"
    assert module.sentinel_for(True) is False
    assert module.sentinel_for(False) is True
    assert module.sentinel_for("abc") == "abc__probe"
    assert module.sentinel_for(7) == -999001
    assert module.sentinel_for(1.5) == -999001.0
    assert module.sentinel_for(["a"]) == ["a", "__probe"]
    assert module.sentinel_for({"k": 1}) == {"k": 1, "__census_probe__": True}
    for original in (None, True, "x", 3, ["a"], {"k": 1}):
        assert module.sentinel_for(original) != original


def test_classify_readback_verdicts() -> None:
    module = importlib.import_module("control_census")
    assert module.classify_readback(None, "s", "s") == "writable"
    assert module.classify_readback(None, "s", None) == "silently-dropped"
    assert module.classify_readback("old", "s", "other") == "server-transformed"


def test_unwrap_and_pick_control() -> None:
    module = importlib.import_module("control_census")
    assert module.unwrap([1]) == [1]
    assert module.unwrap({"results": [2]}) == [2]
    control = {"teams_chat_profile": "p"}
    assert module.pick_control([{"x": 1}, control]) is control
    with pytest.raises(SystemExit):
        module.pick_control([])


def test_static_census_runs_offline() -> None:
    module = importlib.import_module("control_census")
    census = module.classify_fields(module.control_properties())
    assert census["total_properties"] > 4000
    assert census["family_counts"]["teams"] > 40
    assert len(census["action_fields"]) > 20


# ---------------------------------------------------------------------------
# fake-driven: probe loop + run() paths
# ---------------------------------------------------------------------------

TEST_CONTROL = {
    "teams_chat_profile": None,
    "teams_alpha": "orig",
    "teams_dropped": False,
    "teams_transformed": "a",
    "summary": "keep",
}


class _FakeControlClient:
    """Persist-on-POST fake with per-field drop/transform behaviors."""

    def __init__(self, drop: frozenset = frozenset(), transform: frozenset = frozenset()) -> None:
        self.control = copy.deepcopy(TEST_CONTROL)
        self.drop = set(drop)
        self.transform = set(transform)
        self.posts = 0

    async def __aenter__(self) -> "_FakeControlClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: object = None,
        json_body: object = None,
        timeout: object = None,
        as_bytes: bool = False,
    ) -> object:
        if method == "GET" and path == "/Control":
            return [copy.deepcopy(self.control)]
        if method == "POST" and path == "/Control":
            self.posts += 1
            payload = copy.deepcopy(json_body)[0]
            # server ignores writes to drop-fields: originals survive
            preserved = {k: self.control[k] for k in self.drop if k in self.control}
            self.control = payload
            self.control.update(preserved)
            for key in self.transform:
                if key in self.control:
                    self.control[key] = "rewritten-by-server"
            return None
        raise AssertionError(f"unexpected {method} {path}")


def test_probe_fields_verdicts_and_restore() -> None:
    module = importlib.import_module("control_census")
    fake = _FakeControlClient(drop={"teams_dropped"}, transform={"teams_transformed"})
    targets = [
        "teams_alpha",
        "teams_dropped",
        "teams_transformed",
        "teams_never_existed",
        "teams_chat_profile",
    ]
    verdicts, residuals = asyncio.run(module.probe_fields(fake, targets))
    assert verdicts["teams_alpha"]["verdict"] == "writable"
    assert verdicts["teams_dropped"]["verdict"] == "silently-dropped"
    assert verdicts["teams_transformed"]["verdict"] == "server-transformed"
    assert verdicts["teams_never_existed"]["verdict"] == "absent-from-live-object"
    # writable fields were restored to the snapshot value
    assert fake.control["teams_alpha"] == "orig"
    # the server keeps rewriting that field: exactly it remains as residue
    assert residuals == ["teams_transformed"]


def test_probe_fields_clean_when_server_plays_along() -> None:
    module = importlib.import_module("control_census")
    fake = _FakeControlClient(drop={"teams_dropped"})
    verdicts, residuals = asyncio.run(module.probe_fields(fake, ["teams_alpha", "teams_dropped"]))
    assert verdicts["teams_alpha"]["verdict"] == "writable"
    assert residuals == []


def _install_run(
    module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake: object,
    host: str = "clidev.trial.usehalo.com",
) -> None:
    monkeypatch.setattr(module, "EVIDENCE_PATH", tmp_path / "census.json")
    monkeypatch.setattr(
        module, "load_profile", lambda name: SimpleNamespace(tenant_url=f"https://{host}")
    )
    monkeypatch.setattr(module, "HaloClient", lambda profile, profile_name=None: fake)


def test_run_static_writes_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("control_census")
    monkeypatch.setattr(module, "EVIDENCE_PATH", tmp_path / "census.json")
    args = module.build_parser().parse_args([])
    assert asyncio.run(module.run(args)) == 0
    evidence = json.loads((tmp_path / "census.json").read_text(encoding="utf-8"))
    assert evidence["mode"] == "static"
    assert evidence["census"]["total_properties"] > 4000


def test_run_probe_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("control_census")
    fake = _FakeControlClient()
    _install_run(module, monkeypatch, tmp_path, fake)
    small = {"teams_chat_profile": {}, "teams_alpha": {}, "summary": {}}
    monkeypatch.setattr(module, "control_properties", lambda: small)
    args = module.build_parser().parse_args(["--probe", "--limit", "10"])
    assert asyncio.run(module.run(args)) == 0
    evidence = json.loads((tmp_path / "census.json").read_text(encoding="utf-8"))
    assert evidence["mode"] == "static+probe"
    assert evidence["probe"]["clean"] is True
    assert evidence["probe"]["tally"]["writable"] >= 1


def test_run_probe_refuses_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("control_census")
    monkeypatch.setattr(
        module,
        "load_profile",
        lambda name: SimpleNamespace(tenant_url="https://midtowntg.halopsa.com"),
    )
    args = module.build_parser().parse_args(["--probe"])
    with pytest.raises(SystemExit):
        asyncio.run(module.run(args))
