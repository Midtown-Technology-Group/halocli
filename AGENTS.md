# HaloCLI agent guidance

HaloCLI is a standalone Python HaloPSA operator CLI and stdio MCP server; it must remain independent of Bifrost. Start with [README.md](README.md) for command semantics and [RELEASE.md](RELEASE.md) for packaging.

## Map and verification

`src/halocli/cli.py` owns CLI dispatch; `client.py` and `auth.py` own transport/authentication. `resources.py`, `writes.py`, and the vendored `spec/` define operation metadata. Keep registry routes, overlay enrichment, help provenance, and tests consistent when adding operations.

Use Python 3.12+; CI tests 3.12â€“3.14 across Windows, macOS, and Linux. Install with `python -m pip install -e ".[dev]"`, then run `python -m pytest -q` and `python -m ruff check .`. Editable installation is required by packaging metadata tests. Run `python scripts/coverage_report.py --check --top 50` for registry/spec changes; write mismatches must remain empty. Read mismatches are report-only but pinned by tests. Packaging changes also need CI's package checks and installed CLI smoke tests.

## Operator boundaries

- Writes preview with zero network calls by default; CLI execution requires both `--apply` and `--yes`. Preserve raw-request validation and MCP's non-GET `apply: true` guard. Test refusals without contacting a tenant.
- Bound list requests and preserve truncation metadata. Do not retry report execution automatically: timeout retries can repeat expensive queries; HTTP 200 may still contain a Halo SQL failure.
- Authentication discovery must confirm interactive endpoints before browser login. Prefer OS secure credential storage; file-token-cache fallback is an explicit operator decision. Never commit profiles or expose tokens.
- Production writes require the user's authorized target and scope, reviewed preview, and independent readback. Do not treat a documented route or mocked test as live verification.
- Keep release artifacts bound to the selected tag; MSI builds use a clean release environment without dev extras and must pass binary version smoke checks.

## Sonar feedback

Use the shared `sonar-feedback` skill for scan routing and bounded findings.
Repository build/test gates and operator boundaries remain authoritative.
Repo-owned Sonar configuration separates authored source from tests. CI
produces coverage for the scanner; unavailable coverage is not a clean result.
CI is the authoritative analysis mode; automatic analysis stays disabled.

## Secret gate before publication

Before publishing the current PR branch, commit the complete candidate and run
`python3 scripts/secret_gate.py --base origin/main` (native Windows: `python`). Require exit 0;
missing tooling/authentication, scan errors and skipped scans block publication.
Then run the existing repository gates. Repeat after changing outgoing commits.
Push exactly the scanned HEAD. This PR scan covers the current branch against its
fetched intended base; other refs, multi-ref pushes and rewritten history require
`--pre-push` with Git's complete ref-update stream. Stop if any pushed ref is
uncovered. See [secret scanning](SECRET-SCANNING.md) for base selection, new branches,
pre-push input and prerequisites. Preserve GitGuardian App, CI and Gitleaks checks.

For a non-default PR target, fetch its destination and pass that exact ref to
`--base`; the remote default is not a substitute for a release/maintenance base.

The secret gate requires Python 3.12+ and approved ggshield >=1.56.1,<2.
