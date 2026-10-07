"""Pure + fake-driven tests for scripts/build_huntress_integration.py."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_huntress", REPO / "scripts" / "build_huntress_integration.py"
)
assert _spec is not None
assert _spec.loader is not None
bh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bh)


def test_method_docs_strip_and_pin_owner() -> None:
    docs = bh._method_docs(
        [{"name": "Get Thing", "path": "/v1/things", "method": "GET", "bind_phase": "p"}], 43
    )
    assert len(docs) == 1
    assert docs[0]["integration_id"] == 43
    assert docs[0]["method"] == 0  # GET ->0
    assert not any(k.startswith("_") for k in docs[0])


class _FakeRequester:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.deleted: set[str] = set()
        self.next_method = 100

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, path))
        if method == "GET" and path == "/CustomIntegration":
            return []  # no pre-existing integration (build) / for cleanup handled below
        if method == "POST" and path == "/CustomIntegration":
            return [{"id": 43}]
        if method == "POST" and path == "/CustomIntegrationMethod":
            self.next_method += 1
            return [{"id": self.next_method}]
        if method == "GET" and path == "/CustomIntegrationMethod":
            n = (json_body or None) and 0 or 102  # always full catalog on readback
            return [{"id": i, "name": f"m{i}"} for i in range(n)]
        if method == "DELETE":
            self.deleted.add(path)
            return {}
        if method == "GET" and path.startswith("/CustomIntegration/"):
            if path.rsplit("/", 1)[-1] in {p.rsplit("/", 1)[-1] for p in self.deleted}:
                raise RuntimeError("404 after delete")
            return {"id": 43, "name": "Huntress"}
        raise AssertionError(f"unexpected {method} {path}")


class _FakeHalo:
    requester: _FakeRequester = _FakeRequester()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeRequester:
        type(self).requester = _FakeRequester()
        return type(self).requester

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import halocli.client
    import halocli.config

    monkeypatch.setattr(
        halocli.config,
        "load_profile",
        lambda _n: type("P", (), {"tenant_url": "https://clidev.trial.usehalo.com"})(),
    )
    monkeypatch.setattr(halocli.client, "HaloClient", _FakeHalo)
    monkeypatch.setattr(bh.ph, "REPO", tmp_path)
    # inputs come from the real repo-root artifacts
    real = REPO
    monkeypatch.setattr(
        bh,
        "_ph_path",
        lambda p: real / p if ":" not in str(p) else Path(p),
    )


def test_main_builds_full_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["build_huntress_integration.py", "--profile", "dev"])
    assert asyncio.run(bh.main()) == 0
    ev = json.loads((tmp_path / "huntress_integration_evidence.json").read_text(encoding="utf-8"))
    assert ev["integration"]["id"] == 43
    assert ev["methods"]["created"] == 102
    assert ev["methods"]["failed"] == 0
    assert ev["verify"]["methods_on_readback"] == 102
    calls = _FakeHalo.requester.calls
    assert sum(1 for m, p in calls if m == "POST" and p == "/CustomIntegration") == 1
    assert sum(1 for m, p in calls if m == "POST" and p == "/CustomIntegrationMethod") == 102
    out = capsys.readouterr().out
    assert "built integration 43" in out
    assert "102/102 read back" in out


def test_main_cleanup_deletes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch(monkeypatch, tmp_path)

    # cleanup path: an integration with that name EXISTS first
    class _Exists(_FakeRequester):
        async def request(self, *a: Any, **kw: Any) -> Any:
            if a[0] == "GET" and a[1] == "/CustomIntegration":
                return [{"id": 43, "name": "Huntress"}]
            return await super().request(*a, **kw)

    class _ExistsHalo(_FakeHalo):
        async def __aenter__(self) -> _Exists:
            type(self).requester = _Exists()  # type: ignore[assignment]
            return type(self).requester  # type: ignore[return-value]

    monkeypatch.setattr("halocli.client.HaloClient", _ExistsHalo)
    monkeypatch.setattr(
        "sys.argv", ["build_huntress_integration.py", "--profile", "dev", "--cleanup"]
    )
    assert asyncio.run(bh.main()) == 0
    ev = json.loads((tmp_path / "huntress_integration_evidence.json").read_text(encoding="utf-8"))
    assert ev["action"] == "cleanup"
    assert ev["deleted_id"] == 43
    assert ev["verified_gone"] is True
    out = capsys.readouterr().out
    assert "deleted integration 43" in out
