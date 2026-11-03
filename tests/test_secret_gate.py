"""Offline regression tests: real Git histories and a fake scanner, never an API."""

import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GATE = Path(__file__).resolve().parents[1] / "scripts" / "secret_gate.py"
SPEC = importlib.util.spec_from_file_location("secret_gate", GATE)
secret_gate = importlib.util.module_from_spec(SPEC)
sys.modules["secret_gate"] = secret_gate
SPEC.loader.exec_module(secret_gate)
FAKE = """import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("ggshield, version " + os.environ.get("GATE_VERSION", "1.56.1"))
    sys.exit(0)
assert not any(
    Path(entry).resolve().is_relative_to(Path.cwd().resolve())
    for entry in os.get_exec_path()
)
assert "--all-secrets" in args
assert os.environ["GITGUARDIAN_EXIT_ZERO"] == "false"
assert os.environ["GITGUARDIAN_DONT_LOAD_ENV"] == "1"
assert "GITGUARDIAN_LOG_FILE" not in os.environ
assert "ignored_paths" not in Path(args[args.index("--config-path")+1]).read_text()
revision = args[-1]
with open(os.environ["GATE_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(revision + "\\n")
if "GATE_CODE" in os.environ:
    print("sensitive-output-must-stay-private")
    sys.exit(int(os.environ["GATE_CODE"]))
commits = subprocess.check_output(["git", "rev-list", revision], text=True)
for sha in commits.splitlines():
    diff = subprocess.check_output(["git", "show", sha], text=True)
    if "synthetic-secret-marker" in diff:
        sys.exit(1)
sys.exit(0)
"""


class SecretGateTests(unittest.TestCase):
    """Exercise publication blocking with real Git and a fake scanner."""

    def setUp(self):
        """Create a disposable repository, bare remote and synthetic scanner."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITGUARDIAN_", "GG_"))
        }
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("base")
        self.git("clone", "--bare", "-q", ".", str(self.root / "remote.git"))
        self.git("remote", "add", "origin", str(self.root / "remote.git"))
        self.git("switch", "-q", "-c", "feature")
        self.head = self.commit("feature")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "ggshield"
        (self.bin / "fake.py").write_text(FAKE)
        if os.name == "nt":
            fake.with_suffix(".cmd").write_text(
                f'@"{sys.executable}" "{self.bin / "fake.py"}" %*\n'
            )
        else:
            fake.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                f'{shlex.quote(str(self.bin / "fake.py"))} "$@"\n'
            )
            fake.chmod(0o700)
        self.calls = self.root / "calls"
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            GATE_CALLS=str(self.calls),
        )

    def git(self, *args):
        """Run Git without interactive authentication or replacement objects."""
        return subprocess.check_output(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()

    def commit(self, content):
        """Resolve a ref to an immutable commit before selecting history."""
        (self.repo / "file.txt").write_text(content)
        self.git("add", "file.txt")
        self.git("commit", "-q", "-m", "fixture", "--no-gpg-sign")
        return self.git("rev-parse", "HEAD")

    def run_gate(self, *args, input_text=None):
        """Execute the gate without inheriting real scanner credentials."""
        return subprocess.run(
            [sys.executable, str(GATE), *args],
            check=False,
            cwd=self.repo,
            env=self.env,
            input=input_text,
            text=True,
            capture_output=True,
        )

    def test_new_branch_without_upstream_uses_remote_default(self):
        """Prove new branch without upstream uses remote default."""
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEAN:", result.stdout)
        self.assertEqual(self.calls.read_text().splitlines(), [f"{self.base}..{self.head}"])

    def test_feature_upstream_does_not_hide_pr_commits(self):
        """Prove feature upstream does not hide pr commits."""
        self.git("update-ref", "refs/remotes/origin/feature", self.head)
        self.git("config", "branch.feature.remote", "origin")
        self.git("config", "branch.feature.merge", "refs/heads/feature")
        self.assertEqual(self.run_gate().returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), [f"{self.base}..{self.head}"])

    def test_added_then_removed_secret_is_in_history(self):
        """Prove added then removed secret is in history."""
        first = self.commit("synthetic-secret-marker")
        self.commit("feature")
        self.assertEqual(self.git("diff", self.head, "HEAD"), "")
        result = self.run_gate("--base", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(first, self.git("rev-list", self.calls.read_text().strip()))
        self.assertNotIn("synthetic-secret-marker", result.stdout + result.stderr)

    def test_non_main_base_and_detached_head(self):
        """Prove non main base and detached head."""
        self.git("checkout", "-q", "--detach")
        self.assertEqual(self.run_gate("--base", self.base).returncode, 0)

    def test_missing_base_is_not_clean(self):
        """Prove missing base is not clean."""
        self.assertEqual(self.run_gate("--base", "missing").returncode, 20)
        self.assertFalse(self.calls.exists())

    def test_empty_range_is_skipped(self):
        """Prove empty range is skipped."""
        result = self.run_gate("--base", "HEAD")
        self.assertEqual(result.returncode, 10)
        self.assertIn("SKIPPED:", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_auth_scan_errors_and_output_redaction(self):
        """Prove auth scan errors and output redaction."""
        for scanner_code, gate_code in ((1, 1), (3, 3), (2, 4), (4, 4), (128, 4)):
            with self.subTest(scanner_code=scanner_code):
                self.env["GATE_CODE"] = str(scanner_code)
                result = self.run_gate("--base", self.base)
                self.assertEqual(result.returncode, gate_code)
                self.assertNotIn("sensitive-output", result.stdout + result.stderr)

    def test_fail_open_environment_and_dotenv_cannot_bypass(self):
        """Prove fail open environment and dotenv cannot bypass."""
        self.env.update(GITGUARDIAN_EXIT_ZERO="true", GITGUARDIAN_LOG_FILE="unsafe.log")
        (self.repo / ".env").write_text("GITGUARDIAN_EXIT_ZERO=true\n")
        self.assertEqual(self.run_gate("--base", self.base).returncode, 0)
        self.assertFalse((self.repo / "unsafe.log").exists())

    def test_missing_scanner_blocks(self):
        """Prove missing scanner blocks."""
        with (
            patch("secret_gate.executable", side_effect=["git", None]),
            patch("secret_gate.commit", return_value=self.head),
            patch("secret_gate.ranges", return_value=[self.head]),
            patch("secret_gate.git", return_value="1"),
        ):
            self.assertEqual(secret_gate.main(["--base", self.base]), 21)

    def test_missing_git_blocks(self):
        """An absent Git executable must not claim a successful scan."""
        with patch("secret_gate.executable", return_value=None):
            self.assertEqual(secret_gate.main(["--base", self.base]), 21)

    def test_shallow_history_blocks(self):
        """Prove shallow history blocks."""
        (self.repo / ".git/shallow").write_text(self.base + "\n")
        self.assertEqual(self.run_gate("--base", self.base).returncode, 20)

    def test_no_remote_requires_explicit_choice_and_includes_root(self):
        """Prove no remote requires explicit choice and includes root."""
        self.git("remote", "remove", "origin")
        self.assertEqual(self.run_gate().returncode, 20)
        self.assertEqual(self.run_gate("--all-history").returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), [self.head])
        self.assertIn(self.base, self.git("rev-list", self.calls.read_text().strip()))

    def test_remote_base_object_must_be_fetched(self):
        """Prove remote base object must be fetched."""
        stale = self.root / "stale"
        self.git("clone", "--no-hardlinks", "-q", str(self.root / "remote.git"), str(stale))
        self.git("push", "-q", "origin", "HEAD:main")
        result = subprocess.run(
            [sys.executable, str(GATE)],
            check=False,
            cwd=stale,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 20)
        self.assertFalse(self.calls.exists())

    def test_prepush_multiple_refs_and_new_branch(self):
        """Prove prepush multiple refs and new branch."""
        updates = f"refs/heads/feature {self.head} refs/heads/feature {self.base}\n"
        updates += f"refs/heads/other {self.head} refs/heads/other {'0' * 40}\n"
        result = self.run_gate("--pre-push", input_text=updates)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls.read_text().splitlines(),
            [f"{self.base}..{self.head}", self.head],
        )

    def test_prepush_with_unborn_checkout_scans_the_pushed_ref(self):
        """Pushing an existing ref does not depend on the checked-out HEAD."""
        self.git("switch", "--orphan", "unborn")
        updates = f"refs/heads/feature {self.head} refs/heads/feature {self.base}\n"
        result = self.run_gate("--pre-push", input_text=updates)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [f"{self.base}..{self.head}"])

    def test_grafted_history_blocks(self):
        """Legacy grafts must not hide a secret-bearing ancestor."""
        self.commit("synthetic-secret-marker")
        final = self.commit("clean-again")
        (self.repo / ".git/info/grafts").write_text(f"{final} {self.head}\n")
        self.assertEqual(self.run_gate("--base", self.base).returncode, 20)
        self.assertFalse(self.calls.exists())

    def test_environment_grafted_history_blocks(self):
        """An environment-selected graft file must not hide history."""
        grafts = self.root / "external-grafts"
        grafts.write_text(f"{self.head}\n")
        self.env["GIT_GRAFT_FILE"] = str(grafts)
        self.assertEqual(self.run_gate("--base", self.base).returncode, 20)
        self.assertFalse(self.calls.exists())

    def test_prepush_delete_malformed_and_unknown_tip(self):
        """Prove prepush delete malformed and unknown tip."""
        cases = [
            (f"(delete) {'0' * 40} refs/heads/old {self.base}\n", 10),
            ("bad\n", 20),
            (f"refs/heads/feature {self.head} refs/heads/feature {'f' * 40}\n", 20),
        ]
        for updates, expected in cases:
            self.assertEqual(self.run_gate("--pre-push", input_text=updates).returncode, expected)

    def test_changed_head_does_not_claim_clean(self):
        """Prove changed head does not claim clean."""
        with (
            patch("secret_gate.executable", return_value="tool"),
            patch("secret_gate.commit", side_effect=[self.head, self.base]),
            patch("secret_gate.ranges", return_value=[self.head]),
            patch("secret_gate.git", return_value="1"),
            patch("secret_gate.scan", return_value=0),
        ):
            self.assertEqual(secret_gate.main(["--base", self.base]), 4)

    def test_timeout_does_not_claim_clean(self):
        """Prove timeout does not claim clean."""
        with (
            patch("secret_gate.executable", return_value="tool"),
            patch("secret_gate.commit", return_value=self.head),
            patch("secret_gate.ranges", return_value=[self.head]),
            patch("secret_gate.git", return_value="1"),
            patch(
                "secret_gate.scan",
                side_effect=subprocess.TimeoutExpired("scanner", 1800),
            ),
        ):
            self.assertEqual(secret_gate.main(["--base", self.base]), 4)

    def test_force_push_scans_unique_new_history(self):
        """Prove force push scans unique new history."""
        self.git("switch", "-q", "-c", "rewritten", self.base)
        rewritten = self.commit("rewritten")
        updates = f"refs/heads/feature {rewritten} refs/heads/feature {self.head}\n"
        self.assertEqual(self.run_gate("--pre-push", input_text=updates).returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), [f"{self.head}..{rewritten}"])

    def test_merged_side_branch_commits_are_included(self):
        """Prove merged side branch commits are included."""
        self.git("switch", "-q", "-c", "side", self.base)
        secret = self.commit("synthetic-secret-marker")
        self.commit("side-clean")
        self.git("switch", "-q", "feature")
        # Resolve this synthetic merge without dropping the side branch ancestry.
        self.git("merge", "--no-ff", "-s", "ours", "-m", "fixture merge", "side")
        result = self.run_gate("--base", self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn(secret, self.git("rev-list", self.calls.read_text().strip()))

    def test_option_like_ref_rejected_before_git(self):
        """Reject agent-supplied option arguments before command invocation."""
        with patch("secret_gate.git") as command:
            with self.assertRaises(secret_gate.GateError):
                secret_gate.commit("--help")
            command.assert_not_called()

    def test_option_like_remote_rejected_before_git(self):
        """Reject option-like remote names even if a repo config lists one."""
        with patch("secret_gate.git", return_value="--heads") as command:
            with self.assertRaises(secret_gate.GateError):
                secret_gate.remote_base("--heads")
            command.assert_not_called()

    def test_git_boundary_rejects_unexpected_argument_characters(self):
        """Prevent arbitrary agent arguments from reaching the Git process."""
        with patch("secret_gate.subprocess.run") as command:
            with self.assertRaises(secret_gate.GateError):
                secret_gate.git("ls-remote", "--symref", "bad;command", "HEAD")
            command.assert_not_called()

    def test_replace_refs_cannot_hide_outgoing_commits(self):
        """Prove replace refs cannot hide outgoing commits."""
        self.commit("synthetic-secret-marker")
        final = self.commit("clean-again")
        self.git("replace", "--graft", final, self.head)
        self.assertEqual(self.run_gate("--base", self.base).returncode, 1)

    def test_checkout_executable_cannot_claim_clean(self):
        """A checkout-owned decoy must not replace the approved scanner."""
        self.commit("synthetic-secret-marker")
        decoy = self.repo / ("ggshield.cmd" if os.name == "nt" else "ggshield")
        decoy.write_text("@exit /b 0\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n")
        decoy.chmod(0o700)
        self.env["PATH"] = str(self.repo) + os.pathsep + self.env["PATH"]
        # Older Windows lookup can reject the decoy rather than find the next tool.
        expected = (1, 21) if os.name == "nt" else (1,)
        self.assertIn(self.run_gate("--base", self.base).returncode, expected)

    def test_implicit_current_directory_tool_is_rejected(self):
        """Reject a tool returned through Windows' implicit cwd lookup."""
        with (
            patch.dict(os.environ),
            patch("secret_gate.Path.cwd", return_value=self.repo),
            patch("secret_gate.shutil.which", return_value=str(self.repo / "ggshield.cmd")),
        ):
            self.assertIsNone(secret_gate.executable("ggshield"))

    def test_git_environment_cannot_select_a_different_repository(self):
        """Scan the current checkout despite ambient repository selectors."""
        self.commit("synthetic-secret-marker")
        self.env.update(
            GIT_DIR=str(self.root / "remote.git"),
            GIT_WORK_TREE=str(self.root),
            GIT_COMMON_DIR=str(self.root / "remote.git"),
        )
        self.assertEqual(self.run_gate("--base", self.base).returncode, 1)

    def test_scanner_python_import_path_cannot_load_checkout_code(self):
        """Check both scanner invocations reject checkout import overrides."""
        marker = self.repo / "shadow-imported"
        (self.repo / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        )
        self.env["PYTHONPATH"] = str(self.repo) + os.pathsep + self.env.get("PYTHONPATH", "")
        # Run the gate without startup imports, then set the scanner's input.
        program = (
            "import os, runpy, sys; "
            f"os.environ['PYTHONPATH'] = {self.env['PYTHONPATH']!r}; "
            f"sys.argv = [{str(GATE)!r}, '--base', {self.base!r}]; "
            f"runpy.run_path({str(GATE)!r}, run_name='__main__')"
        )
        clean_env = dict(self.env)
        clean_env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=self.repo,
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_linked_worktree_rejects_tools_from_main_checkout(self):
        """A main-checkout decoy cannot become a linked worktree's scanner."""
        self.commit("synthetic-secret-marker")
        linked = self.root / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        decoy = self.repo / ("ggshield.cmd" if os.name == "nt" else "ggshield")
        decoy.write_text("@exit /b 0\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n")
        decoy.chmod(0o700)
        env = dict(self.env, PATH=str(self.repo) + os.pathsep + self.env["PATH"])
        result = subprocess.run(
            [sys.executable, str(GATE), "--base", self.base],
            cwd=linked,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        expected = (1, 21) if os.name == "nt" else (1,)
        self.assertIn(result.returncode, expected, result.stderr)

    def test_annotated_tags_are_blocked_before_scanner_success(self):
        """Never claim coverage for unscanned annotation text."""
        self.git("tag", "-a", "-m", "synthetic-secret-marker", "release")
        tag = self.git("rev-parse", "release")
        result = self.run_gate(
            "--pre-push",
            input_text=f"refs/tags/release {tag} refs/tags/release {'0' * 40}\n",
        )
        self.assertEqual(result.returncode, 20)
        self.assertIn("annotated tags", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_valid_equal_ref_and_at_remote_are_supported(self):
        """Support ordinary configured names without accepting shell syntax."""
        self.git("branch", "fix=api", self.base)
        self.git("remote", "add", "team@code", str(self.root / "remote.git"))
        self.assertEqual(self.run_gate("--base", "fix=api").returncode, 0)
        self.assertEqual(self.run_gate("--remote", "team@code").returncode, 0)

    def test_scan_uses_the_original_resolved_head(self):
        """A ref change cannot switch the immutable commit sent to the scanner."""
        with (
            patch("secret_gate.executable", return_value="tool"),
            patch("secret_gate.commit", side_effect=[self.head, self.base, self.head]),
            patch("secret_gate.git", side_effect=[str(self.repo / ".git"), "false", "1"]),
            patch("secret_gate.scan", return_value=0) as scanner,
        ):
            self.assertEqual(secret_gate.main(["--all-history"]), 4)
            scanner.assert_called_once_with([(self.head, 1)], "tool")

    def test_explicit_release_base_includes_default_branch_history(self):
        """A release PR must scan commits already present on the default branch."""
        self.commit("synthetic-secret-marker")
        self.git("push", "-q", "origin", "HEAD:main")
        (self.repo / "other.txt").write_text("unrelated change")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "fixture", "--no-gpg-sign")
        self.assertEqual(self.run_gate().returncode, 0)
        self.assertEqual(self.run_gate("--base", self.base).returncode, 1)

    def test_unsupported_python_blocks_before_tool_lookup(self):
        """An unsupported gate runtime must not start a publication scan."""
        with (
            patch("secret_gate.sys.version_info", (3, 10, 20)),
            patch("secret_gate.executable") as lookup,
        ):
            self.assertEqual(secret_gate.main(["--all-history"]), 21)
            lookup.assert_not_called()

    def test_unsupported_scanner_version_blocks(self):
        """Unknown scanner contracts must not yield a clean publication result."""
        self.env["GATE_VERSION"] = "1.55.0"
        self.assertEqual(self.run_gate("--base", self.base).returncode, 21)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
