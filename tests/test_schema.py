from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from halocli import schema

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    schema.clear_cache()
    yield
    schema.clear_cache()


# --------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------


def test_spec_loads_and_is_official() -> None:
    spec = schema.load_spec()
    assert spec is not None
    assert len(spec["paths"]) >= 900  # vendored spec currently has 927
    meta = schema.spec_meta()
    assert meta is not None
    assert meta["origin"] == "official"
    assert meta["$schema_source"].startswith("https://")
    assert meta["path_count"] == len(spec["paths"])


def test_spec_load_is_cached_and_fast() -> None:
    start = time.perf_counter()
    first = schema.load_spec()
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert first is not None
    assert elapsed_ms < 500, f"spec load took {elapsed_ms:.0f}ms"
    assert schema.load_spec() is first  # second call served from cache


def test_importing_schema_does_not_read_spec_file() -> None:
    """Importing halocli.schema must not open the JSON file (lazy by contract)."""
    code = (
        "import builtins\n"
        "opened = []\n"
        "real_open = builtins.open\n"
        "def spy(file, *args, **kwargs):\n"
        "    if 'halo_openapi' in str(file):\n"
        "        opened.append(str(file))\n"
        "    return real_open(file, *args, **kwargs)\n"
        "builtins.open = spy\n"
        "import halocli.schema as schema\n"
        "assert not opened, 'spec read at import time: %r' % opened\n"
        "assert schema._SPEC_CACHE is None, 'spec parsed at import time'\n"
        "print('lazy-ok')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "lazy-ok" in result.stdout


# --------------------------------------------------------------------------------------------
# lookup_operation
# --------------------------------------------------------------------------------------------


def test_lookup_exact_collection_path() -> None:
    operation = schema.lookup_operation("GET", "/Tickets")
    assert operation is not None
    assert operation.get("summary")
    assert schema.lookup_operation("get", "/Tickets") is not None  # method is case-insensitive


def test_lookup_templated_item_path() -> None:
    spec = schema.load_spec()
    assert spec is not None
    matched = schema._match_path(spec, "/Tickets/123")
    assert matched is not None
    assert matched[0] == "/Tickets/{id}"
    # /Tickets (collection) and /Tickets/{id} (item) resolve to different operations
    collection = schema.lookup_operation("GET", "/Tickets")
    item = schema.lookup_operation("GET", "/Tickets/123")
    assert collection is not None
    assert item is not None
    assert collection is not item
    # literal template beats the parameterized one
    literal = schema._match_path(spec, "/Tickets/zapier")
    assert literal is not None
    assert literal[0] == "/Tickets/zapier"


def test_lookup_nested_path() -> None:
    spec = schema.load_spec()
    assert spec is not None
    matched = schema._match_path(spec, "/IntegrationData/Get/SalesMailbox/42")
    assert matched is not None
    assert matched[0] == "/IntegrationData/Get/SalesMailbox/{id}"
    assert schema.lookup_operation("GET", "/IntegrationData/Get/SalesMailbox/42") is not None
    # mixed placeholder segment: /TicketApproval/{id}&{seq}
    mixed = schema._match_path(spec, "/TicketApproval/7&3")
    assert mixed is not None
    assert mixed[0] == "/TicketApproval/{id}&{seq}"


def test_lookup_normalizes_urls_prefixes_and_slashes() -> None:
    assert schema.lookup_operation("GET", "/api/Tickets/") is not None
    assert schema.lookup_operation("GET", "/Tickets?include=actions") is not None
    assert schema.lookup_operation("GET", "https://midtowntg.halopsa.com/api/Tickets") is not None
    assert schema.lookup_operation("GET", "Tickets") is not None  # missing leading slash
    assert schema.lookup_operation("GET", "/tickets") is not None  # case-insensitive fallback


def test_lookup_unknown_path_and_unknown_method() -> None:
    assert schema.lookup_operation("GET", "/Definitely/Not/A/Path") is None
    assert schema.lookup_operation("PUT", "/Tickets") is None  # path exists, method does not


# --------------------------------------------------------------------------------------------
# validate_request
# --------------------------------------------------------------------------------------------


def test_validate_known_good_body() -> None:
    problems = schema.validate_request(
        "POST", "/ChatMessage/IsTyping", {"chat_id": 1, "sender_id": 2}
    )
    assert problems == []
    # spec has no request-body opinion for plain GETs -> no problems even with a body
    assert schema.validate_request("GET", "/Tickets", {"anything": True}) == []
    assert schema.validate_request("GET", "/Tickets", None) == []


def test_validate_missing_required_property() -> None:
    problems = schema.validate_request("POST", "/ChatMessage/IsTyping", {"sender_id": 2})
    assert len(problems) == 1
    assert "missing required" in problems[0]
    assert "chat_id" in problems[0]


def test_validate_unknown_property_warns() -> None:
    problems = schema.validate_request(
        "POST", "/ChatMessage/IsTyping", {"chat_id": 1, "sender_id": 2, "bogus_field": True}
    )
    assert len(problems) == 1
    assert problems[0].startswith("warning:")
    assert "bogus_field" in problems[0]


def test_validate_array_request_body() -> None:
    # POST /Tickets takes an array of Faults; Faults declares properties but no required set
    assert schema.validate_request("POST", "/Tickets", [{"summary": "hello"}]) == []
    problems = schema.validate_request(
        "POST", "/Tickets", [{"summary": "hello", "zzz_unknown_field": 1}]
    )
    assert len(problems) == 1
    assert "body[0]" in problems[0]
    assert "zzz_unknown_field" in problems[0]


def test_validate_unknown_endpoint_returns_single_problem() -> None:
    problems = schema.validate_request("GET", "/Definitely/Not/A/Path", None)
    assert len(problems) == 1
    assert problems[0].startswith("unknown endpoint")


def test_validate_wrong_method_reports_known_methods() -> None:
    problems = schema.validate_request("PUT", "/Tickets", None)
    assert len(problems) == 1
    assert problems[0].startswith("unknown endpoint")
    assert "GET" in problems[0]
    assert "POST" in problems[0]


# --------------------------------------------------------------------------------------------
# search_operations
# --------------------------------------------------------------------------------------------


def test_search_match_ranks_exact_segment_first() -> None:
    results = schema.search_operations("tickets")
    assert results
    assert results[0]["path"] == "/Tickets"
    assert results[0]["method"] == "GET"
    assert results[0]["score"] > 0
    assert all(entry["path"] and entry["method"] for entry in results)


def test_search_multi_token_match() -> None:
    results = schema.search_operations("kb article")
    assert results
    assert results[0]["path"] == "/KBArticle"


def test_search_no_match_returns_empty() -> None:
    assert schema.search_operations("qqzzxxnonsense") == []
    assert schema.search_operations("") == []


def test_search_respects_limit() -> None:
    assert len(schema.search_operations("ticket", limit=5)) == 5
    assert len(schema.search_operations("ticket", limit=2)) == 2
    assert schema.search_operations("ticket", limit=0) == []
    assert schema.search_operations("ticket", limit=-1) == []


# --------------------------------------------------------------------------------------------
# graceful degradation
# --------------------------------------------------------------------------------------------


def test_degrades_when_spec_path_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(schema, "SPEC_PATH", str(tmp_path / "does_not_exist.json"))
    schema.clear_cache()
    assert schema.load_spec() is None
    assert schema.spec_meta() is None
    assert schema.lookup_operation("GET", "/Tickets") is None
    assert schema.search_operations("tickets") == []
    assert schema.validate_request("POST", "/Tickets", [{"summary": "x"}]) == []


def test_degrades_when_spec_is_corrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "halo_openapi.json"
    broken.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(schema, "SPEC_PATH", str(broken))
    schema.clear_cache()
    assert schema.load_spec() is None
    assert schema.lookup_operation("GET", "/Tickets") is None
    assert schema.search_operations("tickets") == []
    assert schema.validate_request("GET", "/Nope", None) == []


def test_degrades_when_spec_file_is_renamed_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Never move the real vendored spec: an interrupted run would leave it renamed, other
    # parallel tests would fail, and the test cannot work on a read-only install. Point
    # SPEC_PATH at a temp path instead, clearing the cache around the change.
    real_path = schema.SPEC_PATH
    assert Path(real_path).exists(), "vendored spec should exist before rename test"
    schema.clear_cache()
    monkeypatch.setattr(schema, "SPEC_PATH", str(tmp_path / "renamed_away.json"))
    try:
        assert schema.load_spec() is None
        assert schema.lookup_operation("GET", "/Tickets") is None
        assert schema.search_operations("tickets") == []
        assert schema.validate_request("GET", "/Tickets", None) == []
        # failures are not cached: restoring the path loads the real spec again
        monkeypatch.setattr(schema, "SPEC_PATH", real_path)
        assert schema.load_spec() is not None
    finally:
        schema.clear_cache()
