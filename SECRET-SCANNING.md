# Secret checks before publication

Before publishing the **current PR branch**, commit the complete candidate and
run the local secret gate against the fetched, intended PR base, followed by this
repository's normal validation. Push exactly the scanned HEAD.
A change to any outgoing commit requires a fresh scan. This gate scans committed
history; it does not certify uncommitted files. It performs no push or PR write.

```sh
python3 scripts/secret_gate.py
# Explicit non-default PR target or destination tip (fetch it first):
python3 scripts/secret_gate.py --base origin/release
# Initial publication, empty remote or orphan history, explicitly:
python3 scripts/secret_gate.py --all-history
# Offline regression checks (no GitGuardian calls):
python3 scripts/test_secret_gate.py -q
```

Use `python` instead of `python3` on native Windows. Python 3.10+, Git, and
ggshield on PATH are required. This gate was tested with ggshield **1.56.1**.
Install ggshield through the approved development-tool environment. Use existing
native ggshield authentication or an approved process-injected
`GITGUARDIAN_API_KEY`; never put credentials in the repository. For a private
GitGuardian instance, explicitly set its approved `GITGUARDIAN_INSTANCE`.
The gate never logs in, creates a key, or requests additional access.

By default the gate queries the configured `origin` remote's **current default
HEAD**, resolves that commit locally, and scans `base..HEAD`. It does not assume
`main`/`master`, use a feature branch's already-pushed upstream as the PR base,
or infer a base from `HEAD~1`. Use `--remote NAME` for a different configured
remote. If the remote tip isn't available locally, fetch it and rerun. A missing
base, failed remote lookup, grafted history or shallow clone blocks; fetch complete history first.
An explicit `--base` trusts the selected locally available ref; fetch it before use.

A PR scan certifies only commits outside its selected base that are reachable
from the current HEAD. It does not certify other branches or tags. Before pushing
other refs, multiple refs, or rewritten history, use `--pre-push` with the complete
Git update stream, normally through a composed pre-push hook. If the intended
publication is not covered by the scan, stop; do not reuse its clean result.

`--pre-push` reads Git's standard four-column ref updates from stdin, validates
all updates, then scans each `old..new` range. Force pushes include all commits
unique to the new tip. A new ref scans **all reachable history**, including root
commits; it can cost more API quota. Multiple updates and annotated commit tags
are supported. Unknown tips and non-commit tags block. Deletes have no content.
No hooks or Git configuration are installed automatically; compose existing hooks
rather than replacing them. Agent instructions require the gate even without hooks.

All commits in the selected range reach `ggshield secret scan commit-range`,
including merged side history and content subsequently removed. There is no hook
commit-count cap. Native ggshield detector/file limits still apply (including
binary/oversize content); this is not an attestation that every byte was scanned.
See the official [commit-range](https://docs.gitguardian.com/ggshield-docs/reference/secret/scan/commit-range)
and [configuration](https://docs.gitguardian.com/ggshield-docs/configuration) references.

Only exit **0 / CLEAN** means a successful scanner run. Other outcomes block:

| Exit | Outcome |
| --- | --- |
| 1 | Findings; remediate before publication |
| 2 | Invalid gate arguments |
| 3 | Authentication failed/unavailable; owner must restore approved native auth |
| 4 | Scan/transport/server/timeout error, or HEAD changed during the scan |
| 10 | Skipped: empty range or deletion-only input; no clean scan claimed |
| 20 | Invalid/unavailable base, Git operation, history or hook input |
| 21 | Required executable unavailable |

No skip or fail-open switch is provided. Strict temporary scanner configuration
ignores local/global exclusions and exit-zero settings; dotenv loading and scanner
log files are disabled, and all secrets are requested. Credentials stay in their
native storage/process environment. Raw findings and diagnostics are suppressed,
including source context; investigate findings locally in an approved private
terminal. Do not paste secret values into chat, tickets or logs.

GitGuardian App/server-side checks, CI secret scans, Gitleaks, branch protection,
and existing approval controls remain required and unchanged. Local scanning adds
an earlier gate; it does not replace them. Existing offline CI entry points remain
offline; agent guidance requires this additional gate. Use the local wrapper where
one is documented; otherwise invoke the Python command explicitly.
