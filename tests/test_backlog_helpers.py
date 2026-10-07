"""Regression coverage for the Sonar backlog batch-1 helpers.

Each test pins behavior the backlog fixes relied on: path canonicalization
(S8707), https-only spec fetching (S8703), the sync/async helper dispatch,
and the pure mirror-evidence helpers that were lifted out of ``main()`` so
they are reachable under test.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

# Probe/CLI scripts that the batch-1 fixes touched but that had no tests yet:
# importing them proves the module-level additions (constants, helper defs)
# load cleanly, and the helpers are then exercised directly below.
SCRIPT_MODULES = (
    "runbook_filter_probe",
    "runbook_ticket_guard_probe",
    "runbook_chain_probe",
    "runbook_build_probe",
    "runbook_chain_matrix",
    "runbook_chain_s6b",
    "release_ladder",
    "check_spec_currency",
    "get_backlog_probes",
    "evidence_probes",
    "issue73_probe",
    "release",
    "dev_write_sweep",
    "mirror_evidence_probes",
    "build_coverage_ledger",
    "vendor_halo_spec",
)


@pytest.mark.parametrize("name", SCRIPT_MODULES)
def test_script_modules_import_cleanly(name: str) -> None:
    module = importlib.import_module(name)
    assert module is not None


@pytest.mark.parametrize(
    "name",
    ["runbook_filter_probe", "runbook_ticket_guard_probe", "runbook_chain_probe", "release_ladder"],
)
def test_safe_path_helpers_canonicalize(name: str, tmp_path: Path) -> None:
    module = importlib.import_module(name)
    weird = str(tmp_path / "a" / ".." / "b.txt")
    assert module._safe_path(weird) == (tmp_path / "b.txt").resolve()
    assert module._safe_path(tmp_path).is_absolute()


def test_check_spec_url_requires_allowlisted_host() -> None:
    module = importlib.import_module("check_spec_currency")
    assert module._safe_url(module.DEFAULT_URL) == module.DEFAULT_URL
    for bad in (
        "http://dtcdev.halopsa.com/spec.json",
        "https://evil.example.com/spec.json",
        "https:///nohost",
        "file:///etc/passwd",
        "not-a-url",
    ):
        with pytest.raises(ValueError):
            module._safe_url(bad)


def test_check_spec_main_rejects_bad_url(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("check_spec_currency")
    monkeypatch.setattr(
        sys, "argv", ["check_spec_currency.py", "--url", "https://evil.example.com/spec.json"]
    )
    with pytest.raises(SystemExit):
        module.main()


def test_check_spec_main_fetches_through_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("check_spec_currency")
    seen: dict[str, Any] = {}

    class _Resp:
        def read(self) -> bytes:
            return Path(module.VENDORED).read_bytes()

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

    def fake_urlopen(url: str, timeout: int = 0) -> _Resp:
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["check_spec_currency.py"])
    assert module.main() == 0
    assert seen["url"].startswith("https://")


def test_vendor_url_and_paths_are_validated(tmp_path: Path) -> None:
    module = importlib.import_module("vendor_halo_spec")
    url = "https://example.com/spec.json"
    assert module._safe_url(url) == url
    with pytest.raises(ValueError):
        module._safe_url("http://example.com/spec.json")

    out = tmp_path / "out.json"
    src, safe_out, overlay = module._vendor_safe_paths(None, out, None)
    assert src is None and overlay is None
    assert safe_out == out.resolve()


def test_vendor_fetch_spec_requests_validated_url(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("vendor_halo_spec")
    seen: dict[str, Any] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"paths": {}}'

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

    def fake_urlopen(request: Any, timeout: int = 0) -> _Resp:
        seen["url"] = str(request.full_url)
        return _Resp()

    with pytest.raises(ValueError):
        module.fetch_spec("http://example.com/spec.json")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    spec, raw = module.fetch_spec("https://example.com/spec.json")
    assert spec == {"paths": {}}
    assert raw.startswith(b"{")
    assert seen["url"].startswith("https://")


def test_fallback_attempts_covers_documented_and_scope_params() -> None:
    module = importlib.import_module("get_backlog_probes")
    attempts = module.fallback_attempts(["count", "page_size", "agent_id"])
    assert attempts, "documented params produce at least one attempt"
    assert any("agent_id" in a for a in attempts)


def test_registry_map_classifies_write_verbs() -> None:
    module = importlib.import_module("build_coverage_ledger")
    mapping = module.registry_map()
    assert ("/Tickets", "get") in mapping
    assert any(path == "/Tickets" and verb == "post" for path, verb in mapping)


def test_dev_write_sweep_mirror_canonicalizes_path(tmp_path: Path) -> None:
    module = importlib.import_module("dev_write_sweep")
    db = tmp_path / "mirror.db"
    sqlite3.connect(db).close()
    mirror = module.Mirror(db)
    assert mirror.conn is not None
    # extras() is pure for resources without a special case: call it on an
    # uninitialized Sweeper so the branch-free path runs without a client.
    sweeper = object.__new__(module.Sweeper)
    assert module.Sweeper.extras(sweeper, "resource-without-extras") == {}


def test_mirror_probe_accepts_sync_and_async_callables() -> None:
    module = importlib.import_module("mirror_evidence_probes")
    out: dict[str, Any] = {}

    async def run() -> None:
        await module.probe(out, "sync", lambda: {"a": 1})
        await module.probe(out, "async", _async_result)
        await module.probe(out, "boom", lambda: 1 / 0)

    async def _async_result() -> dict:
        return {"b": 2}

    asyncio.run(run())
    assert out["sync"] == {"a": 1}
    assert out["async"] == {"b": 2}
    assert "ZeroDivisionError" in out["boom"]["error"]


def test_mirror_date_and_agent_helpers() -> None:
    module = importlib.import_module("mirror_evidence_probes")
    rows = [
        {"dateoccurred": "2026-01-02T03:04:05", "datecreated": "", "agent_id": 4},
        {"dateoccurred": None, "datecreated": "2026-01-01", "agent_id": 0},
    ]
    assert module.dates(rows) == ["2026-01-02T03:04:05", ""]
    dated = module.dates_probe(rows)
    assert dated["sampled"] == 2
    assert dated["datecreated_blank"] == 1
    assert dated["dateoccurred_present"] == 1
    assert len(dated["sample_pair"]) == 2
    agent = module.agent_probe(rows)
    assert agent["agent_id_present"] == 1


def test_mirror_closed_cross_census() -> None:
    module = importlib.import_module("mirror_evidence_probes")
    rows = [
        {"status_id": 1, "hasbeenclosed": True},
        {"status_id": 1, "hasbeenclosed": True},
        {"status_id": 2, "hasbeenclosed": None},
        {"status_id": 2, "hasbeenclosed": 0},
    ]
    assert module.closed_cross(rows) == {
        "1": {"true": 2},
        "2": {"null": 1, "0": 1},
    }
