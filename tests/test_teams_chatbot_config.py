"""Unit tests for the Teams chatbot config tool (pure helpers)."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_refuse_production_guard() -> None:
    module = importlib.import_module("teams_chatbot_config")
    module.refuse_production("clidev.trial.usehalo.com")  # ok
    with pytest.raises(SystemExit):
        module.refuse_production("midtowntg.halopsa.com")


def test_unwrap_list_shapes() -> None:
    module = importlib.import_module("teams_chatbot_config")
    assert module.unwrap_list([1, 2]) == [1, 2]
    assert module.unwrap_list({"results": [3]}) == [3]
    assert module.unwrap_list({"value": [4]}) == [4]
    assert module.unwrap_list({"id": 9}) == [{"id": 9}]


def test_apply_chatbot_fields_changes_only_targets() -> None:
    module = importlib.import_module("teams_chatbot_config")
    control = {
        "teams_chat_profile": None,
        "teams_chat_welcome_message": "old welcome",
        "teams_chat_help_message": "old help",
        "teams_authorized": True,
        "unrelated": {"nested": [1, 2]},
    }
    updated, changed = module.apply_chatbot_fields(control, "prof-1", "new welcome", "new help")
    assert changed == {
        "teams_chat_profile": {"before": None, "after": "prof-1"},
        "teams_chat_welcome_message": {"before": "old welcome", "after": "new welcome"},
        "teams_chat_help_message": {"before": "old help", "after": "new help"},
    }
    assert updated["teams_authorized"] is True
    assert updated["unrelated"] == {"nested": [1, 2]}
    # original untouched (deepcopy)
    assert control["teams_chat_profile"] is None
    # no drift outside the intended fields
    assert module.diff_other_fields(control, updated) == {}


def test_diff_other_fields_detects_drift() -> None:
    module = importlib.import_module("teams_chatbot_config")
    before = {"teams_chat_profile": None, "teams_authorized": True}
    after = {"teams_chat_profile": "x", "teams_authorized": False}
    drift = module.diff_other_fields(before, after)
    assert drift == {"teams_authorized": {"before": True, "after": False}}


def test_known_normalizations_are_not_drift() -> None:
    module = importlib.import_module("teams_chatbot_config")
    before = {"trophy_agents": "", "teams_authorized": True}
    after = {"trophy_agents": None, "teams_authorized": True}
    assert module.diff_other_fields(before, after) == {}
    norms = module.server_normalizations(before, after)
    assert norms == {"trophy_agents": {"before": "", "after": None}}


def test_chatbot_state_includes_context() -> None:
    module = importlib.import_module("teams_chatbot_config")
    state = module.chatbot_state({"teams_chat_profile": "p", "enableteamsmsg": True})
    assert state["teams_chat_profile"] == "p"
    assert state["_context"]["enableteamsmsg"] is True


# --------------------------------------------------------------------------
# run()/apply_state/tab_post/manifest against a fake HaloClient (house style)
# --------------------------------------------------------------------------

CONTROL_SEED = {
    "teams_chat_profile": None,
    "teams_chat_welcome_message": None,
    "teams_chat_help_message": None,
    "teams_authorized": None,
    "enableteamsmsg": True,
    "trophy_agents": "",
}

TAB_SEED = {"appname": "Halo PSA", "teamsbot_ticket_type": 0}

PROFILES_SEED = [
    {"id": "prof-external", "name": "Example Chat Profile for External Website", "access_type": 1}
]

MANIFEST_ZIP = b"PK\x03\x04fake-manifest-bytes"


class _FakeHaloClient:
    """Records requests; canned responses drive each persistence scenario."""

    def __init__(self, mode: str = "persist") -> None:
        self.mode = mode  # persist | drop | drift
        self.control = copy.deepcopy(CONTROL_SEED)
        self.requests: list[tuple[str, str, object]] = []

    async def __aenter__(self) -> "_FakeHaloClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def request(  # noqa: PLR0911 - canned router by design
        self,
        method: str,
        path: str,
        *,
        params: object = None,
        json_body: object = None,
        timeout: object = None,
        as_bytes: bool = False,
    ) -> object:
        self.requests.append((method, path, json_body))
        if method == "GET" and path == "/Control":
            return [copy.deepcopy(self.control)]
        if method == "GET" and path == "/Control/Teams":
            return copy.deepcopy(TAB_SEED)
        if method == "GET" and path == "/ChatProfile":
            return copy.deepcopy(PROFILES_SEED)
        if method == "POST" and path == "/Control":
            payload = copy.deepcopy(json_body)[0]
            if self.mode == "persist":
                self.control = payload
            elif self.mode == "drift":
                self.control = payload
                self.control["enableteamsmsg"] = not payload.get("enableteamsmsg")
            # mode == "drop": silently ignore the write (Halo's real behavior
            # for the welcome/help fields)
            return None
        if method == "POST" and path == "/Control/Teams":
            from halocli.errors import HaloCLIError

            raise HaloCLIError("HaloPSA unknown error (405) on /api/Control/Teams")
        if method == "POST" and path == "/IntegrationData/MicrosoftTeams/Manifest":
            return MANIFEST_ZIP if as_bytes else {"ok": True}
        raise AssertionError(f"unexpected request: {method} {path}")


def _install(
    module: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str = "persist"
) -> _FakeHaloClient:
    fake = _FakeHaloClient(mode)
    monkeypatch.setattr(
        module,
        "load_profile",
        lambda name: SimpleNamespace(tenant_url="https://clidev.trial.usehalo.com"),
    )
    monkeypatch.setattr(module, "HaloClient", lambda profile, profile_name=None: fake)
    monkeypatch.setattr(module, "EVIDENCE_PATH", tmp_path / "evidence.json")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    return fake


def _args(*argv: str) -> argparse.Namespace:
    module = importlib.import_module("teams_chatbot_config")
    return module.build_parser().parse_args(list(argv))


def test_run_read_mode_writes_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    fake = _install(module, monkeypatch, tmp_path)
    assert asyncio.run(module.run(_args("--profile", "dev"))) == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["tenant"] == "clidev.trial.usehalo.com"
    assert evidence["read"]["chat_profiles"][0]["access_type"] == 1
    assert evidence["read"]["chatbot"]["teams_chat_profile"] is None
    assert ("GET", "/Control/Teams", None) in fake.requests


def test_run_refuses_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    _install(module, monkeypatch, tmp_path)
    monkeypatch.setattr(
        module,
        "load_profile",
        lambda name: SimpleNamespace(tenant_url="https://midtowntg.halopsa.com"),
    )
    pending = module.run(_args("--profile", "prod"))
    with pytest.raises(SystemExit):
        asyncio.run(pending)


def test_run_apply_persists_all_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    fake = _install(module, monkeypatch, tmp_path, mode="persist")
    code = asyncio.run(
        module.run(_args("--profile", "dev", "--apply", "--chat-profile", "prof-external"))
    )
    assert code == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    persistence = evidence["apply"]["persistence"]
    assert all(entry["persisted"] for entry in persistence.values())
    assert evidence["apply"]["drift"] == {}
    assert evidence["apply"]["not_persisted"] == []
    posts = [r for r in fake.requests if r[0] == "POST"]
    assert len(posts) == 3  # one full-object save per field


def test_run_apply_dropped_fields_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = importlib.import_module("teams_chatbot_config")
    _install(module, monkeypatch, tmp_path, mode="drop")
    code = asyncio.run(
        module.run(_args("--profile", "dev", "--apply", "--chat-profile", "prof-external"))
    )
    assert code == 1
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert set(evidence["apply"]["not_persisted"]) == set(module.CHATBOT_FIELDS)


def test_run_apply_restores_on_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    fake = _install(module, monkeypatch, tmp_path, mode="drift")
    code = asyncio.run(
        module.run(_args("--profile", "dev", "--apply", "--chat-profile", "prof-external"))
    )
    assert code == 1
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert "enableteamsmsg" in evidence["apply"]["drift"]
    assert evidence["apply"]["restored"] is True
    # last POST restored the exact original (pre-write) object
    posts = [r for r in fake.requests if r[0] == "POST"]
    assert posts[-1][2] == [copy.deepcopy(CONTROL_SEED)]


def test_tab_post_records_405(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    _install(module, monkeypatch, tmp_path)
    code = asyncio.run(
        module.run(_args("--profile", "dev", "--tab-post", "--chat-profile", "prof-external"))
    )
    assert code == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert "405" in evidence["tab_post"]["post"]
    assert evidence["tab_post"]["readback_chatbot"]["teams_chat_profile"] is None


def test_manifest_generates_zip_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    _install(module, monkeypatch, tmp_path)
    code = asyncio.run(module.run(_args("--profile", "dev", "--manifest")))
    assert code == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    manifest = evidence["manifest"]
    assert manifest["looks_like_zip"] is True
    assert manifest["bytes"] == len(MANIFEST_ZIP)
    artifact = Path(manifest["artifact"])
    assert artifact.read_bytes() == MANIFEST_ZIP
    assert manifest["requested"]["name"] == "Halo Service Chatbot"


def test_apply_requires_chat_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("teams_chatbot_config")
    _install(module, monkeypatch, tmp_path)
    assert asyncio.run(module.run(_args("--profile", "dev", "--apply"))) == 2
    assert asyncio.run(module.run(_args("--profile", "dev", "--tab-post"))) == 2
