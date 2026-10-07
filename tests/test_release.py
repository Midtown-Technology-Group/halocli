"""Offline regression tests for scripts/release.py (issue #65).

The release dance must never run for real under test: every test replaces the
module's `run`/`gh` seams (and its clock) with fakes, so no `git`, `gh`,
network, or sleep call can escape. What is pinned here:

* every wait prints timestamped (`[YYYY-MM-DDTHH:MM:SSZ]`) progress lines;
* an interrupted run re-runs safely: matching tags/pushes are reused, foreign
  or version-drifted tags abort, and an already-attached `halocli.msi`
  skips the duplicate MSI dispatch;
* a manifest that does not match the release asset aborts before any push.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_harness", REPO / "scripts" / "release.py")
assert _spec is not None
assert _spec.loader is not None
rel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rel)

STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]")
HEAD = "a" * 40
OTHER = "b" * 40


class FakeClock:
    """Stand-in for the `time` module: instant sleeps that advance the clock."""

    def __init__(self, step: float = 5.0) -> None:
        self.now = 1_000_000.0
        self.step = step
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += self.step


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(rel, "time", fake)
    monkeypatch.setattr(rel, "utc_now", lambda: "2026-10-07T00:00:00Z")
    return fake


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def _ok(args: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, stdout, "")


def _fail(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 1, "", "no such thing")


def test_wait_workflow_logs_timestamped_progress(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-progress run is narrated with timestamps until it completes."""
    calls = {"n": 0}

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps([{"databaseId": 7, "status": "in_progress", "conclusion": None}])
        return json.dumps([{"databaseId": 7, "status": "completed", "conclusion": "success"}])

    monkeypatch.setattr(rel, "gh", fake_gh)
    assert rel.wait_workflow("Release", "v1.16.0", timeout_s=600) == "success"
    assert clock.sleeps, "the wait must actually poll, not return on the first answer"
    lines = _lines(capsys)
    assert len(lines) >= 3, f"expected start + progress + finish lines, got: {lines}"
    assert all(STAMP.match(line) for line in lines), lines
    assert any("run 7" in line and "in_progress" in line for line in lines)
    assert any("finished: success" in line for line in lines)


def test_wait_workflow_timeout_aborts(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: json.dumps([]))
    with pytest.raises(SystemExit, match="timeout waiting"):
        rel.wait_workflow("Release", "v1.16.0", timeout_s=30)
    assert all(STAMP.match(line) for line in _lines(capsys))


def test_wait_new_dispatch_narrates_and_returns_conclusion(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        if "view" in args:
            return json.dumps({"status": "completed", "conclusion": "success"})
        seen.append("list")
        return json.dumps([{"databaseId": 9, "status": "in_progress", "conclusion": None}])

    monkeypatch.setattr(rel, "gh", fake_gh)
    assert rel.wait_new_workflow_dispatch("Build MSI Release", known_ids={1, 2}) == "success"
    lines = _lines(capsys)
    assert all(STAMP.match(line) for line in lines), lines
    assert any("run 9" in line for line in lines)


def test_follow_dispatched_run_timeout_aborts(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rel,
        "gh",
        lambda args, *, cwd=None: json.dumps({"status": "in_progress", "conclusion": None}),
    )
    deadline = clock.time() + 30
    started = clock.time()
    with pytest.raises(SystemExit, match="timeout waiting for run 9"):
        rel._follow_dispatched_run("Build MSI Release", 9, deadline, started)


def test_find_winget_pr_skips_unrelated_titles(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    prs = [
        {"number": 3, "title": "Update halocli to 9.9.9", "headRefName": "other"},
        {"number": 4, "title": "Update halocli to 1.16.0", "headRefName": "auto-1.16.0"},
    ]
    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: json.dumps(prs))
    found = rel.find_winget_pr("1.16.0")
    assert found["number"] == 4
    lines = _lines(capsys)
    assert all(STAMP.match(line) for line in lines), lines
    assert any("#4" in line for line in lines)


def _show_pyproject(version: str) -> str:
    return f'[project]\nname = "halocli"\nversion = "{version}"\n'


def test_ensure_release_tag_creates_when_missing(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def fake_git_ok(cmd: list[str], *, cwd: Any = None) -> subprocess.CompletedProcess[str]:
        return _fail(cmd)

    def fake_run(
        cmd: list[str], *, cwd: Any = None, check: bool = True, capture: bool = True
    ) -> str:
        ran.append(cmd)
        assert cmd[:2] == ["git", "rev-parse"] or cmd[0] == "git"
        if cmd[:2] == ["git", "rev-parse"]:
            return HEAD
        return ""

    monkeypatch.setattr(rel, "_git_ok", fake_git_ok)
    monkeypatch.setattr(rel, "run", fake_run)
    assert rel.ensure_release_tag("1.16.0") == "created"
    assert ["git", "-c", "tag.gpgsign=false", "tag", "v1.16.0"] in ran


def test_ensure_release_tag_reuses_matching_tag_without_mutation(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def fake_git_ok(cmd: list[str], *, cwd: Any = None) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return _ok(cmd, HEAD + "\n")
        assert cmd[:2] == ["git", "show"]
        return _ok(cmd, _show_pyproject("1.16.0"))

    def fake_run(
        cmd: list[str], *, cwd: Any = None, check: bool = True, capture: bool = True
    ) -> str:
        ran.append(cmd)
        return HEAD

    monkeypatch.setattr(rel, "_git_ok", fake_git_ok)
    monkeypatch.setattr(rel, "run", fake_run)
    assert rel.ensure_release_tag("1.16.0") == "reused"
    assert not [c for c in ran if c[:2] == ["git", "-c"] or c[:2] == ["git", "tag"]], ran
    assert all(STAMP.match(line) for line in _lines(capsys))


def test_ensure_release_tag_rejects_foreign_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel, "_git_ok", lambda cmd, *, cwd=None: _ok(cmd, OTHER + "\n"))
    monkeypatch.setattr(rel, "run", lambda cmd, *, cwd=None, check=True, capture=True: HEAD)
    with pytest.raises(SystemExit, match="not HEAD"):
        rel.ensure_release_tag("1.16.0")


def test_ensure_release_tag_rejects_version_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_ok(cmd: list[str], *, cwd: Any = None) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "show"]:
            return _ok(cmd, _show_pyproject("9.9.9"))
        return _ok(cmd, HEAD + "\n")

    monkeypatch.setattr(rel, "_git_ok", fake_git_ok)
    monkeypatch.setattr(rel, "run", lambda cmd, *, cwd=None, check=True, capture=True: HEAD)
    with pytest.raises(SystemExit, match="mismatched tag"):
        rel.ensure_release_tag("1.16.0")


def test_ensure_tag_pushed_skips_matching_remote(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(rel, "_git_ok", lambda cmd, *, cwd=None: _ok(cmd, HEAD + "\n"))

    def fake_run(
        cmd: list[str], *, cwd: Any = None, check: bool = True, capture: bool = True
    ) -> str:
        ran.append(cmd)
        return f"{HEAD}\trefs/tags/v1.16.0\n"

    monkeypatch.setattr(rel, "run", fake_run)
    assert rel.ensure_tag_pushed("v1.16.0") == "already-pushed"
    assert not [c for c in ran if c[:2] == ["git", "push"]], ran


def test_ensure_tag_pushed_pushes_when_remote_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(rel, "_git_ok", lambda cmd, *, cwd=None: _ok(cmd, HEAD + "\n"))

    def fake_run(
        cmd: list[str], *, cwd: Any = None, check: bool = True, capture: bool = True
    ) -> str:
        ran.append(cmd)
        return ""  # ls-remote prints nothing: origin has no such tag

    monkeypatch.setattr(rel, "run", fake_run)
    assert rel.ensure_tag_pushed("v1.16.0") == "pushed"
    assert ["git", "push", "origin", "v1.16.0"] in ran


def test_ensure_tag_pushed_rejects_remote_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel, "_git_ok", lambda cmd, *, cwd=None: _ok(cmd, HEAD + "\n"))
    monkeypatch.setattr(
        rel,
        "run",
        lambda cmd, *, cwd=None, check=True, capture=True: f"{OTHER}\trefs/tags/v1.16.0\n",
    )
    with pytest.raises(SystemExit, match="refusing to move"):
        rel.ensure_tag_pushed("v1.16.0")


def test_release_has_msi_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rel, "gh", lambda args, *, cwd=None: json.dumps({"assets": [{"name": "halocli.msi"}]})
    )
    assert rel.release_has_msi("1.16.0") is True

    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: json.dumps({"assets": []}))
    assert rel.release_has_msi("1.16.0") is False

    def missing(args: list[str], *, cwd: Any = None) -> str:
        raise SystemExit("FAILED (1): gh release view v1.16.0\nrelease not found")

    monkeypatch.setattr(rel, "gh", missing)
    assert rel.release_has_msi("1.16.0") is False

    def broken(args: list[str], *, cwd: Any = None) -> str:
        raise SystemExit("FAILED (1): gh release view\nconnection reset by peer")

    monkeypatch.setattr(rel, "gh", broken)
    with pytest.raises(SystemExit, match="connection reset"):
        rel.release_has_msi("1.16.0")


def _stub_dance(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, msi_attached: bool) -> None:
    """Replace every main() step with a recorder; nothing may touch the network."""
    monkeypatch.setattr(rel, "preflight", lambda version, strict=True: [])
    monkeypatch.setattr(rel, "ensure_release_tag", lambda version: calls.append("tag") or "reused")
    monkeypatch.setattr(rel, "ensure_tag_pushed", lambda tag: calls.append("push") or "pushed")
    monkeypatch.setattr(rel, "wait_workflow", lambda *a, **k: calls.append("wait") or "success")
    monkeypatch.setattr(rel, "release_has_msi", lambda version: msi_attached)
    monkeypatch.setattr(rel, "pre_dispatch_run_ids", lambda workflow: {1})
    monkeypatch.setattr(rel, "gh", lambda args, **k: calls.append("dispatch") or "")
    monkeypatch.setattr(
        rel, "wait_new_workflow_dispatch", lambda *a, **k: calls.append("msi-wait") or "success"
    )
    monkeypatch.setattr(
        rel,
        "verify_release_assets",
        lambda version: ("ab" * 32, "{12345678-1234-1234-1234-123456789012}"),
    )
    monkeypatch.setattr(
        rel,
        "find_winget_pr",
        lambda version, **k: {"number": 11, "title": "x", "headRefName": "auto"},
    )
    monkeypatch.setattr(
        rel,
        "winget_manifest_values",
        lambda branch, version: ("ab" * 32, "{12345678-1234-1234-1234-123456789012}"),
    )
    monkeypatch.setattr(rel, "winget_dance", lambda *a: calls.append("dance"))
    monkeypatch.setattr(rel, "wait_pr_checks", lambda *a, **k: calls.append("checks"))
    monkeypatch.setattr(rel, "merge_winget_and_verify_feed", lambda version: calls.append("feed"))


def test_resumed_run_skips_duplicate_msi_dispatch(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """halocli.msi already attached: no second `workflow run`, the dance continues."""
    calls: list[str] = []
    _stub_dance(monkeypatch, calls, msi_attached=True)
    monkeypatch.setattr(
        "sys.argv", ["release.py", "1.16.0", "--yes", "--skip-tests", "--skip-pipx"]
    )
    assert rel.main() == 0
    assert "dispatch" not in calls
    assert "msi-wait" not in calls
    assert calls == ["tag", "push", "wait", "dance", "checks", "feed"]


def test_fresh_run_dispatches_msi_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _stub_dance(monkeypatch, calls, msi_attached=False)
    monkeypatch.setattr(
        "sys.argv", ["release.py", "1.16.0", "--yes", "--skip-tests", "--skip-pipx"]
    )
    assert rel.main() == 0
    assert calls == ["tag", "push", "wait", "dispatch", "msi-wait", "dance", "checks", "feed"]


def test_manifest_mismatch_aborts_before_any_push(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _stub_dance(monkeypatch, calls, msi_attached=False)
    monkeypatch.setattr(
        rel,
        "winget_manifest_values",
        lambda branch, version: ("ff" * 32, "{12345678-1234-1234-1234-123456789012}"),
    )
    monkeypatch.setattr(
        "sys.argv", ["release.py", "1.16.0", "--yes", "--skip-tests", "--skip-pipx"]
    )
    with pytest.raises(SystemExit, match="MANIFEST MISMATCH"):
        rel.main()
    assert "dance" not in calls
    assert "checks" not in calls
    assert "feed" not in calls
