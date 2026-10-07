#!/usr/bin/env python3
"""The release dance, scripted: tag -> release -> MSI -> winget -> feed -> pipx.

Replaces the15 manual steps that previously took a full session (and one
hex-transcription near-miss). Every verification is machine-checked: MSI
SHA256+ProductCode are compared against the winget manifest before any
push, the winget PR must carry a good signature, and the feed API must
serve the version before pipx installs it.

    python scripts/release.py 1.14.0 --dry-run        # preflight + plan only
    python scripts/release.py 1.14.0 --verify-existing # rehearse checks on an existing release
    python scripts/release.py 1.14.0 --yes             # run the dance

Steps refuse to continue on any mismatch. --skip-pipx omits the local
install; --skip-tests skips the local gate run (CI still gates the tag).

Tag, tag push and MSI stages can resume with the same arguments. A merged
winget PR still requires --verify-existing rather than replaying the dance.
Every wait prints timestamped progress, and a tag or artifact that does not
match the requested version aborts before any mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINGET = REPO.parent / "mtg-winget"
FEED_URL = "https://winget.midtowntg.com/api/packageManifests/MidtownTechnologyGroup.Halocli"
RELEASE_URL = "https://github.com/Midtown-Technology-Group/halocli/releases/download"
RESTORE_BRANCH = "codex/fix-swa-deploy-auth-sku"  # dance returns the winget repo here


def utc_now() -> str:
    """Current UTC time as `YYYY-MM-DDTHH:MM:SSZ` for progress lines."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    """Print a timestamped progress line (waits use this, never bare print)."""
    print(f"[{utc_now()}] {message}", flush=True)


def _elapsed(started: float) -> str:
    return f"{int(time.time() - started)}s elapsed"


def run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = True
) -> str:
    """Run a command, optionally capturing stdout; raise on unexpected failure."""
    proc = subprocess.run(
        cmd,
        cwd=cwd or REPO,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise SystemExit(
            f"FAILED ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}"
        )
    return (proc.stdout or "").strip()


def gh(args: list[str], *, cwd: Path | None = None) -> str:
    return run(["gh", *args], cwd=cwd)


def wait_workflow(
    workflow: str, branch: str, *, timeout_s: int = 1800, cwd: Path | None = None
) -> str:
    """Poll until the newest matching workflow run completes; return conclusion.

    Already-completed runs return at once, so re-running after an interruption
    resumes the wait instead of starting new work.
    """
    started = time.time()
    deadline = started + timeout_s
    run_id = None
    log(f"waiting for workflow {workflow!r} on {branch!r} (timeout {timeout_s}s)")
    while time.time() < deadline:
        out = gh(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--branch",
                branch,
                "--limit",
                "1",
                "--json",
                "databaseId,status,conclusion",
            ],
            cwd=cwd,
        )
        rows = json.loads(out)
        if rows:
            run_id = rows[0]["databaseId"]
            if rows[0]["status"] == "completed":
                conclusion = rows[0]["conclusion"] or "unknown"
                log(f"workflow {workflow!r} run {run_id} finished: {conclusion}")
                return conclusion
            log(f"workflow {workflow!r} run {run_id}: {rows[0]['status']} ({_elapsed(started)})")
        else:
            log(f"workflow {workflow!r}: no runs yet ({_elapsed(started)})")
        time.sleep(20)
    raise SystemExit(f"timeout waiting for {workflow} on {branch} (run {run_id})")


def wait_pr_checks(pr: int, repo: str, *, timeout_s: int = 1800) -> None:
    """Poll checks with a wall-clock limit and visible progress."""
    started = time.time()
    deadline = started + timeout_s
    log(f"waiting for PR #{pr} checks in {repo} (timeout {timeout_s}s)")
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            proc = subprocess.run(
                ["gh", "pr", "checks", str(pr), "--repo", repo, "--json", "name,state,bucket"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(30, remaining),
            )
        except subprocess.TimeoutExpired:
            log(f"PR #{pr} check query timed out ({_elapsed(started)})")
            continue
        if proc.returncode not in {0, 1, 8}:
            raise SystemExit(f"PR #{pr} check query failed ({proc.returncode}): {proc.stderr}")
        try:
            checks = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise SystemExit(f"could not parse PR #{pr} checks: {proc.stdout[:200]!r}")
        failed = [c for c in checks if c["bucket"] == "fail"]
        if failed:
            raise SystemExit(f"PR #{pr} checks failed: {failed}")
        pending = [c for c in checks if c["bucket"] == "pending"]
        if proc.returncode == 1 and not pending:
            raise SystemExit(f"PR #{pr} check query failed: {proc.stderr or proc.stdout}")
        if checks and not pending and proc.returncode == 0:
            log(f"PR #{pr} checks passed")
            return
        log(f"PR #{pr} checks: {len(checks)} found, {len(pending)} pending ({_elapsed(started)})")
        time.sleep(min(20, max(0, deadline - time.time())))
    raise SystemExit(f"timeout waiting for PR #{pr} checks in {repo} after {timeout_s}s")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def product_code(msi: Path) -> str:
    out = run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO / "scripts" / "get_msi_product_code.ps1"),
            "-Path",
            str(msi),
        ]
    )
    code = out.strip().splitlines()[-1].strip()
    if not re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", code):
        raise SystemExit(f"unparsable ProductCode from MSI: {code!r}")
    return code.upper()


def pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "([^"]+)"', text, re.M)
    assert m, "version not found in pyproject.toml"
    return m.group(1)


def preflight(version: str, *, strict: bool = True) -> list[str]:
    """Check release preconditions; in strict mode any problem aborts."""
    problems: list[str] = []
    if pyproject_version() != version:
        problems.append(
            f"pyproject version is {pyproject_version()}, expected {version} "
            "(bump it and commit first)"
        )
    status = run(["git", "status", "--porcelain"])
    if status:
        problems.append(f"halocli working tree not clean:\n{status}")
    branch = run(["git", "branch", "--show-current"])
    if branch != "main":
        problems.append(f"release must run from main (on {branch!r})")
    if problems and strict:
        raise SystemExit("preflight failed:\n- " + "\n- ".join(problems))
    if not problems:
        log(f"preflight ok: main clean, version {version}")
    return problems


def local_gates() -> None:
    py = str(REPO / ".venv" / "Scripts" / "python.exe")
    run([py, "-m", "ruff", "check", "."])
    run([py, "-m", "ruff", "format", "--check", "."])
    run([py, "-m", "mypy", "src/halocli", "--ignore-missing-imports"])
    run([py, "scripts/build_coverage_ledger.py", "--check"])
    run([py, "-m", "pytest", "-q", "--no-header"])
    log("local gates ok (ruff, format, mypy, ledger, tests)")


def verify_release_assets(version: str) -> tuple[str, str]:
    """Download the release MSI; return (sha256, productcode) - the known goods."""
    import urllib.request

    msi = Path(f"C:/Users/ThomasBray/AppData/Local/Temp/opencode/release-{version}.msi")
    msi.parent.mkdir(parents=True, exist_ok=True)
    url = f"{RELEASE_URL}/v{version}/halocli.msi"
    log(f"downloading {url}")
    urllib.request.urlretrieve(url, msi)
    digest = sha256_file(msi)
    code = product_code(msi)
    log(f"release MSI: sha256={digest} productcode={code}")
    return digest, code


def find_winget_pr(version: str, *, timeout_s: int = 3600) -> dict:
    """Wait for an open winget-releaser PR; fail fast if it already merged."""
    started = time.time()
    deadline = started + timeout_s
    log(f"waiting for the winget-releaser PR for {version} (timeout {timeout_s}s)")
    while time.time() < deadline:
        out = gh(["pr", "list", "--state", "open", "--json", "number,title,headRefName"])
        for pr in json.loads(out):
            if pr["title"] == f"Update halocli to {version}":
                log(f"found winget PR #{pr['number']} ({pr['headRefName']})")
                return pr
        merged = json.loads(
            gh(["pr", "list", "--state", "merged", "--limit", "100", "--json", "number,title"])
        )
        for pr in merged:
            if pr["title"] == f"Update halocli to {version}":
                raise SystemExit(
                    f"winget PR #{pr['number']} for {version} already merged; "
                    "the dance cannot resume after this point. Use --verify-existing."
                )
        log(f"no winget PR for {version} yet ({_elapsed(started)})")
        time.sleep(15)
    raise SystemExit(f"no winget PR appeared for {version} within {timeout_s}s")


def winget_manifest_values(pr_branch: str, version: str) -> tuple[str, str]:
    """(sha256, productcode) declared by the automation PR's manifest."""
    run(["git", "fetch", "origin", pr_branch], cwd=WINGET)
    text = run(
        [
            "git",
            "show",
            f"origin/{pr_branch}:manifests/m/MidtownTechnologyGroup/Halocli/{version}.yaml",
        ],
        cwd=WINGET,
    )
    sha = re.search(r"InstallerSha256:\s*([0-9a-fA-F]{64})", text)
    pc = re.search(r"ProductCode:\s*'?(\{[0-9A-Fa-f-]{36}\})'?", text)
    if not sha or not pc:
        raise SystemExit(f"could not parse manifest from branch {pr_branch}:\n{text[:500]}")
    return sha.group(1).lower(), pc.group(1).upper()


def winget_dance(pr_number: int, pr_branch: str, version: str, sha: str, pc: str) -> None:
    """Re-commit the automation PR with our SSH signature (branch rules)."""
    run(["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"], cwd=WINGET)
    run(["git", "checkout", f"pr-{pr_number}"], cwd=WINGET)
    run(["git", "reset", "--soft", "origin/main"], cwd=WINGET)
    msg = Path(f"C:/Users/ThomasBray/AppData/Local/Temp/opencode/winget-{version}.txt")
    msg.write_text(
        f"Update halocli to {version}\n\n"
        f"Automated release: SHA256 {sha.lower()} and ProductCode {pc} "
        f"verified byte-exact against the v{version} release asset by "
        f"scripts/release.py.\n",
        encoding="utf-8",
    )
    run(["git", "commit", "-F", str(msg)], cwd=WINGET)
    sig = run(["git", "log", "-1", "--format=%G?"], cwd=WINGET)
    if sig not in {"G", "E", "U"}:  # G=good SSH sig (ours), E=expired-but-signed
        raise SystemExit(f"unexpected signature status after commit: {sig!r}")
    if sig == "U":
        raise SystemExit("commit is unsigned - branch protection would reject it")
    log(f"winget commit signed ({sig})")
    run(["git", "push", "-f", "origin", f"pr-{pr_number}:{pr_branch}"], cwd=WINGET)
    run(["git", "checkout", RESTORE_BRANCH], cwd=WINGET)
    log(f"force-pushed {pr_branch}; winget repo restored to {RESTORE_BRANCH}")


def merge_winget_and_verify_feed(version: str) -> None:
    out = gh(["pr", "list", "--state", "open", "--json", "number,title"], cwd=WINGET)
    prs = [p for p in json.loads(out) if f"Update halocli to {version}" in p["title"]]
    if not prs:
        raise SystemExit("winget PR vanished before merge")
    gh(["pr", "merge", str(prs[0]["number"]), "--merge"], cwd=WINGET)
    log(f"merged winget PR #{prs[0]['number']}")
    conclusion = wait_workflow("deploy-feed.yml", "main", cwd=WINGET)
    if conclusion != "success":
        raise SystemExit(f"feed deploy finished {conclusion}")
    log("feed deploy: success")
    # the merge-push auto-deploys; give the CDN a moment, then verify the feed
    import urllib.request

    versions: list[str] = []
    for attempt in range(10):
        try:
            with urllib.request.urlopen(FEED_URL, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            versions = [v.get("PackageVersion") for v in data.get("Data", {}).get("Versions", [])]
            if version in versions:
                log(f"feed serves {version} (versions: {versions})")
                return
            log(f"feed poll {attempt}: {version} not served yet (versions: {versions})")
        except Exception as exc:  # noqa: BLE001
            log(f"feed poll {attempt}: {exc}")
        time.sleep(10)
    raise SystemExit(f"feed never served {version} (last versions: {versions})")


def pipx_install(version: str) -> None:
    import urllib.request

    wheel = Path(
        f"C:/Users/ThomasBray/AppData/Local/Temp/opencode/halocli-{version}-py3-none-any.whl"
    )
    wheel.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(
        f"{RELEASE_URL}/v{version}/halocli-{version}-py3-none-any.whl", wheel
    )
    run(["pipx", "uninstall", "halocli"])
    run(["pipx", "install", str(wheel)])
    run(["pipx", "runpip", "halocli", "install", "fastapi>=0.111.0", "uvicorn>=0.30.0"])
    run(
        [
            "pipx",
            "runpip",
            "halocli",
            "install",
            "mtg-microsoft-auth @ git+https://github.com/Midtown-Technology-Group/"
            "mtg-microsoft-auth.git@main",
        ]
    )
    shown = run(["halocli", "--version"])
    if shown != version:
        raise SystemExit(f"installed halocli reports {shown!r}, expected {version!r}")
    log(f"pipx: halocli {shown} installed with extras")


def verify_existing(version: str) -> None:
    """Rehearsal: run the artifact/manifest/feed verification chain (read-only)."""
    digest, code = verify_release_assets(version)
    # for merged releases, read the manifest from winget main instead
    text = run(["git", "fetch", "origin", "main"], cwd=WINGET) or run(
        ["git", "show", f"origin/main:manifests/m/MidtownTechnologyGroup/Halocli/{version}.yaml"],
        cwd=WINGET,
    )
    sha = re.search(r"InstallerSha256:\s*([0-9a-fA-F]{64})", text)
    pc = re.search(r"ProductCode:\s*'?(\{[0-9A-Fa-f-]{36}\})'?", text)
    assert sha and pc, "manifest not found on main"
    ok_sha = sha.group(1).lower() == digest.lower()
    ok_pc = pc.group(1).upper() == code.upper()
    import urllib.request

    with urllib.request.urlopen(FEED_URL, timeout=30) as resp:
        versions = [
            v.get("PackageVersion")
            for v in json.loads(resp.read().decode("utf-8")).get("Data", {}).get("Versions", [])
        ]
    ok_feed = version in versions
    log(f"rehearsal v{version}: sha_match={ok_sha} productcode_match={ok_pc} feed_serves={ok_feed}")
    if not (ok_sha and ok_pc and ok_feed):
        raise SystemExit("rehearsal FAILED")


def _follow_dispatched_run(workflow: str, run_id: int, deadline: float, started: float) -> str:
    """Poll one workflow run to completion; return its conclusion."""
    while time.time() < deadline:
        cur = json.loads(gh(["run", "view", str(run_id), "--json", "status,conclusion"]))
        if cur["status"] == "completed":
            conclusion = cur["conclusion"] or "unknown"
            log(f"workflow {workflow!r} run {run_id} finished: {conclusion}")
            return conclusion
        log(f"workflow {workflow!r} run {run_id}: {cur['status']} ({_elapsed(started)})")
        time.sleep(20)
    raise SystemExit(f"timeout waiting for run {run_id} of {workflow}")


def wait_new_workflow_dispatch(
    workflow: str, *, known_ids: set[int], display_title: str | None = None, timeout_s: int = 900
) -> str:
    """Wait for a run of `workflow` that was NOT in known_ids, then its conclusion."""
    started = time.time()
    deadline = started + timeout_s
    new_id = None
    log(f"waiting for a NEW run of {workflow!r} (timeout {timeout_s}s)")
    while time.time() < deadline:
        out = gh(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--limit",
                "100",
                "--json",
                "databaseId,status,conclusion,displayTitle",
            ]
        )
        rows = json.loads(out)
        fresh = [
            r
            for r in rows
            if r["databaseId"] not in known_ids
            and (display_title is None or r["displayTitle"] == display_title)
        ]
        if fresh:
            new_id = fresh[0]["databaseId"]
            if fresh[0]["status"] == "completed":
                conclusion = fresh[0]["conclusion"] or "unknown"
                log(f"workflow {workflow!r} run {new_id} already finished: {conclusion}")
                return conclusion
            log(f"workflow {workflow!r} new run {new_id}: {fresh[0]['status']}")
            return _follow_dispatched_run(workflow, new_id, deadline, started)
        log(f"workflow {workflow!r}: no new run yet ({_elapsed(started)})")
        time.sleep(10)
    raise SystemExit(f"timeout waiting for a NEW run of {workflow} (saw {new_id})")


def msi_workflow_runs() -> list[dict]:
    """Read recent MSI runs. An active run without a version is ambiguous."""
    out = gh(
        [
            "run",
            "list",
            "--workflow",
            "Build MSI Release",
            "--limit",
            "100",
            "--json",
            "databaseId,status,conclusion,displayTitle",
        ]
    )
    return json.loads(out)


def build_msi_stage(version: str) -> None:
    """Reuse a version-matched run or dispatch once; ambiguous state aborts."""
    if release_has_msi(version):
        log(f"release v{version} already carries halocli.msi: skipping MSI dispatch")
        return
    title = f"Build MSI Release v{version}"
    rows = msi_workflow_runs()
    matching = [r for r in rows if r["displayTitle"] == title]
    active = [r for r in matching if r["status"] != "completed"]
    if len(active) > 1:
        raise SystemExit(
            f"multiple active MSI runs for v{version}: {[r['databaseId'] for r in active]}"
        )
    if active:
        run_id = active[0]["databaseId"]
        log(f"Build MSI Release run {run_id} for v{version} already active: resuming wait")
        started = time.time()
        conclusion = _follow_dispatched_run("Build MSI Release", run_id, started + 900, started)
    else:
        # Old workflow runs have no version in their display title. Their
        # active outcome cannot be safely assigned to this release.
        unknown = [
            r
            for r in rows
            if r["status"] != "completed"
            and not r["displayTitle"].startswith("Build MSI Release v")
        ]
        if unknown:
            raise SystemExit(
                f"active MSI run(s) without version identity: {[r['databaseId'] for r in unknown]}"
            )
        if matching:
            latest = matching[0]
            raise SystemExit(
                f"MSI run {latest['databaseId']} for v{version} already concluded "
                f"{latest['conclusion']}, but the release has no halocli.msi; inspect it before redispatch"
            )
        known_ids = {r["databaseId"] for r in rows}
        try:
            gh(["workflow", "run", "Build MSI Release", "-f", f"version={version}"])
        except SystemExit as exc:
            # A lost dispatch response does not prove the run was rejected.
            observed = [
                r
                for r in msi_workflow_runs()
                if r["displayTitle"] == title and r["databaseId"] not in known_ids
            ]
            if len(observed) != 1 or observed[0]["status"] == "completed":
                raise SystemExit(
                    f"MSI dispatch outcome uncertain for v{version}; inspect workflow runs before retry: {exc}"
                ) from exc
            run_id = observed[0]["databaseId"]
            log(f"MSI dispatch response lost; following observed run {run_id} for v{version}")
            started = time.time()
            conclusion = _follow_dispatched_run("Build MSI Release", run_id, started + 900, started)
        else:
            log(f"Build MSI Release dispatched for v{version}")
            conclusion = wait_new_workflow_dispatch(
                "Build MSI Release", known_ids=known_ids, display_title=title
            )
    if conclusion != "success":
        raise SystemExit(f"MSI workflow concluded {conclusion}")
    log("MSI workflow: success")


def _git_ok(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """git that reports instead of aborting, so callers can probe state."""
    return subprocess.run(
        cmd,
        cwd=cwd or REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def local_tag_commit(tag: str) -> str | None:
    """SHA the local tag points at, or None when the tag does not exist."""
    proc = _git_ok(["git", "rev-parse", "--verify", f"refs/tags/{tag}"])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[0].strip()


def head_commit() -> str:
    return run(["git", "rev-parse", "HEAD"])


def remote_tag_commit(tag: str) -> str | None:
    """SHA the origin tag points at, or None when origin has no such tag."""
    out = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/tags/{tag}" and re.fullmatch(r"[0-9a-f]{40}", sha.strip()):
            return sha.strip()
    return None


def tag_pyproject_version(tag: str) -> str | None:
    """pyproject version committed under `tag` (artifact identity of the tag)."""
    proc = _git_ok(["git", "show", f"{tag}:pyproject.toml"])
    if proc.returncode != 0:
        return None
    m = re.search(r'^version = "([^"]+)"', proc.stdout, re.M)
    return m.group(1) if m else None


def ensure_release_tag(version: str) -> str:
    """Create `v{version}` on HEAD, or reuse it after identity verification.

    Returns "created" or "reused". A tag that already exists but points
    anywhere other than HEAD, or whose tree declares a different version,
    aborts: re-running must never silently adopt a foreign or stale tag.
    """
    tag = f"v{version}"
    existing = local_tag_commit(tag)
    head = head_commit()
    if existing is None:
        run(["git", "-c", "tag.gpgsign=false", "tag", tag])
        log(f"tag {tag} created on {head}")
        return "created"
    if existing != head:
        raise SystemExit(
            f"tag {tag} already exists on {existing}, not HEAD ({head}): "
            "refusing to reuse a tag that does not match this checkout"
        )
    tagged_version = tag_pyproject_version(tag)
    if tagged_version != version:
        raise SystemExit(
            f"tag {tag} declares pyproject version {tagged_version!r}, "
            f"not {version!r}: refusing to release from a mismatched tag"
        )
    log(f"tag {tag} already on HEAD with matching version: reusing")
    return "reused"


def ensure_tag_pushed(tag: str) -> str:
    """Push `tag` unless origin already has it on the same commit.

    Returns "pushed" or "already-pushed". An origin tag on a different
    commit aborts: force-pushing a release tag would repoint a published
    release.
    """
    local = local_tag_commit(tag)
    remote = remote_tag_commit(tag)
    if remote is None:
        run(["git", "push", "origin", tag])
        log(f"tag {tag} pushed")
        return "pushed"
    if remote != local:
        raise SystemExit(
            f"origin tag {tag} points at {remote}, local tag at {local}: "
            "refusing to move a published release tag"
        )
    log(f"tag {tag} already on origin at the same commit: skipping push")
    return "already-pushed"


def release_has_msi(version: str) -> bool:
    """True when release `v{version}` already carries the `halocli.msi` asset.

    Read-only: lets a resumed run skip dispatching a duplicate MSI build.
    A missing release (fresh run) reports False; other `gh` failures abort
    rather than guessing, so a transient error can never skip the build.
    """
    try:
        out = gh(["release", "view", f"v{version}", "--json", "assets"])
    except SystemExit as exc:
        message = str(exc).lower()
        if "not found" in message or "404" in message or "no release" in message:
            return False
        raise
    try:
        assets = json.loads(out).get("assets", [])
    except json.JSONDecodeError:
        raise SystemExit(f"could not parse assets for release v{version}: {out[:200]!r}")
    return any(a.get("name") == "halocli.msi" for a in assets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="X.Y.Z (must match pyproject for a real run)")
    parser.add_argument("--dry-run", action="store_true", help="preflight + plan only")
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="rehearse verifications against an already-released version",
    )
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-pipx", action="store_true")
    parser.add_argument("--yes", action="store_true", help="actually run the dance")
    args = parser.parse_args()
    version = args.version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"version must be X.Y.Z, got {version!r}")

    if args.verify_existing:
        verify_existing(version)
        return 0

    problems = preflight(version, strict=not (args.dry_run or not args.yes))
    for problem in problems:
        print(f"PREFLIGHT WARNING (would block a real run): {problem}")
    plan = [
        "1. local gates (ruff, format, mypy, ledger, pytest)",
        f"2. tag v{version} (created, or verified-reused on resume) + push (or skipped)",
        "3. watch Release workflow",
        "4. dispatch Build MSI Release unless halocli.msi is attached (resume); watch it",
        "5. download release MSI; compute SHA256 + ProductCode",
        "6. wait for the winget-releaser PR; byte-exact verify its manifest",
        "7. signed re-commit dance + force-push + winget PR checks + merge",
        "8. watch Deploy Winget Feed; verify the feed API serves the version",
        "9. pipx install wheel + extras; verify `halocli --version`",
    ]
    print("PLAN:")
    for step in plan:
        print(f"  {step}")
    if args.dry_run or not args.yes:
        print("dry-run: stopping before any mutation (pass --yes to run)")
        return 0

    if not args.skip_tests:
        local_gates()

    ensure_release_tag(version)
    ensure_tag_pushed(f"v{version}")
    conclusion = wait_workflow("Release", f"v{version}")
    if conclusion != "success":
        raise SystemExit(f"Release workflow concluded {conclusion}")
    log("Release workflow: success")

    build_msi_stage(version)

    digest, code = verify_release_assets(version)

    pr = find_winget_pr(version)
    log(f"winget PR #{pr['number']} ({pr['headRefName']})")
    manifest_sha, manifest_pc = winget_manifest_values(pr["headRefName"], version)
    if manifest_sha != digest.lower() or manifest_pc != code.upper():
        raise SystemExit(
            "MANIFEST MISMATCH - aborting before any push:\n"
            f"  manifest sha {manifest_sha} vs release {digest.lower()}\n"
            f"  manifest pc  {manifest_pc} vs release {code.upper()}"
        )
    log("manifest byte-exact match")

    winget_dance(pr["number"], pr["headRefName"], version, digest, code)
    wait_pr_checks(pr["number"], "Midtown-Technology-Group/mtg-winget")
    merge_winget_and_verify_feed(version)

    if not args.skip_pipx:
        pipx_install(version)
    log(f"RELEASE {version} COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
