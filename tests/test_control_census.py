"""Unit tests for the Control census tool (pure helpers)."""

from __future__ import annotations

import importlib

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
    # sentinels never equal the original
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
