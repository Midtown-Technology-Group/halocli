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
import urllib.request
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


def test_wait_new_dispatch_ignores_other_versions(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        calls["n"] += 1
        rows = [
            {
                "databaseId": 9,
                "status": "completed",
                "conclusion": "failure",
                "displayTitle": "Build MSI Release v9.9.9",
            }
        ]
        if calls["n"] > 1:
            rows.insert(
                0,
                {
                    "databaseId": 10,
                    "status": "completed",
                    "conclusion": "success",
                    "displayTitle": "Build MSI Release v1.16.0",
                },
            )
        return json.dumps(rows)

    monkeypatch.setattr(rel, "gh", fake_gh)
    assert (
        rel.wait_new_workflow_dispatch(
            "Build MSI Release", known_ids=set(), display_title="Build MSI Release v1.16.0"
        )
        == "success"
    )
    assert calls["n"] == 2


def test_wait_pr_checks_reports_progress_and_passes(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        _ok([], json.dumps([{"name": "ci", "state": "IN_PROGRESS", "bucket": "pending"}])),
        _ok([], json.dumps([{"name": "ci", "state": "SUCCESS", "bucket": "pass"}])),
    ]
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(rel.subprocess, "run", fake_run)
    rel.wait_pr_checks(11, "Midtown-Technology-Group/mtg-winget", timeout_s=45)
    assert len(calls) == 2
    assert all("--watch" not in args and kw["timeout"] <= 30 for args, kw in calls)
    lines = _lines(capsys)
    assert all(STAMP.match(line) for line in lines)
    assert any("pending" in line for line in lines)
    assert "checks passed" in lines[-1]


def test_wait_pr_checks_timeout_is_bounded_and_visible(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def pending(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        return subprocess.CompletedProcess(
            args, 8, json.dumps([{"name": "ci", "bucket": "pending"}]), ""
        )

    monkeypatch.setattr(rel.subprocess, "run", pending)
    with pytest.raises(SystemExit, match="timeout waiting for PR #11 checks"):
        rel.wait_pr_checks(11, "Midtown-Technology-Group/mtg-winget", timeout_s=25)
    assert 1 <= calls["n"] <= 5
    assert any("pending" in line for line in _lines(capsys))


def test_wait_pr_checks_waits_for_initial_check_publication(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        subprocess.CompletedProcess([], 1, "", "no checks reported on the branch"),
        _ok([], json.dumps([{"name": "ci", "state": "SUCCESS", "bucket": "pass"}])),
    ]
    monkeypatch.setattr(rel.subprocess, "run", lambda args, **kwargs: responses.pop(0))
    rel.wait_pr_checks(11, "Midtown-Technology-Group/mtg-winget", timeout_s=45)
    assert not responses
    assert any("no checks reported yet" in line for line in _lines(capsys))
    assert clock.sleeps


def test_find_winget_pr_skips_unrelated_titles(
    clock: FakeClock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    prs = [
        {"number": 3, "title": "Update halocli to 9.9.9", "headRefName": "other"},
        {"number": 4, "title": "Update halocli to 1.16.0", "headRefName": "auto-1.16.0"},
    ]
    monkeypatch.setattr(rel, "find_winget_pr_via_git", lambda v: None)
    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: json.dumps(prs))
    found = rel.find_winget_pr("1.16.0")
    assert found["number"] == 4
    lines = _lines(capsys)
    assert all(STAMP.match(line) for line in lines), lines
    assert any("#4" in line for line in lines)


def test_find_winget_pr_reports_already_merged(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        if "merged" in args:
            return json.dumps([{"number": 4, "title": "Update halocli to 1.16.0"}])
        return "[]"

    monkeypatch.setattr(rel, "find_winget_pr_via_git", lambda v: None)
    monkeypatch.setattr(rel, "gh", fake_gh)
    with pytest.raises(SystemExit, match="already merged.*--verify-existing"):
        rel.find_winget_pr("1.16.0")


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
    """API fallback: still the authoritative reader when the URL probe is unavailable."""
    monkeypatch.setattr(
        rel, "gh", lambda args, *, cwd=None: json.dumps({"assets": [{"name": "halocli.msi"}]})
    )
    assert rel.release_has_msi_via_api("1.16.0") is True

    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: json.dumps({"assets": []}))
    assert rel.release_has_msi_via_api("1.16.0") is False

    def missing(args: list[str], *, cwd: Any = None) -> str:
        raise SystemExit("FAILED (1): gh release view v1.16.0\nrelease not found")

    monkeypatch.setattr(rel, "gh", missing)
    assert rel.release_has_msi_via_api("1.16.0") is False

    def broken(args: list[str], *, cwd: Any = None) -> str:
        raise SystemExit("FAILED (1): gh release view\nconnection reset by peer")

    monkeypatch.setattr(rel, "gh", broken)
    with pytest.raises(SystemExit, match="connection reset"):
        rel.release_has_msi_via_api("1.16.0")


def _stub_dance(monkeypatch: pytest.MonkeyPatch, calls: list[str], *, msi_attached: bool) -> None:
    """Replace every main() step with a recorder; nothing may touch the network."""
    monkeypatch.setattr(rel, "preflight", lambda version, strict=True: [])
    monkeypatch.setattr(rel, "ensure_release_tag", lambda version: calls.append("tag") or "reused")
    monkeypatch.setattr(rel, "ensure_tag_pushed", lambda tag: calls.append("push") or "pushed")
    monkeypatch.setattr(rel, "wait_workflow", lambda *a, **k: calls.append("wait") or "success")
    monkeypatch.setattr(rel, "release_has_msi", lambda version: msi_attached)
    monkeypatch.setattr(rel, "msi_workflow_runs", lambda: [])
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


def test_msi_stage_reuses_only_matching_active_run(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rel, "release_has_msi", lambda version: False)
    monkeypatch.setattr(
        rel,
        "msi_workflow_runs",
        lambda: [
            {
                "databaseId": 9,
                "status": "in_progress",
                "conclusion": None,
                "displayTitle": "Build MSI Release v1.16.0",
            },
            {
                "databaseId": 8,
                "status": "in_progress",
                "conclusion": None,
                "displayTitle": "Build MSI Release v9.9.9",
            },
        ],
    )
    seen: list[list[str]] = []

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        seen.append(args)
        return json.dumps({"status": "completed", "conclusion": "success"})

    monkeypatch.setattr(rel, "gh", fake_gh)
    rel.build_msi_stage("1.16.0")
    assert seen == [["run", "view", "9", "--json", "status,conclusion"]]


def test_msi_stage_blocks_ambiguous_active_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel, "release_has_msi", lambda version: False)
    monkeypatch.setattr(
        rel,
        "msi_workflow_runs",
        lambda: [
            {
                "databaseId": 7,
                "status": "in_progress",
                "conclusion": None,
                "displayTitle": "Build MSI Release",
            },
        ],
    )
    with pytest.raises(SystemExit, match="without version identity"):
        rel.build_msi_stage("1.16.0")


def test_msi_stage_reconciles_lost_dispatch_response(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rel, "release_has_msi", lambda version: False)
    rows = iter(
        [
            [],
            [
                {
                    "databaseId": 9,
                    "status": "in_progress",
                    "conclusion": None,
                    "displayTitle": "Build MSI Release v1.16.0",
                }
            ],
        ]
    )
    monkeypatch.setattr(rel, "msi_workflow_runs", lambda: next(rows))
    seen: list[list[str]] = []

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        seen.append(args)
        if args[0] == "workflow":
            raise SystemExit("network lost")
        return json.dumps({"status": "completed", "conclusion": "success"})

    monkeypatch.setattr(rel, "gh", fake_gh)
    rel.build_msi_stage("1.16.0")
    assert len([a for a in seen if a[0] == "workflow"]) == 1
    assert ["run", "view", "9", "--json", "status,conclusion"] in seen


def test_msi_stage_stops_when_dispatch_outcome_cannot_be_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rel, "release_has_msi", lambda version: False)
    monkeypatch.setattr(rel, "msi_workflow_runs", lambda: [])
    dispatched = {"n": 0}

    def lost(args: list[str], *, cwd: Any = None) -> str:
        dispatched["n"] += 1
        raise SystemExit("network lost")

    monkeypatch.setattr(rel, "gh", lost)
    with pytest.raises(SystemExit, match="outcome uncertain.*inspect workflow runs"):
        rel.build_msi_stage("1.16.0")
    assert dispatched["n"] == 1


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


def test_find_winget_pr_queries_the_winget_repository(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR lives in mtg-winget; gh must run there, not in halocli.

    Regression: the lookup ran without cwd, so gh resolved the halocli repository
    from the working directory and never saw the winget PR - the 1.19.0 dance
    waited the full 3600s while PR #55 sat open.
    """
    seen: list[Any] = []

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        seen.append(cwd)
        if "open" in args:
            return json.dumps(
                [{"number": 55, "title": "Update halocli to 1.19.0", "headRefName": "auto-1.19.0"}]
            )
        return "[]"

    monkeypatch.setattr(rel, "find_winget_pr_via_git", lambda v: None)
    monkeypatch.setattr(rel, "gh", fake_gh)
    found = rel.find_winget_pr("1.19.0")

    assert found["number"] == 55
    assert seen and all(cwd == rel.WINGET for cwd in seen), seen


def test_feed_versions_treats_401_as_requires_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The feed is Entra-protected; an anonymous 401 is a skip, not a failure."""
    import urllib.error

    def fake_urlopen(url: str, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert rel.feed_versions() is None
    assert any("requires authentication" in line for line in _lines(capsys))


def test_feed_versions_returns_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"Data": {"Versions": [{"PackageVersion": "1.19.0"}]}}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: FakeResponse())
    assert rel.feed_versions() == ["1.19.0"]


def test_feed_versions_raises_on_other_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(url: str, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit, match="feed check failed"):
        rel.feed_versions()


def test_pipx_install_accepts_the_version_prefix(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`halocli --version` prints "halocli 1.19.0"; that install is a success."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> str:
        calls.append(cmd)
        if cmd[:2] == ["halocli", "--version"]:
            return "halocli 1.19.0"
        return ""

    monkeypatch.setattr(rel, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlretrieve", lambda url, path: None)
    rel.pipx_install("1.19.0")

    assert ["halocli", "--version"] in calls


_SHA = "a" * 64
_PC = "{20252E2C-CC71-4390-A1A4-9B048BFAA8D8}"


def _manifest(sha: str = _SHA, pc: str = _PC) -> str:
    return f"InstallerSha256: {sha}\nProductCode: '{pc}'\n"


def _patch_merge_seams(
    monkeypatch: pytest.MonkeyPatch, *, feed: Any, conclusion: str = "success"
) -> list[list[str]]:
    merged: list[list[str]] = []

    def fake_gh(args: list[str], *, cwd: Any = None) -> str:
        if args[:3] == ["pr", "list", "--state"]:
            return json.dumps([{"number": 55, "title": "Update halocli to 1.19.0"}])
        merged.append(args)
        return ""

    monkeypatch.setattr(rel, "WINGET", rel.REPO)
    monkeypatch.setattr(rel, "gh", fake_gh)
    monkeypatch.setattr(rel, "wait_workflow", lambda *a, **k: conclusion)
    monkeypatch.setattr(rel, "feed_versions", lambda **k: feed)
    monkeypatch.setattr(
        rel,
        "time",
        type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0.0)}),
    )
    return merged


def test_merge_skips_feed_check_when_it_requires_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 401 feed must not fail a release whose winget PR merged and deployed."""
    merged = _patch_merge_seams(monkeypatch, feed=None)
    rel.merge_winget_and_verify_feed("1.19.0")

    assert merged and merged[0][:2] == ["pr", "merge"]
    assert any("feed check skipped" in line for line in _lines(capsys))


def test_merge_confirms_when_the_feed_serves_the_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_merge_seams(monkeypatch, feed=["1.18.0", "1.19.0"])
    rel.merge_winget_and_verify_feed("1.19.0")
    assert any("feed serves 1.19.0" in line for line in _lines(capsys))


def test_merge_fails_when_the_feed_serves_versions_without_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_merge_seams(monkeypatch, feed=["1.18.0"])
    with pytest.raises(SystemExit, match="feed never served 1.19.0"):
        rel.merge_winget_and_verify_feed("1.19.0")


def test_merge_fails_when_the_feed_deploy_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_merge_seams(monkeypatch, feed=["1.19.0"], conclusion="failure")
    with pytest.raises(SystemExit, match="feed deploy finished failure"):
        rel.merge_winget_and_verify_feed("1.19.0")


def test_merge_fails_when_the_pr_vanished(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel, "gh", lambda args, *, cwd=None: "[]")
    with pytest.raises(SystemExit, match="winget PR vanished"):
        rel.merge_winget_and_verify_feed("1.19.0")


def test_verify_existing_passes_without_a_feed_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rehearsal must still work when the feed is auth-protected (ok_feed=None)."""
    monkeypatch.setattr(rel, "verify_release_assets", lambda v: (_SHA, _PC))
    monkeypatch.setattr(rel, "run", lambda cmd, **kw: _manifest())
    monkeypatch.setattr(rel, "feed_versions", lambda **k: None)
    rel.verify_existing("1.19.0")
    assert any("feed_serves=skipped (auth required)" in line for line in _lines(capsys))


def test_verify_existing_fails_on_a_manifest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel, "verify_release_assets", lambda v: (_SHA, _PC))
    monkeypatch.setattr(rel, "run", lambda cmd, **kw: _manifest(sha="b" * 64))
    monkeypatch.setattr(rel, "feed_versions", lambda **k: ["1.19.0"])
    with pytest.raises(SystemExit, match="rehearsal FAILED"):
        rel.verify_existing("1.19.0")


def test_verify_existing_fails_when_the_feed_lacks_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rel, "verify_release_assets", lambda v: (_SHA, _PC))
    monkeypatch.setattr(rel, "run", lambda cmd, **kw: _manifest())
    monkeypatch.setattr(rel, "feed_versions", lambda **k: ["1.18.0"])
    with pytest.raises(SystemExit, match="rehearsal FAILED"):
        rel.verify_existing("1.19.0")


def test_find_winget_pr_via_git_uses_branch_and_pull_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery must cost no API quota: branch by prefix, number by pull ref."""
    sha = "a" * 40
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> str:
        calls.append(cmd)
        if any("refs/heads/automation/update-halocli-1.19.1-*" in c for c in cmd):
            return f"{sha}\trefs/heads/automation/update-halocli-1.19.1-40351958940"
        if any("refs/pull/*/head" in c for c in cmd):
            return f"{sha}\trefs/pull/56/head\n{'b' * 40}\trefs/pull/55/head"
        return ""

    monkeypatch.setattr(rel, "run", fake_run)
    found = rel.find_winget_pr_via_git("1.19.1")
    assert found is not None
    assert found["number"] == 56
    assert found["branch"] == "automation/update-halocli-1.19.1-40351958940"
    assert found["discovered_via"] == "git"
    assert all("ls-remote" in " ".join(c) for c in calls)


def test_find_winget_pr_via_git_returns_none_without_the_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rel, "run", lambda cmd, **kw: "")
    assert rel.find_winget_pr_via_git("9.9.9") is None


def test_release_has_msi_uses_the_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Ok:
        status = 200

        def __enter__(self) -> "Ok":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        seen.append(request.full_url)
        return Ok()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert rel.release_has_msi("1.19.1") is True
    assert seen and seen[0].endswith("/v1.19.1/halocli.msi")


def test_release_has_msi_reports_false_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert rel.release_has_msi("0.0.0") is False
