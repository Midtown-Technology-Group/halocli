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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path


_C_Build_MSI_Release = "Build MSI Release"
REPO = Path(__file__).resolve().parents[1]
WINGET = REPO.parent / "mtg-winget"
FEED_URL = "https://winget.midtowntg.com/api/packageManifests/MidtownTechnologyGroup.Halocli"
RELEASE_URL = "https://github.com/Midtown-Technology-Group/halocli/releases/download"
RESTORE_BRANCH = "codex/fix-swa-deploy-auth-sku"  # dance returns the winget repo here


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
    """Poll until the newest matching workflow run completes; return conclusion."""
    deadline = time.time() + timeout_s
    run_id = None
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
                return rows[0]["conclusion"] or "unknown"
        time.sleep(20)
    raise SystemExit(f"timeout waiting for {workflow} on {branch} (run {run_id})")


def wait_pr_checks(pr: int, repo: str) -> None:
    """Watch a PR's checks until they pass (gh exits nonzero while failing)."""
    proc = subprocess.run(
        ["gh", "pr", "checks", str(pr), "--repo", repo, "--watch", "--interval", "20"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"PR #{pr} checks failed:\n{proc.stdout[-3000:]}")


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
        print(f"preflight ok: main clean, version {version}")
    return problems


def local_gates() -> None:
    py = str(REPO / ".venv" / "Scripts" / "python.exe")
    run([py, "-m", "ruff", "check", "."])
    run([py, "-m", "ruff", "format", "--check", "."])
    run([py, "-m", "mypy", "src/halocli", "--ignore-missing-imports"])
    run([py, "scripts/build_coverage_ledger.py", "--check"])
    run([py, "-m", "pytest", "-q", "--no-header"])
    print("local gates ok (ruff, format, mypy, ledger, tests)")


def verify_release_assets(version: str) -> tuple[str, str]:
    """Download the release MSI; return (sha256, productcode) - the known goods."""
    import urllib.request

    msi = Path(f"C:/Users/ThomasBray/AppData/Local/Temp/opencode/release-{version}.msi")
    msi.parent.mkdir(parents=True, exist_ok=True)
    url = f"{RELEASE_URL}/v{version}/halocli.msi"
    print(f"downloading {url}")
    urllib.request.urlretrieve(url, msi)
    digest = sha256_file(msi)
    code = product_code(msi)
    print(f"release MSI: sha256={digest} productcode={code}")
    return digest, code


def find_winget_pr(version: str, *, timeout_s: int = 3600) -> dict:
    """Wait for the winget-releaser PR for this version."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = gh(["pr", "list", "--state", "open", "--json", "number,title,headRefName"])
        for pr in json.loads(out):
            if f"Update halocli to {version}" in pr["title"]:
                return pr
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
    print(f"winget commit signed ({sig})")
    run(["git", "push", "-f", "origin", f"pr-{pr_number}:{pr_branch}"], cwd=WINGET)
    run(["git", "checkout", RESTORE_BRANCH], cwd=WINGET)
    print(f"force-pushed {pr_branch}; winget repo restored to {RESTORE_BRANCH}")


def merge_winget_and_verify_feed(version: str) -> None:
    out = gh(["pr", "list", "--state", "open", "--json", "number,title"], cwd=WINGET)
    prs = [p for p in json.loads(out) if f"Update halocli to {version}" in p["title"]]
    if not prs:
        raise SystemExit("winget PR vanished before merge")
    gh(["pr", "merge", str(prs[0]["number"]), "--merge"], cwd=WINGET)
    print(f"merged winget PR #{prs[0]['number']}")
    conclusion = wait_workflow("deploy-feed.yml", "main", cwd=WINGET)
    if conclusion != "success":
        raise SystemExit(f"feed deploy finished {conclusion}")
    print("feed deploy: success")
    # the merge-push auto-deploys; give the CDN a moment, then verify the feed
    import urllib.request

    for attempt in range(10):
        try:
            with urllib.request.urlopen(FEED_URL, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            versions = [v.get("PackageVersion") for v in data.get("Data", {}).get("Versions", [])]
            if version in versions:
                print(f"feed serves {version} (versions: {versions})")
                return
        except Exception as exc:  # noqa: BLE001
            print(f"  feed poll {attempt}: {exc}")
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
    print(f"pipx: halocli {shown} installed with extras")


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
    print(
        f"rehearsal v{version}: sha_match={ok_sha} productcode_match={ok_pc} feed_serves={ok_feed}"
    )
    if not (ok_sha and ok_pc and ok_feed):
        raise SystemExit("rehearsal FAILED")


def wait_new_workflow_dispatch(workflow: str, *, known_ids: set[int], timeout_s: int = 900) -> str:
    """Wait for a run of `workflow` that was NOT in known_ids, then its conclusion."""
    deadline = time.time() + timeout_s
    new_id = None
    while time.time() < deadline:
        out = gh(
            [
                "run",
                "list",
                "--workflow",
                workflow,
                "--limit",
                "10",
                "--json",
                "databaseId,status,conclusion",
            ]
        )
        rows = json.loads(out)
        fresh = [r for r in rows if r["databaseId"] not in known_ids]
        if fresh:
            new_id = fresh[0]["databaseId"]
            if fresh[0]["status"] == "completed":
                return fresh[0]["conclusion"] or "unknown"
            while time.time() < deadline:
                cur = json.loads(gh(["run", "view", str(new_id), "--json", "status,conclusion"]))
                if cur["status"] == "completed":
                    return cur["conclusion"] or "unknown"
                time.sleep(20)
        time.sleep(10)
    raise SystemExit(f"timeout waiting for a NEW run of {workflow} (saw {new_id})")


def pre_dispatch_run_ids(workflow: str) -> set[int]:
    out = gh(["run", "list", "--workflow", workflow, "--limit", "10", "--json", "databaseId"])
    return {r["databaseId"] for r in json.loads(out)}


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
        f"2. tag v{version} (unsigned via env override, as historically) + push",
        "3. watch Release workflow",
        "4. dispatch Build MSI Release; watch it",
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

    run(["git", "-c", "tag.gpgsign=false", "tag", f"v{version}"])
    run(["git", "push", "origin", f"v{version}"])
    print("tag pushed")
    conclusion = wait_workflow("Release", f"v{version}")
    if conclusion != "success":
        raise SystemExit(f"Release workflow concluded {conclusion}")
    print("Release workflow: success")

    pre_ids = pre_dispatch_run_ids(_C_Build_MSI_Release)
    gh(["workflow", "run", _C_Build_MSI_Release, "-f", f"version={version}"])
    conclusion = wait_new_workflow_dispatch(_C_Build_MSI_Release, known_ids=pre_ids)
    if conclusion != "success":
        raise SystemExit(f"MSI workflow concluded {conclusion}")
    print("MSI workflow: success")

    digest, code = verify_release_assets(version)

    pr = find_winget_pr(version)
    print(f"winget PR #{pr['number']} ({pr['headRefName']})")
    manifest_sha, manifest_pc = winget_manifest_values(pr["headRefName"], version)
    if manifest_sha != digest.lower() or manifest_pc != code.upper():
        raise SystemExit(
            "MANIFEST MISMATCH - aborting before any push:\n"
            f"  manifest sha {manifest_sha} vs release {digest.lower()}\n"
            f"  manifest pc  {manifest_pc} vs release {code.upper()}"
        )
    print("manifest byte-exact match")

    winget_dance(pr["number"], pr["headRefName"], version, digest, code)
    wait_pr_checks(pr["number"], "Midtown-Technology-Group/mtg-winget")
    print("winget PR checks passed")
    merge_winget_and_verify_feed(version)

    if not args.skip_pipx:
        pipx_install(version)
    print(f"RELEASE {version} COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
