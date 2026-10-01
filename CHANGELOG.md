# Changelog

All notable changes to HaloCLI are documented here.

HaloCLI uses semantic-ish versioning while it is young: patch releases are
small fixes and packaging polish, minor releases may add commands or change
operator workflows, and major releases are reserved for breaking CLI behavior.

## 1.1.1 - 2026-10-01

- **Fixed: malformed `--data` crashed with a raw traceback** on resource
  `create`/`update` and on `raw` — those paths called the unguarded JSON
  parser directly, so a typo'd inline body (or a bad file path) surfaced an
  uncaught `JSONDecodeError` on stderr. All `--data` paths now share the
  guarded loader: a structured `{"category": "validation", "error":
  "Invalid --data: …"}` on stderr and exit 1. Found live on the installed
  1.1.0 while verifying the `users update` MFA command; regression test
  covers all three commands.

## 1.1.0 - 2026-10-01

- **`users` becomes write-capable** — promoted from `raw` after a live
  end-to-end MFA chain (issue #28, test user 4266 under client 625,
  authorized): `halocli users create/update/delete` with zero-network preview
  and the `--apply --yes` gate.
  - **End-user MFA reset, first-class:**
    `halocli users update <id> --data '{"_revoke_authenticatorapp": true}'
    --apply --yes` — verified live: returns 200 even for unenrolled users
    (write-only flag, not echoed). Also exposed: `resetpassword`,
    `new_password` (tenant policy: >=16 chars, <=2 identical in a row,
    lower+upper+number+special), `locked`.
  - `required_create_fields` = the **live-proven set** (firstname, surname,
    name, emailaddress, client_id, site_id) — Halo's sequential 400s named
    `name` ("Username must be entered") and `site_id` ("A Site must be
    selected") explicitly; the spec itself declares zero required fields.
  - Findings from the chain: there is **no admin-side MFA enable**
    (`twofactor_enabled`/`authenticatorapp_configured` silently ignored, no
    POST embeds `Uname`, portal is Entra SSO-fronted) and MFA state is not
    readable on this scope (`authenticatorapp_configured` never returned,
    0/9 users). Full evidence in issue #28.
- **Fixed: `raw` treated array-body spec warnings as fatal.** Validator
  warnings inside array bodies arrive path-prefixed
  (`body[0]: warning: ...`), which missed the `startswith("warning: ")`
  split, so unknown properties were refused instead of surfacing as
  `spec_warnings` (the documented behavior). Classification now strips the
  validator's own `body[N]: ` prefix before the check
  (`_split_spec_problems`), with unit + regression tests — including
  CodeRabbit's follow-up: path text containing `warning: ` can no longer
  downgrade an unknown-endpoint refusal.
- **Review fixes from CodeRabbit (PR #29):** credential values
  (`new_password` et al.) are masked as `***` in all rendered write
  payloads — preview and post-apply — while the wire request is untouched;
  README examples use `<id>` placeholders instead of the real test-user id;
  docstrings added to touched test helpers and `raw`.

## 1.0.0 - 2026-10-01

- **BREAKING: `halocli search` renamed to `halocli catalog`** (offline
  registry + vendored-spec discovery), per the versioning policy — majors are
  reserved for breaking CLI behavior. `search` now means **live tenant
  search**. A legacy multi-term invocation fails with the migration hint
  ("offline catalog discovery moved to `halocli catalog`") instead of
  silently querying the tenant; single-term legacy calls resolve to live
  search, which is why this is a major, not a minor.
- **New `halocli search <term>`**: shaped cross-entity search over
  `GET /Search` (tickets, articles, clients, users, assets, services —
  grouped by the stable `use` key, since `table` is absent on service rows).
  Flags: `--count-per-entity` (1–100, server default ~5), `--limit` (output
  rows; Halo still returns everything — observed ~160 KB at 50/entity).
  Output carries `row_count`, `count`, `entity_counts`, `items[:limit]` and
  a `hint`; non-array bodies fail as `validation`, permission failures exit 1
  with a diagnostic. Verified live 2026-10-01 across 8 read-only probes.
- **New `searches` resource** (`/Search`) for registry coverage — reads are
  GET-only in the spec; `supports_get` is false because `/Search/{id}` does
  not exist. Registry total: 36.

## 0.14.0 - 2026-10-01

- **Three billing resources join the registry, read-only by design** (slice ④):
  `invoice-payments` (`/InvoicePayment`), `invoice-statuses`
  (`/InvoiceStatus`) and `recurring-invoices` (`/RecurringInvoice`) — each
  with `list`/`get`, live-verified envelopes and table columns taken from real
  list rows (payments: `client_name`/`amount`/`date`; statuses: `status_name`
  under the unusual `data` envelope; recurring: `total`/`nextcreationdate`,
  negative ids observed on this tenant).
  - **Deliberately no write metadata**: recording payments and the
    `POST /RecurringInvoice/process` bulk-invoice generator are the
    highest-stakes writes in the endpoint recon — they stay `raw` until
    read-back verification handlers exist. `invoice-statuses` config writes
    can join the preview-first surface later.
  - `/InvoiceStatus` was missing summary/description upstream — filled from
    `halo_overlay.json` (4 actions), applied offline (no refetch).

## 0.13.0 - 2026-10-01

- **`crm-notes` becomes write-capable** (promoted from `raw`): `halocli crm-notes
  create`, `update` and `delete` now exist with zero-network preview and the
  usual `--apply --yes` gate. `required_create_fields=("note",)` only — the
  spec declares no required fields and the anchor is polymorphic (client,
  supplier, quote, invoice, ticket), so demanding `client_id` would wrongly
  block supplier/quote notes. Verification `spec`: writes never fired at the
  tenant.
  - Live evidence 2026-09-30: envelope `{"actions": [...]}`, `GET
    /CRMNote/14630` → 200; `list_key="actions"` declared explicitly; table
    columns corrected (`datetime`, not the dead `date`).
  - Known read quirk documented in the declaration: Halo ignores `page_no`
    on `/CRMNote`, so `list` can duplicate rows when the result exceeds one
    page (`--param count=<n>` fetches in one page). Tracked as its own issue.

## 0.12.0 - 2026-10-01

- **`kb` becomes write-capable** (promoted from `raw`): `halocli kb create`,
  `update` and `delete` now exist with zero-network preview and the usual
  `--apply --yes` gate. Verified live 2026-09-30: list envelope is
  `{"articles": [...]}` with real pagination and `GET /KBArticle/{id}` returns
  200; the spec declares no required fields, so `name` + `description` are a
  documented assumption (per the `agents` precedent). `POST`/`DELETE` were
  never fired at the tenant — writes stay `verification: spec`.
  - Table columns corrected: live rows carry `name`, never the old `title`.
  - `list_key` declared as `articles` (previously found only by fallback).
- **`quotations` gains three write-gated operations** (promoted from `raw`):
  `lines`, `approval` and `view` (`POST /Quotation/Lines`, `/Approval`,
  `/View`). All three are spec-documented bare-array POSTs routed through the
  generic dispatcher: zero-network preview, `--data` passes through verbatim,
  execution requires `--apply --yes`, verification `spec` (never fired at the
  tenant). Live reads 2026-09-30: list 200 (223 quotes), `GET /Quotation/79`
  200.
  - Table columns corrected: `quote_number` exists in neither the spec nor
    this tenant and `total` is detail-only; the projection now uses live list
    fields (`title`, `status`, `date`). `list_key="quotes"` declared.
- **README contract rationale corrected**: the old text justified `contracts`
  being read-only with "the spec documents no `POST /Contract`" — true but
  about a path the resource does not use. It now states the real decision:
  `POST /ClientContract` exists (standard upsert), but billing-bearing client
  contracts need an explicit operator promotion decision.

## 0.11.0 - 2026-09-29

- **The bundled web front end is removed** (#19). Gone from the repository and
  the wheel: `frontend/` (React source + vitest suite), `index.html`,
  `vite.config.ts`, `tsconfig.json`, `package.json`, `package-lock.json`, and
  the committed `src/halocli/web_static/` build output. Package data is now
  just `spec/*.json`.
  - **Why:** CI has no Node step, so nothing ever ran the 6 vitest tests or
    the build — dependabot #7 bumped `vitest` ^4 → ^5 (a major) plus six other
    packages, clearing 8 lockfile vulnerabilities (4 high), with every check
    green and not one JS test executed. Removing the front end removes an
    unguarded, drifting build artifact instead of adding toolchain to police it.
  - **The Todo HTTP API is unchanged.** `halocli todo web` still serves
    `/api/todos`, `/api/clients`, `/api/tickets` and `/api/me`, docs at
    `/docs`; only static-file hosting was dropped. `/` now returns service info
    instead of the SPA shell, and the startup line says "Halo Todo API" with
    the docs URL. The API's 6 Python tests are untouched and still run in CI.
  - The shipped bundle was checked before deletion: no `nanoid`, `postcss` or
    `browserslist` code was present, so #7's advisory fixes never applied to
    anything users ran — they were dev-toolchain only.
  - The `web` extra (`fastapi`, `uvicorn`) is still required for `todo web`.

## 0.10.0 - 2026-09-29

- **New `halocli reports run <id>`** (closes #15): executes a report and prints
  `row_count`, `count`, `columns` and `items`, instead of the raw 28 MB Halo
  envelope. `--limit` (default 20) bounds the rows rendered — Halo sends every
  row regardless — and the output says when the CLI trimmed it. `--timeout`
  (default 120s) bounds the wait.
  - **`report.load_error` is now a failure**, not a silent success: Halo
    returns HTTP 200 with the SQL error buried in the payload, so a broken
    query previously looked like a healthy run. It now exits 1 with
    `category: validation` and Halo's own message.
  - **The 50,000-row cap is surfaced.** Measured against 135,148 rows of real
    data: the CLI warns that a full result is not being shown rather than
    letting a truncated set read as complete.
  - Execution failures carry an actionable `hint` for the two slow modes:
    Halo's own gateway 504s at ~60s, and a client timeout. Both were live
    failures first — a 30s profile timeout against this report rendered as
    `{"category": "unknown", "error": ""}`, which says nothing.
  - **Execution does not retry.** Halo's 504 is a gateway deadline on a heavy
    query, not a transient blip (measured ~60s to 504, repeatably); with the
    default 3 retries that clear one-minute failure became a four-minute hang.
    Verified live: one attempt, 61.5s, `category: server` with the hint.
- **New `halocli reports clone <id> --name ...`**: GETs a report, strips the
  identity fields (`id`, `guid`, `published_id`, …) so Halo creates a copy
  instead of upserting over the source, POSTs it as a one-element array, then
  **verifies by reading back** — new id/guid, SQL copied, name applied, and the
  source re-read to prove it was untouched. Verification failures exit 1
  rather than reporting success. Preview is the default and takes zero network
  calls (no profile needed); `--apply --yes` executes, matching every other
  write command.
- Both are declared as `ResourceOperation`s (so `reports --help` lists them in
  the contract format and the coverage oracle verifies them against the vendored
  spec) but carry a `handler`, because the generic dispatcher cannot express
  them. An unknown handler name fails registration loudly rather than falling
  back to generic behaviour that would silently do the wrong thing.
- `HaloClient.request()` accepts a per-request `timeout`, and
  `classify_error()` now recognises timeouts **by exception type**, not only by
  text — httpcore timeouts stringify to `""`, which is why the live failure
  rendered as an empty `unknown` error. Errors with no message fall back to the
  classified message instead of rendering `""`.
- `_capability_summary()`/`_verbs()` now account for declared operations: a
  resource whose only write is an operation (`reports clone`, `invoices pdf`)
  was advertised as "writes only via raw", telling agents a real subcommand did
  not exist.
- **Review on PR #18 caught three defects before merge**, each verified rather
  than taken on trust:
  - **Critical — a regression I had just introduced.** Passing
    `timeout=None` to httpx unconditionally is *not* "use the client default";
    it sets connect/read/write/pool all to `None`, i.e. **no timeout at all**.
    Every request that did not supply one (list, get, raw, search, MCP, todo)
    had silently lost the profile-wide timeout. Neither the suite nor a live
    smoke test could see it — MockTransport ignores timeouts and nothing in
    those runs stalled. The argument is now forwarded only when supplied, and
    a test pins it (verified to fail against the buggy form with
    `{'connect': None, ...}`).
  - A failed verification read after a successful clone POST hid the new id,
    so an operator who retried would create a second copy. It now reports the
    id, `verification: skipped`, and a warning not to retry blindly — on
    stdout, matching `_finish_write`, because losing that id to a stderr
    redirect is exactly what causes the duplicate.
  - An empty result reported no columns, though Halo echoes them in
    `availablefields` (verified present in the execution response for reports
    5/143/146, matching row keys exactly there), so `--output table` rendered
    nothing for a zero-row run.
- The suite grows 289 -> 315 tests, chiefly `tests/test_reports_commands.py`
  (both commands, their failure modes, and the contract/help registration),
  timeout-classification tests in `test_utils.py`, the timeout-forwarding test
  in `test_client.py`, and a guard in `test_packaging.py` that the suite
  imports `src/` rather than a non-editable snapshot in site-packages — twice
  this release cycle a stray `pip install .` left tests passing against a
  stale copy of the tree.

## 0.9.0 - 2026-09-29

- **Python support is now 3.12 – 3.14** (`requires-python = ">=3.12"`); 3.10
  and 3.11 are dropped, and the CI matrix runs 3.12/3.13/3.14 instead of
  3.10/3.11/3.12. Reasons, since dropping interpreters is a real cost:
  - **Python 3.10 reaches EOL on 2026-10-31** (per the PSF devguide it is
    already in security-only status), so keeping it would mean supporting a
    dead version within weeks;
  - the floor was set to 3.12 (EOL 2028-10) rather than 3.11 (EOL 2027-10) so
    the supported range has years of security coverage left;
  - **the floor is enforced, not aspirational** -- verified: installing into a
    3.11 venv fails with `Package 'halocli' requires a different Python:
    3.11.9 not in '>=3.12'`.
  - Users pinned to 3.10/3.11 should stay on **0.8.2**.
- **The MSI now embeds Python 3.14 instead of 3.10** (`release-msi.yml`).
  This is the most consequential line in this release: PyInstaller bundles the
  interpreter into `halocli.exe`, so building on 3.10 meant every per-machine
  install shipped a runtime that goes EOL on 2026-10-31. 3.14 is current
  stable (bugfix through 2030-10). PyInstaller 6.22.3 declares support for
  3.8-3.15, so this is within its supported range.
- Verified locally before changing anything: **289 tests pass on 3.13 and on
  3.14** (and on 3.11, the version being dropped), and the **MSI builds and
  smoke-tests on both 3.13 and 3.14** -- the 3.14 build producing a working
  `halocli.exe` that reported `0.8.2` under the existing smoke test.
- RELEASE.md now warns that **building the MSI from a dev environment bundles
  dev-only dependencies** into the installer: `todo_web.py` imports fastapi
  when it is installed, so a venv with `.[dev]` produces an exe containing
  fastapi/starlette/uvicorn/pytest (20.3 MB locally) while CI's clean
  `pip install . pyinstaller` produces 13.7 MB. Release artifacts come from CI.

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
- **The overlay applier accepts two new target shapes** (the vendor script
  itself stays stdlib-only): `$.paths[...].<method>.parameters[?@.name=='<name>'].description`
  and `$.components.schemas.<Schema>.properties.<prop>.description`, both
  with the same fill-if-missing semantics. Parameters are addressed by a
  JSONPath filter on *name*, so they survive upstream re-ordering and -- unlike
  a positional index -- remain applyable by external overlay runners.
- **The overlay's JSONPath portability claim is now proved, not commented.**
  `test_overlay_targets_are_standard_jsonpath` parses all 85 targets with
  `jsonpath-ng` (new dev-only dependency) and asserts each resolves to exactly
  one node holding its update. This matters: a target that parses but resolves
  to zero nodes would let an external runner silently skip that action while
  applying the rest, with the run still reporting success.
- `resolve_overlay_target()` was split out of `apply_overlay()` so tests can
  assert every overlay target resolves against the committed spec **without
  mutating it** - `schema.load_spec()` caches process-wide, so a mutating
  check would leak into later tests.
- Four new tests: the two new shapes fill and skip correctly, unknown
  parameter/property/schema targets fail loudly,
  `test_report_interface_prose_documented` pins all four findings (plus the
  array schema) so they cannot silently drift out of the spec, and the
  JSONPath interop test above.
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
