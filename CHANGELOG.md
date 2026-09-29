# Changelog

All notable changes to HaloCLI are documented here.

HaloCLI uses semantic-ish versioning while it is young: patch releases are
small fixes and packaging polish, minor releases may add commands or change
operator workflows, and major releases are reserved for breaking CLI behavior.

## 0.8.2 - 2026-09-29

- **Folded four findings from live report-interface testing into the spec**,
  so the next person does not have to rediscover them by trial and error
  (cost: several 28 MB report executions and a few 400s/415s):
  - `POST /Report` now has a `summary` and a `description` documenting that
    the body must be a **JSON array** of `AnalyzerProfile` (a bare object
    returns 400, a non-JSON `Content-Type` returns 415), that omitting `id`
    creates while including it updates, and that the stored report is
    returned. *Correction to my own earlier claim: the array schema itself
    was already correct upstream — my first probe only looked for a
    top-level `$ref` and reported a gap that did not exist. What was missing
    was prose, not schema.*
  - `loadreport` on `GET /Report/{id}` is now documented as the execution
    path: it returns `report.rows` and **Halo caps results at 50,000 rows** —
    measured against 135,148 closed tickets, i.e. the report silently
    returned 37% of the data.
  - `AnalyzerProfile.properties.sql` is now documented: Halo runs report SQL
    **inside a derived table**, so a trailing `ORDER BY` is rejected unless
    the `SELECT` also specifies `TOP`/`OFFSET`/`FOR XML`.
- **The overlay applier accepts two new target shapes** (stdlib-only, still
  no JSONPath dependency): `$.paths[...].<method>.parameters['<name>'].description`
  and `$.components.schemas.<Schema>.properties.<prop>.description`, both
  with the same fill-if-missing semantics. Targets are addressed by parameter
  *name* rather than index so they survive upstream re-ordering.
- `resolve_overlay_target()` was split out of `apply_overlay()` so tests can
  assert every overlay target resolves against the committed spec **without
  mutating it** — `schema.load_spec()` caches process-wide, so a mutating
  check would leak into later tests.
- Three new tests: the two new shapes fill and skip correctly, unknown
  parameter/property/schema targets fail loudly, and
  `test_report_interface_prose_documented` pins all four findings (plus the
  array schema) so they cannot silently drift out of the spec.
- Filed #15 for the commands these findings argue for: `reports run`
  (row count, cap warning, and surfacing `report.load_error`, which Halo
  returns with HTTP 200) and `reports clone`.

## 0.8.1 - 2026-09-29

- **Fixed every MSI ever shipped (0.5.0 – 0.8.0).** The frozen `halocli.exe`
  exited 1 with `Console script entry point not found: halocli` on every
  command. `halocli_launcher.py` resolves the console script through
  `importlib.metadata.entry_points()`, which reads `halocli-*.dist-info`, but
  the PyInstaller spec only bundled data via `collect_data_files()` — which
  does **not** include dist-info. PyInstaller only *WARNs* for the missing
  data, so the build exited 0 and shipped a dead binary every time. The spec
  now calls `copy_metadata("halocli")`. Verified by rebuilding, extracting the
  MSI, and running the exe with `PATH` stripped to `System32` (no Python
  present): `halocli 0.8.1`.
- **`build-msi.ps1` smoke-tests the binary it produced** — `halocli.exe
  --version` must print the expected version or the build fails. Negative-
  tested: reverting the fix makes the build exit 1 with the launcher's error
  instead of publishing. Four new tests in `tests/test_packaging.py` fail PR
  CI if the fix is removed, commented out, or the smoke test is deleted.
- **MSI releases are gated tag-vs-release instead of by event name.** Releases
  are created by the Release workflow with `GITHUB_TOKEN`, and token-created
  releases do not fire `release: published`. Every manually-dispatched run
  therefore skipped `gh release upload` and the mtg-winget notify, leaving
  0.7.0 – 0.8.0 with **no MSI on the release page** and mtg-winget frozen at
  0.5.0. The workflow now asks whether a release *exists* for the tag: if yes
  it attaches `halocli.msi` and dispatches mtg-winget; if no it uploads the
  workflow artifact (now uploaded on every run, so a MSI is always fetchable).
- **`build-msi.ps1` no longer breaks under Windows PowerShell 5.1** when the
  caller pipes `2>&1`: PyInstaller logs to stderr, and PS 5.1 turns redirected
  native stderr into terminating errors under `$ErrorActionPreference='Stop'`.
  Native tools now run through an `Invoke-Native` wrapper. (CI uses pwsh 7,
  where this never manifested — so this only affected local builds.)
- **RELEASE.md now documents the MSI**: how it is packaged (PyInstaller
  onefile — no Python runtime, no venv, no `pip` needed on the target),
  the tag-vs-release gating, the smoke test, and how to dispatch and build it
  locally.

## 0.8.0 - 2026-09-29

- **`halocli <resource> list` now stops at 500 records by default** and reports
  it honestly: the payload carries `"truncated": true`, `"total_available"`
  and a `hint` telling you to pass `--all`. A complete result never carries
  those keys. This is a behavior change from unbounded paging — on our own
  tenant `/Tickets` holds 137,329 records, and a bare `halocli tickets list`
  previously paged for ~18 minutes (measured 0.8s/page) with no output. It now
  returns in ~4s. Opt back into the old behavior with `--all`, or set
  `--max-records`/`--max-pages` explicitly; `--all` cannot be combined with
  those flags.
- **New `halocli auth whoami`** — reports the identity the token acts as
  (`/Agent/me`) and the scope Halo actually *granted*, read from the local
  token cache rather than the profile's requested scope. Scope is fixed at
  token issuance (a refresh grant never widens it), so this is the
  authoritative answer for the token in use.
- **`auth whoami --check PATH`** (repeatable) probes endpoints with a
  read-only `GET take=1` and reports reachability, classifying failures the
  same way the CLI does. `checks_all_reachable` summarizes the batch.
- The 403 `diagnostic` now points at `auth whoami --check`, and a new README
  section documents Halo's two-layer model (application scopes **and** agent
  role, combined as AND — scopes only narrow, never widen).
- `list_all` now reports truncation explicitly through an optional caller-owned
  `stats` dict (`record_count`, `returned`, `truncated`); stopping at a limit
  is only truncation when Halo says more records exist.

## 0.7.2 - 2026-09-29

- **Removed `halocli expenses get`** — it could only ever 404. Probing the live
  tenant (read-only, 2026-09-29) showed Halo has no `GET /Expense/{id}`:
  `/Expense/1` and `/Expense/0` both answer 404 while the collection
  `/Expense` answers 403 (permission), and the official spec documents only
  the collection. The registry now sets `supports_get=False` for expenses,
  and `cli.py` honors that flag instead of registering `get` unconditionally.
  `expenses list` is unaffected (the collection route exists).
- The coverage oracle's `read_mismatches` gate now also checks
  `{endpoint}/{id}` whenever a resource promises `supports_get`, so a
  registry over-promise of this class fails CI instead of shipping a
  always-404 subcommand. All three gates remain empty.
- Fixed a latent MCP bug the above exposed: `_verbs` treated
  `supports_get=False` as "no GET at all", which would have told agents the
  expenses resource is unreadable. Every resource exposes `list`, so `GET`
  stays in `verbs`; the flag only switches the capability string between
  `list/get` and `list`.
- Tests: the enrichment suite's surfaced-pair helper now respects
  `supports_get`, letting the previous `/Expense/{id}` exception be deleted
  (no special cases); new pins cover the missing `get` command, the
  item-route oracle check, and the list-still-advertises-GET contract.

## 0.7.1 - 2026-09-28

- Enriched the vendored spec so typed consumers can actually load it.
  Upstream Halo ships 3 operationIds for 1455 operations, which made Cloudflare
  Forge resolve only 3 of 1455; after this release it resolves **1455 of 1455**
  (verified against Forge's `init()`). Two layers, both applied by
  `scripts/vendor_halo_spec.py`:
  - missing `operationId`s are synthesized deterministically as
    `{method}_{path}` (e.g. `post_invoice_pdf_id`); the 3 upstream IDs are
    preserved, IDs are unique across all 1455 operations, and re-running the
    refresh is a no-op;
  - new committed overlay `src/halocli/spec/halo_overlay.json`
    (OpenAPI Overlay 1.0.0, 81 actions) fills `summary`/`description` for
    every surfaced operation, applied fill-if-missing so upstream prose is
    never overwritten. 1041 -> 1001 operations missing descriptions; all
    operations reachable from the resource registry are complete.
- `halocli search` results now carry synthesized `operationId`s and curated
  summaries (e.g. `POST /Invoice/PDF/{id}` -> `post_invoice_pdf_id`,
  "Render an invoice as a PDF"), so the operation payloads agents see in
  `halo_search` / `halocli search` are self-describing.
- New enforcement in `tests/test_spec_enrichment.py` (10 tests): every
  operation has a unique rule-conforming operationId, every surfaced
  operation has id + summary + description, overlay targets exist in the
  spec, and fill-if-missing / loud-failure semantics are pinned. Registry
  growth now fails CI until the overlay is extended.

## 0.7.0 - 2026-09-28

- Added first-class commands for 22 nested operations across three resources
  (`tickets` x7, `invoices` x5, `attachments` x10), declared as
  `ResourceOperation` metadata and generated into subcommands: e.g.
  `halocli invoices pdf 42 --save invoice.pdf`, `halocli tickets zapier`,
  `halocli attachments get-image 7`. Path arguments are arity-checked and
  URL-quoted; reads never take `--apply/--yes`; writes preview by default
  (zero network, no profile needed) and require `--apply --yes` to execute.
  Binary responses (PDFs, images) are never dumped into JSON — pass
  `--save PATH` to write them to a file.
- Multipart operations (e.g. `attachments upload-image`) accept `--file` and
  send the spec-declared `multipart/form-data` field `file`.
- Every operation carries honest verification provenance (`live`,
  `live:403`, `live:404`, `live:500`, `route-verified`, `spec`) shown in `--help`,
  recorded from probing the real tenant on 2026-09-28 — writes are never
  fired at a tenant without an operator.
- The coverage oracle gained an `operation` classification (declared
  nested ops count as first-class) plus an `operation_mismatches` gate that
  fails `--check` when a declared operation's path or method is missing
  from the spec: first-class coverage 125 -> 147 operations (8.6% -> 10.1%),
  candidates 361 -> 358 (Attachment, Tickets and Invoice fully declared).
- MCP `halo_search`/`halo_resources` now surface operations (`operations`
  entries with name/method/path/write, `matched_operations` on search hits),
  so agents can discover e.g. `pdf` and execute it through `halo_execute`
  by passing the operation's path — still exactly three tools.

## 0.6.0 - 2026-09-25

- Vendored the official HaloPSA REST API v2 OpenAPI specification
  (`src/halocli/spec/halo_openapi.json`, 927 paths) with a re-runnable
  `scripts/vendor_halo_spec.py` refresh script.
- Added `halocli search`, offline discovery across the resource registry and
  the vendored spec, and `halocli raw` spec validation (unknown endpoints and
  missing required body fields are refused by default; `--no-validate`
  bypasses it, warnings pass through as `spec_warnings`).
- Added first-class write commands for the nine resources with write metadata:
  `create`, `update`, `delete` are preview-by-default dry runs that make zero
  network calls, and require `--apply --yes` to execute (matching `raw`).
  All nine support create, update and delete. `contracts` stays read-only
  because the vendored spec documents no `POST /Contract` (only
  `POST /ClientContract` and `POST /SupplierContract`, whose semantics
  differ). A create payload containing a concrete `id` is rejected — Halo
  treats POST-with-id as an update — and the preview shows every field that
  apply will send, with declared preview fields ordered first.
- Added `halocli serve`, a code-mode MCP server (newline-delimited JSON-RPC
  over stdio, stdlib only) exposing exactly three tools — `halo_search`,
  `halo_execute`, `halo_resources` — instead of one tool per Halo operation.
  Non-GET calls require `apply: true` and responses are bounded at 40k chars.
  Tools advertise MCP annotations (`readOnlyHint` etc.) so hosts can gate
  writes without prompting on discovery calls — e.g. Codex's `writes`
  approval mode.
- Added a coverage oracle (`scripts/coverage_report.py`) that diffs the
  vendored spec against the resource registry offline: first-class coverage
  percentages, uncurated roots ranked as curation candidates, and both
  mismatch directions — `write_mismatches` (write metadata pointing at
  endpoints the spec lacks, the `/Contract` bug class; `--check` is a CI
  gate) and `read_mismatches` (registry endpoints absent from the spec).
  Test-pinned so a spec refresh cannot silently change the state.
- Fixed three registry endpoints that returned 404 against the live
  tenant (verified 2026-09-28): `contracts` `/Contract` → `/ClientContract`
  (list 200; detail 401 for agents without contract-module view rights),
  `opportunities` `/Opportunity` → `/Opportunities` (correct path; 403
  without Sales-module permission), `projects` `/Project` → `/Projects`
  (200; Halo exposes projects through the fault schema, rows carry
  `summary`, so the table fields follow). All three are now spec-documented
  and the oracle's `read_mismatches` list is empty.
- Added multipart upload and binary-safe responses to `HaloClient`
  (`files=`/`data=` passthrough, `download()`, bytes payloads for
  non-JSON content types such as attachments, report exports and PDFs).
- Isolated the test suite from ambient machine state: real config profiles,
  `HALO_*` environment variables, the Windows Credential Manager keyring, and
  the browser/OAuth callback are all faked or redirected per test.
- Fixed JSON output being corrupted by terminal soft-wrapping when piped, so
  `halocli ... | jq` is now reliable.

## 0.5.0 - 2026-04-27

- Added `halocli todo web`, a local-first FastAPI/Vite React Todo web UI over
  Halo appointment-backed tasks.
- Added normalized Todo API routes for listing, creating, updating, completing,
  and noting tasks, preserving the HaloCLI metadata marker in `note_html`.
- Added client/ticket picker APIs and 0-duration Halo time-entry-backed work
  logs for Todo updates.
- Added Todo work-log history reads from Halo time entries.
- Added the optional `web` package extra for FastAPI and Uvicorn.

## 0.4.0 - 2026-04-26

- Added a central Halo resource registry.
- Expanded first-class read commands across common HaloPSA operational,
  business, and admin resources.
- Added generic `get` commands for registry resources.
- Added resource-specific default table fields.
- Kept first-class writes limited to the specialized `todo add` command and
  guarded raw requests.
- Kept live Microsoft To Do auth optional while preserving JSON preview.

## 0.3.4 - 2026-04-24

- Fixed Python 3.10 compatibility by replacing `enum.StrEnum` usage.

## 0.3.3 - 2026-04-24

- Added GitHub Actions CI across Windows, macOS, and Linux.
- Added tagged GitHub Release builds with wheel, source distribution, and
  CycloneDX SBOM artifacts.
- Added package metadata checks with `twine check`.
- Added dependency auditing with `pip-audit`.
- Added release checklist documentation.
- Made CLI tests less sensitive to terminal rendering differences across
  operating systems.

## 0.3.2 - 2026-04-24

- Added standalone HaloPSA CLI package with JSON-first command output.
- Added client-credentials auth and experimental interactive Halo OAuth login.
- Added discovery for Halo authorization-code endpoints.
- Added guarded raw requests that require `--apply --yes` for write methods.
- Added OS secure token storage reporting for Windows Credential Manager/DPAPI
  and macOS Keychain.
- Added third-party prior-art notice for `netaryx/pyhalopsa`.
- Added GPL-3.0-only licensing.
