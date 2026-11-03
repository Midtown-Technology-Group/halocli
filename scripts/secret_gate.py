"""Fail-closed ggshield gate for committed PR history or Git pre-push input."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MINIMUM_PYTHON = (3, 11)


class GateError(Exception):
    """A safe, classified publication-blocking failure."""

    def __init__(self, code, message):
        """Retain the exit classification and a value-free diagnostic."""
        self.code = code
        self.message = message


def checkout_owned(path):
    """Reject code from any checkout, including sibling linked worktrees."""
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def command_environment():
    """Bind children to the current checkout and approved import directories."""
    env = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
    ):
        env.pop(name, None)
    if "PYTHONPATH" in env:
        paths = []
        for entry in env["PYTHONPATH"].split(os.pathsep):
            path = Path(entry)
            if path.is_absolute() and not checkout_owned(path.resolve()):
                paths.append(str(path.resolve()))
        if paths:
            env["PYTHONPATH"] = os.pathsep.join(paths)
        else:
            env.pop("PYTHONPATH", None)
    env["PYTHONSAFEPATH"] = "1"
    return env


def executable(name):
    """Resolve approved tools outside the checkout, including on Windows."""
    if name not in {"git", "ggshield"}:
        return None
    cwd = Path.cwd().resolve()
    root = next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), cwd)
    os.environ["NODEFAULTCURRENTDIRECTORYINEXEPATH"] = "1"
    directories = []
    for entry in os.get_exec_path():
        directory = Path(entry)
        if (
            directory.is_absolute()
            and not directory.resolve().is_relative_to(root)
            and not checkout_owned(directory.resolve())
        ):
            directories.append(str(directory.resolve()))
    os.environ["PATH"] = os.pathsep.join(directories)
    found = shutil.which(name, path=os.environ["PATH"])
    if found:
        resolved = Path(found).resolve()
        if not resolved.is_relative_to(root) and not checkout_owned(resolved):
            return str(resolved)
    return None


def git(*args):
    """Run Git without interactive authentication or replacement objects."""
    if not args or not all(re.fullmatch(r"[\w./@{}^~+:=-]+", arg) for arg in args):
        raise GateError(20, "BASE_ERROR: invalid Git argument.")
    tool = executable("git")
    if not tool:
        raise GateError(21, "UNAVAILABLE: install approved Git outside the checkout.")
    env = dict(
        command_environment(),
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="Never",
    )
    result = subprocess.run(
        [tool, *args], check=False, env=env, capture_output=True, text=True, timeout=90
    )
    if result.returncode:
        raise GateError(
            20,
            "BASE_ERROR: Git operation failed; check refs/access and fetch the base.",
        )
    return result.stdout.strip()


def commit(ref):
    """Resolve a ref to an immutable commit before selecting history."""
    if not re.fullmatch(r"(?!-)[\w./@{}^~+=-]+", ref):
        raise GateError(20, "BASE_ERROR: invalid commit reference.")
    sha = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise GateError(20, "BASE_ERROR: reference did not resolve to one commit.")
    return sha


def remote_base(remote):
    """Resolve the configured remote default tip without guessing a branch."""
    if not re.fullmatch(r"(?!-)[\w./@-]+", remote):
        raise GateError(20, "BASE_ERROR: invalid remote name.")
    if remote not in git("remote").splitlines():
        raise GateError(20, "BASE_ERROR: select a configured remote or an explicit --base.")
    for line in git("ls-remote", "--symref", "--", remote, "HEAD").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "HEAD" and re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
            return commit(parts[0])
    raise GateError(
        20,
        "BASE_ERROR: no remote default; supply --base or --all-history.",
    )


def ranges(args, stream, head):
    """Select complete PR or pre-push commit ranges."""
    grafts = Path(git("rev-parse", "--git-common-dir")) / "info/grafts"
    if (grafts.exists() and grafts.stat().st_size) or os.environ.get("GIT_GRAFT_FILE"):
        raise GateError(20, "BASE_ERROR: grafted history is unsupported; restore complete history.")
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise GateError(20, "BASE_ERROR: shallow history; fetch --unshallow before scanning.")
    if not args.pre_push:
        base = commit(args.base) if args.base else None
        if not base and not args.all_history:
            base = remote_base(args.remote)
        return [f"{base}..{head}" if base else head]
    result = []
    for line in stream:
        revision = push_range(line)
        if revision:
            result.append(revision)
    return list(dict.fromkeys(result))


def push_range(line):
    """Parse one complete Git pre-push update into its outgoing range."""
    fields = line.split()
    if len(fields) != 4 or not all(re.fullmatch(r"[0-9a-f]{40,64}", fields[i]) for i in (1, 3)):
        raise GateError(20, "BASE_ERROR: malformed Git pre-push input.")
    _, local, _, old = fields
    if set(local) == {"0"}:
        return None
    if git("cat-file", "-t", local) == "tag":
        raise GateError(
            20,
            "BASE_ERROR: annotated tags require an approved annotation scan; publication blocked.",
        )
    head = commit(local)
    # New refs include root history, without a hook commit limit or HEAD~1 guess.
    return head if set(old) == {"0"} else f"{commit(old)}..{head}"


def scan(revisions, executable):
    # Keep native authentication, but prevent config/dotenv/fail-open/logging
    # settings from changing this gate. Never persist or forward findings.
    """Run strict ggshield scans and suppress sensitive scanner diagnostics."""
    env = {
        k: v for k, v in command_environment().items() if not k.startswith(("GITGUARDIAN_", "GG_"))
    }
    for key in ("GITGUARDIAN_API_KEY", "GITGUARDIAN_INSTANCE"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(
        GITGUARDIAN_DONT_LOAD_ENV="1",
        GITGUARDIAN_EXIT_ZERO="false",
        GITGUARDIAN_FAIL_ON_SERVER_ERROR="true",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="Never",
    )
    version = subprocess.run(
        [executable, "--version"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.fullmatch(r"ggshield, version (\d+)\.(\d+)\.(\d+)\s*", version.stdout)
    if version.returncode or not match:
        raise GateError(21, "UNAVAILABLE: cannot verify the approved ggshield version.")
    if not (1, 56, 1) <= tuple(map(int, match.groups())) < (2, 0, 0):
        raise GateError(21, "UNAVAILABLE: ggshield >=1.56.1,<2 is required.")
    with tempfile.TemporaryDirectory(prefix="secret-gate-") as directory:
        config = Path(directory) / "config.yaml"
        config.write_text(
            "version: 2\nexit_zero: false\nsecret:\n  show_secrets: false\n",
            encoding="utf-8",
        )
        for revision, count in revisions:
            print(f"SCANNING: {count} commit(s), {revision}", flush=True)
            # Even censored findings can include sensitive source context.
            result = subprocess.run(
                [
                    executable,
                    "--no-check-for-updates",
                    "--config-path",
                    str(config),
                    "secret",
                    "scan",
                    "commit-range",
                    "--all-secrets",
                    revision,
                ],
                check=False,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
            )
            if result.returncode == 1:
                raise GateError(
                    1,
                    "FINDINGS: secrets detected; remediate before publication.",
                )
            if result.returncode == 3:
                raise GateError(
                    3,
                    "AUTH_FAILED: restore approved native ggshield authentication.",
                )
            if result.returncode:
                raise GateError(
                    4,
                    f"SCAN_ERROR: scanner exit {result.returncode}; publication blocked.",
                )
    return 0


def main(argv=None, stream=None):
    """Run the publication gate and return its classified exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--base",
        help="explicit PR base or destination tip, resolved to an immutable commit",
    )
    mode.add_argument(
        "--all-history",
        action="store_true",
        help="scan all HEAD history for an initial/orphan publication",
    )
    mode.add_argument(
        "--pre-push",
        action="store_true",
        help="read Git ref updates from stdin; performs no push",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="configured remote for the default PR base lookup",
    )
    args = parser.parse_args(argv)
    try:
        if sys.version_info < MINIMUM_PYTHON:
            raise GateError(21, "UNAVAILABLE: use supported Python 3.11 or newer.")
        if not executable("git"):
            raise GateError(21, "UNAVAILABLE: install Git before running this gate.")
        head = None if args.pre_push else commit("HEAD")
        selected = ranges(args, sys.stdin if stream is None else stream, head)
        revisions = [(r, int(git("rev-list", "--count", r, "--"))) for r in selected]
        revisions = [(r, n) for r, n in revisions if n]
        if not revisions:
            raise GateError(10, "SKIPPED: no outgoing commits; no clean scan is claimed.")
        scanner = executable("ggshield")
        if not scanner:
            raise GateError(
                21,
                "UNAVAILABLE: install approved ggshield; publication blocked.",
            )
        scan(revisions, scanner)
        if not args.pre_push and head != commit("HEAD"):
            raise GateError(
                4,
                "SCAN_ERROR: HEAD changed; rerun for the new candidate.",
            )
        print(f"CLEAN: ggshield passed for {sum(n for _, n in revisions)} commit scan(s).")
        return 0
    except GateError as error:
        print(error.message, file=sys.stderr)
        return error.code
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(
            "SCAN_ERROR: command failed or timed out; publication blocked.",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    sys.exit(main())
