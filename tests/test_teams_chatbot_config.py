"""Unit tests for the Teams chatbot config tool (pure helpers)."""

from __future__ import annotations

import importlib

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


def test_chatbot_state_includes_context() -> None:
    module = importlib.import_module("teams_chatbot_config")
    state = module.chatbot_state({"teams_chat_profile": "p", "enableteamsmsg": True})
    assert state["teams_chat_profile"] == "p"
    assert state["_context"]["enableteamsmsg"] is True
