# HaloCLI

Standalone HaloPSA CLI for safe operator and automation workflows.

HaloCLI is intentionally independent of Bifrost. It uses direct HaloPSA OAuth
client-credentials auth by default and emits JSON so humans and scripts can use
the same command surface.

## Install

From the latest GitHub release tag:

```powershell
pipx install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.5.0
```

If you use `uv`:

```powershell
uv tool install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.5.0
```

From a local checkout:

```powershell
pipx install --force .
```

If you use `uv`:

```powershell
uv tool install .
```

For development:

```powershell
python -m pip install -e ".[dev]"
```

Supported Python versions are **3.12 – 3.14** (`requires-python = ">=3.12"`).
Python 3.10 reaches end-of-life on 2026-10-31 and 3.11 is security-only
until 2027-10, so both were dropped rather than tested into retirement —
install `0.8.2` if you are pinned to either. The Windows MSI takes no Python
at all: it bundles its own interpreter (Python 3.14 as of 0.9.0), which is
why the MSI build's Python version is a security-relevant choice rather than
a build detail.

Release packaging is documented in `RELEASE.md`. GitHub Releases include the
wheel, source distribution, and a CycloneDX SBOM.

## Configure

Environment variables win over stored profile values:

```powershell
$env:HALO_TENANT_URL = "https://yourtenant.halopsa.com"
$env:HALO_CLIENT_ID = "..."
$env:HALO_CLIENT_SECRET = "..."
$env:HALO_SCOPE = "all"
```

Or create a local profile:

```powershell
halocli configure --auth-mode client-credentials
```

Do not commit profile files or secrets.

## Entra SSO And Interactive Login

HaloCLI can safely discover whether a Halo instance exposes CLI-usable
authorization-code style endpoints:

```powershell
halocli auth discover --tenant-url https://yourtenant.halopsa.com
```

If discovery does not confirm an authorization endpoint, keep using
client-credentials for API automation:

```powershell
halocli configure --profile thomas --auth-mode client-credentials
```

You can create an experimental interactive profile, but `halocli auth login`
will refuse to continue until discovery has confirmed the instance supports the
right browser callback flow. The easiest onboarding sequence is:

```powershell
halocli configure `
  --profile thomas `
  --tenant-url https://yourtenant.halopsa.com `
  --client-id YOUR_HALO_OAUTH_CLIENT_ID `
  --auth-mode halo-interactive

halocli auth discover `
  --tenant-url https://yourtenant.halopsa.com `
  --profile thomas `
  --save

halocli auth login --profile thomas
halocli auth test --profile thomas
```

**Profile defaulting:** when exactly one profile is configured, `--profile`
may be omitted — `default` resolves to it (and to its token cache). With
multiple profiles, an explicit `--profile` is required and the error lists
the configured names.

Interactive login opens the system browser, listens on a temporary localhost
callback, exchanges the authorization code at Halo's token endpoint, and stores
tokens in the operating system's secure credential store. On Windows this is
Windows Credential Manager, backed by Windows data protection behavior. On
macOS this is Keychain. Use `--allow-file-token-cache` only on machines where
secure credential storage is unavailable and you understand the local-file
tradeoff.

The default redirect URL to register in Halo is:

```text
http://127.0.0.1:8765/callback
```

If that port conflicts on a workstation, use `halocli auth login --callback-port
8766` and add the matching redirect URL in the Halo application.

For macOS fleets managed by Intune, treat Intune as the install and config
distribution path first. Entra SSO is a Halo user-login path first. If Halo does
not expose delegated API tokens for CLI use, managed-device identity will need a
separate Entra-backed broker rather than pretending the Intune enrollment is
itself a Halo API credential.

## Diagnosing 403s

Halo gates API access on **two independent layers, both of which must pass**:

1. the **API application's Permissions tab** (OAuth scopes — these are fixed at
   token issuance, so a refresh never widens them; re-run `halocli auth login`
   after changing them);
2. the **logging-in agent's role** and its module access levels.

Scopes only ever *narrow* further — they never grant beyond the agent's role.
Because of that, being a full admin in the Halo UI does not imply an API call
will succeed: the application may simply lack the scope for that endpoint.

`auth whoami` reports the identity the token acts as and the scope Halo
actually granted (read from the local token cache, not the profile's requested
scope), and `--check` probes endpoints read-only:

```powershell
halocli auth whoami --profile thomas
halocli auth whoami --profile thomas --check /Invoice --check /Tickets
```

A 403 response includes a `diagnostic` pointing at this command. `--check`
performs a `GET` with `take=1` — it never issues a write.

## Examples

```powershell
halocli auth test
halocli auth whoami                          # identity + granted OAuth scope
halocli auth whoami --check /Invoice         # is this endpoint reachable now?
halocli auth discover --tenant-url https://yourtenant.halopsa.com
halocli tickets list --open --max-records 25
halocli tickets list --all                   # every record (no ceiling)
halocli clients list --param search=Example
halocli sites get 123
halocli assets list --param client_id=42 --output table
halocli agents list --output table
halocli raw GET /Client --param search=Example
```

## Resource Commands

HaloCLI has registry-driven read commands for common HaloPSA resources:

```text
tickets, clients, agents, teams, users, kb, sites, assets, actions,
statuses, priorities, categories, ticket-types, slas, appointments,
contracts, invoices, invoice-payments, invoice-statuses,
recurring-invoices, opportunities, projects, suppliers, items,
quotations, releases, reports, timesheet-events, canned-text, searches,
outgoing, outgoing-attempts, email-templates, tags, popup-notes,
lookups, outcomes, call-log, mailboxes, charge-rates, address,
agent-check-ins, approval-process, approval-process-rules, asset-groups,
asset-types, automations, billing-templates, booking-types,
budget-types, cabs, call-scripts, client-prepays, consignments,
cost-centres, currencies, custom-buttons, custom-queries, custom-tables,
dashboard-links, database-lookups, distribution-lists,
email-address-books, email-rules, email-stores, events, event-rules,
faq-lists, feeds, feedbacks, fields, field-groups, field-infos,
holidays, incoming-webhook-attempts, invoice-changes, item-groups,
item-stocks, item-stock-histories, journeys, licence-changes,
notifications, notification-messages, organisations, pdf-templates,
products, purchase-orders, qualifications, release-types, roles,
sales-mailboxes, sales-mailbox-details, sales-orders, schedules,
schedule-occurrences, services, service-categories,
service-request-details, service-restrictions, stock-bins, stock-traces,
taxes, templates, ticket-approvals, ticket-areas, ticket-rules,
ticket-type-fields, to-do-groups, user-changes, user-roles,
view-columns, view-filters, view-list-groups, view-lists, workflows,
workflow-targets, formattedemails, workflowsteps, webhooks, workdays,
software-licences, crm-notes, top-levels, expenses, timesheets,
attachments, area-request-types, audits, bulk-emails, cab-members,
cab-roles, crm-note-replies, csp-consumption-data, csv-templates,
call-events, certificates, change-calendars, confirm-closures,
contact-groups, contact-group-contacts, contract-rules,
contract-schedules, contract-schedule-plans, device-licences,
distribution-list-logs, downtimes, email-template-variables,
historical-ticket-volumes, invoice-detail-prorata, mail-campaign-logs,
meter-readings, escalation-messages, powershell-scripts,
powershell-script-criteria, powershell-script-processing,
product-branches, product-components, publish-profiles, recurring-items,
release-note-groups, release-pipelines, remote-sessions,
report-repositories, resource-types, saved-forecasts,
service-availabilities, service-statuses, single-sign-on-attempts,
software-licence-roles, supplier-contracts, tax-rules,
ticket-type-groups, timeslots, to-dos, transcription-stores,
xtype-roles, csp-invoices, item-suppliers, asset-changes,
asset-software, incoming-emails
```

54 of these are **route-verified reads whose tenant holds no rows yet**: their
`list` returns whatever Halo returns (usually `count: 0` — every documented
and scope parameter was probed live on 2026-10-02) and the table view shows
`id` only until a populated tenant yields column evidence. JSON output always
carries Halo's raw payload.

Each resource supports:

```powershell
halocli <resource> list --param key=value --max-records 25
halocli <resource> get ID
```

Undocumented params are warned about, not silently dropped: Halo ignores
unknown query params (`--param assigned_to=37` on tickets returns the
*entire* tenant), so `list` warns per param against the spec and suggests
close matches when they exist (`--param clinet_id=1` → "did you mean
`client_id`?"). `/Tickets` alone documents 194 params — the warning exists
because a filter that silently does nothing is worse than an error.

`list` stops at **500 records** by default and says so: HaloPSA tenants can be
large (`/Tickets` on our own instance holds 137k records — an unbounded fetch
would take ~18 minutes and look like a hang). When the ceiling is hit the
payload carries `"truncated": true`, `"total_available"`, and a `hint`; a
complete result never carries them. Pass `--all` to fetch every record, or set
`--max-records`/`--max-pages` explicitly. `--all` cannot be combined with those
two flags.

**Halo's rate limit: 700 API requests per rolling 5 minutes** (Fair Use
Policy — exceeding it returns `429 Too Many Requests`; native/Halo-internal
integrations are exempt). HaloCLI already treats429 as retryable with the
server's `Retry-After` honored, and the default ceilings keep ordinary use
far under budget — sequential fetches are why a full `--all` walk of even a
137k-row tenant stays comfortably inside the limit.

**Bulk is a bounded client-side loop — Halo has no bulk API** (DTC's own
"Bulk Updates" tooling is PowerShell looping over the REST API). The
HaloCLI pattern: page through targets with `list` (`--param` filters +
`--max-records`), build payloads from what you read, preview every write
(zero network), then apply sequentially with `--apply --yes` — which the
rate limit above rewards. A first-class JSONL bulk driver is an open
decision (issue #73): whatever shape it takes inherits the
preview-all-then-apply contract so a bulk run is never a blind blast.

Some endpoints ignore paging entirely (`/CRMNote` answers every `page_no` with
page 1). The list loop detects the repeated page, never appends it, recovers
the full set with one `count=<total>` request, and reports
`"paging_ignored": true` — so a lying pager can no longer duplicate rows (the
`/CRMNote` case went from 150 items/50 distinct to 133/133, verified live).

`/Feed` is a different animal: an activity stream with **no page numbers at
all** — the pageinate trio is ignored (page 2 is page 1 verbatim, `page_size`
discarded). Its own documented params are `count` (window size) and
`older_than_id`/`newer_than_id` (position), so `list` (and `sync`) walk
`count` windows via the cursor instead of looping pages: windows are
disjoint and boundary-exclusive (proven live), rows are deduped by id, and
four independent guards — short window, non-advancing cursor, repeated-id
stall, and the record ceiling — guarantee termination. A `--all` fetch on
`feeds` genuinely streams the whole feed (~166k rows here) rather than
re-reading one window forever.

**Labels are resolved automatically.** Halo returns bare foreign keys
(`status_id: 9`, `priority_id: 4`) and only occasionally denormalises a name,
so `list`, `get` and post-`apply` results hydrate the tenant's own label
beside every resolvable id — `status_name: "Closed"`, `priority_name: "Low"` —
using a bounded lookup read per entity (detail-fetch fallback for ids past the
first page). Raw ids are never touched, labels Halo already sent are never
overwritten, and table output prefers the label column. Opt out with
`--no-labels`. Write *previews* stay zero-network by contract, so they show
the ids that go on the wire; read the labels with `get`/`list`. Quirks:
priorities are GUID-keyed while tickets store integers (joined via
`priorityid`, since `/Priority/<int>` 404s), and unresolvable ids
(e.g. external-system references) simply stay bare.

The `reports` resource also declares two nested operations, both verified live
against the tenant:

```powershell
halocli reports run 147 --limit 20                  # execute; row_count/columns/items
halocli reports clone 147 --name "A copy"           # preview (zero network)
halocli reports clone 147 --name "A copy" --apply --yes   # create it
```

`reports run` exits non-zero when Halo cannot execute the query — note that
Halo reports SQL failures with **HTTP 200** and the error buried in the
payload — and warns when the result reaches Halo's 50,000-row cap. Execution
is deliberately not retried: Halo's gateway answers 504 at roughly 60s, so a
retry would re-run the same expensive query instead of failing fast.

Resources with nested endpoints also expose them as first-class commands
(`halocli <resource> --help` lists them with method, path and summary):

```powershell
halocli tickets zapier                        # GET /Tickets/zapier
halocli invoices lines                        # GET /Invoice/lines
halocli invoices pdf 42 --save invoice.pdf    # POST /Invoice/PDF/{id}, binary -> file
halocli quotations lines --data lines.json --apply --yes   # POST /Quotation/Lines (array body)
halocli attachments get-image 7 --save img    # GET /Attachment/image/{id}
halocli attachments upload-image --file pic.png --apply --yes
```

These are generated from `ResourceOperation` metadata, so every path and
method is verified against the vendored spec by the coverage oracle.
Behaviour worth knowing:

- **Path arguments are arity-checked** — a missing or extra positional fails
  with usage (exit 2) before anything is sent.
- **Reads never take `--apply`/`--yes`**; writes preview by default (zero
  network, no profile needed) and require `--apply --yes` to execute.
- **Binary responses** (PDFs, images) are never dumped into JSON: pass
  `--save PATH` to write them to a file, otherwise you get byte counts.
- **Verification provenance** appears in `--help` per operation: `live`,
  `live:403`/`live:500`/`live:404` (probed against the real tenant),
  `route-verified` (route confirmed, no live data), or `spec`
  (spec-documented, not probed — writes are never fired at a tenant
  without an operator).

Sixty-four resources carry write metadata and first-class write commands:
the ticketing core (tickets, actions, statuses, priorities, kb, canned-text),
the people/CRM layer (clients, sites, assets, agents, appointments, users,
crm-notes, timesheet-events), writes-batch-1's config/reference set —
tags, outcomes, categories, ticket-types, ticket-areas, releases,
release-types, email-templates, faq-lists, cost-centres, budget-types, cabs,
call-scripts, qualifications, asset-groups, asset-types, item-groups,
item-stocks, stock-bins, service-categories, services, pdf-templates and
to-do-groups — and writes-batch-2's set: organisations, teams, suppliers,
slas, workdays, products, fields, field-groups, field-infos, custom-tables,
holidays and lookups - plus writes-batch-3's set: crm-note-replies, certificates, email-template-variables, release-note-groups, release-pipelines, ticket-type-groups, item-suppliers, product-components, and the POST-only tier (agent-check-ins, call-log, to-dos - the spec offers no DELETE /{id}, so the CLI withholds the command) - plus the tackle-the-25 set: address, contact-groups, contact-group-contacts and contract-schedule-plans. All batches' routes are spec-verified (`POST` on the
collection, `DELETE /{id}`) but were never fired at a tenant
(`verification: spec`); their single required create field is the observed
primary column — a documented house assumption, since the spec declares no
required fields anywhere.
`users` carries the live-proven create set plus account actions —
including the end-user MFA reset:

```powershell
halocli users update <id> --data '{"_revoke_authenticatorapp": true}' --apply --yes
halocli users update <id> --data '{"resetpassword": true}' --apply --yes
halocli users create --data user.json              # firstname, surname, name,
                                                  # emailaddress, client_id, site_id
```

Passwords passed via `--data` are masked as `***` in preview and result output
(the request itself carries the real value). `contracts` had deferred its writes pending an operator decision; the tackle-the-25 pass (2026-10-02) resolved it: the contract core is now **argued-raw with final, schema-cited reasons** (creation carries prepay auto-topup fields and `_send_appointment_invites`/`_send_outstanding_emails` flags; approval carries a signature/token; `POST /SupplierContract` sits behind a 403 scope), while `contracts next-ref` is first-class and the contract visit-plan children gained full CUD. Contact and address writes (`contact-groups`, `contact-group-contacts`, `address`) joined the same pass. Writes
are **preview by default**: without flags
they validate the payload and print what would be sent, with zero network
calls. Executing requires both `--apply` and `--yes`:

```powershell
halocli tickets create --data payload.json                      # dry-run preview
halocli tickets create --data payload.json --apply --yes        # executes
halocli tickets update 123 --data payload.json --apply --yes
halocli tickets delete 123 --apply --yes
```

Raw write-capable requests require both `--apply` and `--yes`:

```powershell
halocli raw POST /Tickets --data payload.json --apply --yes
```

`raw` validates the method, path and body against the vendored OpenAPI spec
before sending. Unknown endpoints and missing required body fields are
refused; pass `--no-validate` to bypass the check (spec warnings are returned
as `spec_warnings` either way).

## Endpoint Coverage

Every operation in the vendored spec (1,455 of them) carries an explicit
disposition in `coverage_ledger.json` — coverage is enforced, not aspirational:

| Disposition | Meaning | Count |
|---|---|---|
| `first-class` | reachable as a real `halocli` command today | 506 |
| `backlog` | promotion candidate or unprobed — reason in `note` | 8 |
| `deliberately-raw` | argued to stay raw (billing risk, integrations, secrets) | 908 |
| `dormant` | known-dead on this tenant (live evidence in `note`) | 10 |
| `junk` | vestigial/duplicate in the spec | 23 |

```powershell
python scripts/build_coverage_ledger.py          # regenerate after spec/policy changes
python scripts/build_coverage_ledger.py --check  # what CI runs; exit 1 if stale
python scripts/prod_get_sweep.py                 # re-probe every GET (read-only)
```

Classification lives in `coverage_policy.json` (segment-level, human-owned,
plus per-operation overrides for live-evidence flips). A spec re-vendor that
introduces an unclassified segment fails CI until a human decides — the ledger
is the spine of the endpoint-promotion effort.

**Live evidence:** `sweep_results.json` holds the 2026-10-02 production GET
sweep — all 797 GET operations probed read-only (358 × 200, 292 route-verified
via validation-400, 149 backlog candidates now live-200, plus the 401/403/404/
500/timeout findings that produced the `dormant` flips above). The sweep
runner is structurally write-free (GET-only code path).

## Release & Version Tracking

Three committed artifacts keep "what build runs where, and what's out
there" answerable offline:

- **`halo_version_snapshot.json`** — per-instance `/InstanceInfo`
  captures with our belief-state (spec sha, op counts) at capture time.
  Current record: `midtowntg` (prod) and `dtcdev.halopsa.com` — the
  swagger upstream we vendor from — both run **2.236.133** on stable
  tracks, while the `clidev` trial runs **2.250.32**. Re-run with
  `python scripts/halo_version_snapshot.py --profile dev` (or
  `--public-url` for credential-less instances — `/InstanceInfo` answers
  without auth on these builds).
- **`halo_release_ladder.json`** — the community release ladder parsed
  from [usehalo.co](https://www.usehalo.co/) (community-run, not
  affiliated with Halo): versions with Stable/Beta/Unreleased status and
  dates, plus `current: {latest_stable, latest_beta, latest_unreleased}`.
  Re-run `python scripts/release_ladder.py` when releases move.
- **The track nuance that matters**: hosted-group *names* mislead
  (`USDEMODB2-TRIALS1` is not the beta track) — the ladder shows2.250
  went **Stable on 28 Sept 2026**, so the trial runs a *current GA build*
  ahead of prod, not a beta. Conversely `check_spec_currency` compares us
  to *dtcdev* (stable2.236) — surface newer than that (2.252+ beta,2.254/2.256
  unreleased) is spec-uncovered by definition; trial probes are the only
  lens on it, and the snapshot's drift flags mark stale captures.

## Endpoint Discovery

Two kinds of search, split at 1.0.0:

```powershell
halocli search backup                    # LIVE tenant search: tickets, articles,
                                         # clients, users, assets, services
halocli search server --limit 10         # shape the output (row_count/entity_counts)
halocli search server --count-per-entity 20   # per-entity cap (server default ~5)

halocli catalog invoice                  # OFFLINE: registry + vendored spec discovery
halocli catalog "site" --limit 5
```

**Breaking in 1.0.0:** `halocli search` used to mean offline discovery — that
is now `halocli catalog`. A live-search invocation with more than one term
fails with this migration hint rather than querying the tenant. `catalog`
searches both the resource registry and the vendored HaloPSA OpenAPI spec
(927 paths) offline:

Refresh the vendored spec whenever Halo revs its API:

```powershell
python scripts/vendor_halo_spec.py
```

The refresh is not a plain copy: the vendor script also **enriches** the spec
so it is usable by typed consumers (Forge, Fern, OpenAPI tooling), which
otherwise choke on Halo's near-empty metadata (upstream ships 3 operationIds
for 1455 operations):

- missing `operationId`s are synthesized deterministically as
  `{method}_{path}` (e.g. `get_invoice_pdf_id`); upstream IDs are kept;
- `src/halocli/spec/halo_overlay.json` (OpenAPI Overlay 1.0.0) fills prose the
  upstream spec omits: `summary`/`description` for every operation the registry
  surfaces, query-parameter descriptions (e.g. `loadreport`), and schema-property
  descriptions (e.g. `AnalyzerProfile.sql`) for behaviours learned by live API
  testing. Applied with
  fill-if-missing semantics so upstream improvements survive refreshes.
  When the registry grows, `tests/test_spec_enrichment.py` fails until the
  overlay is extended — edit the overlay JSON directly.

Then re-run the coverage oracle to see what the new spec adds:

```powershell
python scripts/coverage_report.py --top 25   # JSON report
python scripts/coverage_report.py --check    # CI gate: exit 1 on write mismatches
```

The oracle diffs the vendored spec against the resource registry offline. It
reports first-class coverage (`list`/`get` per operation), ranks uncurated
roots as curation candidates (roots of existing resources first — extending
`Attachment` or `Tickets` is cheaper than adopting a new subsystem), and
verifies registry promises against the spec in both directions:

- `write_mismatches` — create/update/delete metadata pointing at endpoints the
  spec does not support (the `/Contract` bug class; must stay empty),
- `read_mismatches` — registry endpoints absent from the spec entirely
  (undocumented reads that work today but nothing verifies).

`tests/test_coverage.py` pins the current state of both lists, so a spec
refresh that changes them fails the suite and forces a re-evaluation.

## Offline Mirror: sync, sql, standup, triage

`halocli sync` mirrors registry resources into a local SQLite database so
cross-resource questions answer instantly instead of re-paging the tenant or
guessing filter params (`--param assigned_to` silently returns all 137k
tickets). Everything below reads the local file — nothing here writes to
Halo, and the write contract is unchanged.

```powershell
halocli sync                          # ops-relevant core, bounded (500 rows/resource)
halocli sync --all-resources          # the whole registry
halocli sync -r tickets --all         # one resource, no ceiling
halocli sql "SELECT status_name, COUNT(*) AS n FROM tickets GROUP BY status_name"
halocli triage --stale-days 7 --limit 10
halocli standup --since 24h
```

Design notes (evidence: `mirror_evidence.json`,
`scripts/mirror_evidence_probes.py`, read-only probes):

- **JSON-first rows + per-resource views**: rows live in `mirror_rows`; a
  view per resource (`SELECT summary FROM tickets`) projects
  plain-identifier keys via `json_extract`, and exotic keys stay reachable
  through the `data` column. Rows sharing an id across parents survive
  (sequence-numbered) instead of overwriting each other.
- **Sync-time label hydration** bakes `agent_name`/`status_name`/
  `client_name` into the mirror (ticket payloads ship none — proven), so
  digests and SQL never need mapping tables.
- **Bounded by default**: 500 rows per resource, `--all` opts out,
  truncation recorded in `mirror_state` and echoed by `triage`/`standup` —
  a partial tickets mirror says so instead of implying completeness.
- **Failures keep data**: a resource that errors is recorded in the
  summary; its previous rows stay (a transient 500 must not wipe the mirror).
- **`sql` is SELECT-only** against the local file — single statement,
  conservative keyword guard, `--limit` reported not implied. It never
  reaches Halo; Halo's server-side SQL surface stays deliberately raw —
  this is not that.
- Tenant-specific facts proven live: status ids are not portable (no
  hardcoded closed-id set — names are configurable via `--closed-status`),
  `dateclosed` drives "closed in window", `/Feed` is cursor-based
  (`count` + `older_than_id`; walked by `list`/`sync`, never
  page-walked — page numbers are ignored there), and `/Actions`
  unfiltered times out (excluded from the core sync set).

Ideas adapted from Servosity's msp-skills halopsa CLI (Apache-2.0) — the
SELECT-only guard stance and the hand-written digest concept; every field
note above was re-proven against our own tenant.

## Ad-hoc SQL Through Halo's Reporting API

Halo has no SQL endpoint — it executes SQL through the **reporting
API**, and the contract is peculiar enough to write down (live-confirmed
on the trial 2026-10-03; distilled from
[Mtg-Thomas/HaloSQLStudio](https://github.com/Mtg-Thomas/HaloSQLStudio)):

```powershell
# the body is a ONE-ELEMENT ARRAY; _testonly runs it without saving
halocli raw POST /Report --apply --yes --data '[{"sql": "SELECT TOP 5 name FROM tickets", "_testonly": true, "_loadreportonly": true}]'
```

- **Errors are HTTP 200**: read `report.load_error`; success is
  `loaded: true` with `available_columns[]` (`name`, `data_type`) and
  `report.rows[]` (objects keyed by column name, values as strings).
- **One statement only** (a second fails: *"Incorrect syntax near ';'"*,
  proven live) and **no `--` comments** (also rejected live — use
  `/* */` block comments).
- **View compatibility:** SQL you *save* as a report backs a Halo VIEW —
  no CTEs, window functions (`ROW_NUMBER`, `LAG`, …), `OFFSET/FETCH`,
  `ORDER BY` *inside view definitions*, temp objects, hints or `EXEC`.
  Ad-hoc `_testonly` execution was more lenient on our trial
  (2.250.32 accepted `ORDER BY`), so treat the view rules as the
  save-time constraint and the two-statement/`--` rules as universal.
- **Scopes are their own permission set**: `read:reporting
  edit:reporting` on the API application (plus `offline_access` for
  refresh) — separate from `all:standard`.
- **Variables** are substituted *client-side before sending*:
  `$agentid`, `$siteid`, `$clientid` (plain numeric ids only) and
  `@startdate`, `@enddate` (quoted `'YYYY-MM-DD'`); left empty, Halo
  substitutes the logged-in context.
- **Metadata is SQL too**: tables/columns via
  `INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo'`. Listing saved
  reports does NOT need SQL though — REST exists:
  `GET /ReportRepository` (params include `loadreport`,
  `reportingperiod*`, `report_access_token`, `reportgroup_id`); the
  `AnalyzerProfile JOIN LOOKUP … fid=41` query is the fallback trick
  SQLStudio uses from inside the SQL console itself.
- **Saving**: `POST /report` (lowercase!) with `[{sql, name,
  description, id?}]` → `{id}` (`id` present = update).
- **Running saved reports** is `halocli reports run` (first-class:
  retries disabled because a timeout would re-run an expensive query —
  Halo's 504 is a gateway deadline, not a blip). The same never-retry
  rule applies to ad-hoc `_testonly` executions.

## Advanced Configuration: Workflows, Runbooks & Rules

These surfaces are *trigger/side-effect configuration* — a misconfigured
rule or workflow fires notifications, automations and status changes
across the whole tenant, not against one record, which is why the family
was kept raw for so long. **Workflows are now promoted to first-class**
(preview/apply create, update and delete) after a full round-trip on the
trial — see "Creating a workflow" below; rules, event rules and
automations stay raw behind the deliberate gate. Raw never meant
"undocumented": the shapes below are captured live from the
trial (evidence: `advanced_config_evidence.json`, re-run with
`python scripts/advanced_config_probes.py --profile dev`).

**The safe method** (identical for every raw surface):

1. Read the real object first — never author from imagination:
   ```powershell
   halocli workflows list
   halocli raw GET "/Workflow/3" --param includedetails=true   # full document
   halocli raw GET /workflowstep --param includecriteriainfo=true
   halocli raw GET "/TicketRules/12" --param includedetails=true
   ```
2. Build the payload from what you read (copy the shape, change the
   intent), then **preview** — zero network, spec-validated:
   `halocli raw POST /Workflow --data @payload.json` (no `--apply`).
3. Fire with **`--apply --yes`, against the trial first** — these
   objects react to live tenant traffic the moment they are active.
4. Verify by re-reading (and watch `/Automation` — the run log — to see
   what your change actually did).

**Mental model** (proven shapes):

- **Workflow** (`/Workflow`) is a container: list rows are thin
  (`id, name, active, note`); `GET /Workflow/{id}?includedetails=true`
  returns the whole document — `stages[]` (sequence positions),
  `steps[]` (each with `isstart/isend`, `steptype`, `duration`,
  `pipeline_stage_id`, and its `actions[]`), `targets[]`,
  `always_allow_actions[]`, and a `flow_chart_json` string (the visual
  designer's canvas — keep whatever you read unless you mean to redraw
  it).
- **Step actions** are where the power sits (read them standalone via
  `/workflowstep`): each action carries `action_id/outcome`, branching
  `conditions[]`, approvals (`approval_result`), todos, time-limit
  actions, **runbook bindings (`automation_runbook_name`) with
  `runbook_variable_mappings[]`**, and chat bindings. Editing a
  workflow means round-tripping this document.
- **Rules** (`/TicketRules`, `/EventRule`) are the triggers: `criteria[]`
  rows (`tablename` + `fieldname` + `value_*`, with `partialmatch` /
  `matchseparatedvalues` semantics), optional `criteria_groups` and
  `database_lookups[]`, then the mutation set (`new_agent_id`,
  `new_priority_id`, `new_status_id`, `new_sla_id`, `new_template_id`,
  **`new_workflow_id`** — this is how a rule routes a ticket *into* a
  workflow), ordered by `precedence` with `stopmatching` as the
  short-circuit flag, plus `outcome_id` (the button outcome that fires
  it) and `events[]` for event rules.
- **`/Automation` is the run log, not the definition**: rows record what
  fired (`workflow_id/step/seq`, `runbook_name`, `ticket_id`, `status`,
  `error`, retries, and a `trace[]` of log lines on the detail read).
  Use it to audit; **`POST /Automation/{runbookId}` (body
  `{"formCollection": {...}}`) is the manual runbook execution entry** —
  a real side effect, so preview first and trial first.
- **`/workflowstep` is GET-only in the spec** — steps are edited as part
  of the workflow document, not via their own endpoint.

### Creating a workflow (first-class — the proven recipe)

```powershell
halocli workflows get 18 --param includedetails=true   # read Halo's own example first
# ...copy that document and edit intent, applying the sanitize list...
halocli workflows create --data @workflow.json          # preview: zero network
halocli workflows create --data @workflow.json --apply --yes
halocli workflows delete <id> --apply --yes
```

The in-box library is the best teacher: the trial ships ten Halo-shipped
workflows (Incident Management —5 steps/45 actions; KB Draft Workflow —
the small one used as the template). The crux is **sanitizing**, because
Halo's POST-with-id = update convention means identity must never ride
along: strip top-level `id`/`guid`/`in_use`/`notinuse`, stage
`id`/`guid`/`translations`, step `guid`/`fdid`, action `id`/`flow_id`;
repoint `flow_id` links to `0`; and **force `active: false`** so the new
workflow is inert until you deliberately activate it (nothing references
it, so it can never fire on live traffic). Proven end-to-end 2026-10-03:
created id 19 from KB Draft's document, GET-verified (stages/steps/
actions/flow chart all carried), DELETE-verified gone
(`scripts/workflow_create_probe.py`).

### Runbooks: where the definitions live

- **The method library is readable**: `GET /CustomIntegrationMethod`
  (43 methods on the trial — e.g. *"OpenAI: Analyse End-User Sentiment"*
  with base URL, path, auth type) plus `GET /CustomIntegrationMethodValue`
  (step input/output mappings) and `GET /IntegrationRunbookVariableGroup`
  (the variable palette — "Ticket Variables" → `faults`).
- **Execution and history**: `POST /Automation/{runbookId}` (body
  `{"formCollection": {...}}`) triggers a run; `GET /Automation` is the
  per-run log (`workflow_*`, `runbook_*`, `status`, `error`, `trace[]` —
  runs fired by tickets our sweep created appear there).
- **Definitions are UI/JSON territory**: the official guide
  (<https://www.usehalo.com/guides/1630>) documents *Configuration >
  Integrations > Custom Integrations > Integration Runbooks* with
  **Import from JSON** as the interchange format — there is no spec
  endpoint for the definition itself, so we encode the shape knowledge
  rather than inventing a route. Workflow step automations (trigger
  types: immediate / N minutes after / N days before a date field, plus
  sequencing since 2.228) are covered by
  <https://www.usehalo.com/guides/2355>.

**What stays raw, per family** (final reasons in `coverage_policy.json`):
rules/event-rules/automations (side effects above), `EmailRule`
(outbound mail), `CustomQuery`/`DatabaseLookup` (raw SQL),
integration plumbing (`IntegrationData/*` — sync state, not operator
input), `ScreenLayout`/`View*` (per-agent UI chrome), and the
money-adjacent rules (user-accepted stance). **Promoted twice on live
evidence:** custom fields (schema authoring) and now workflows
(round-trip create → verify → delete) — both keep preview/apply as the
deliberate gate.

## Quick-Work Recipes (field-tested in the dispatch portal)

Distilled from
[Mtg-Thomas/halodispatchportal](https://github.com/Mtg-Thomas/halodispatchportal)
— the handrolled pathways a production dispatch tool relies on:

- **One-call bootstrap**: `GET /ClientCache?iscachebuild=true` returns
  agent, statuses, ticket types, ticket areas, field infos, agents,
  lookups, fields and mailboxes in a single payload — enough to power
  every picker without N round trips. Now first-class:
  `halocli client-cache list` (promoted from *junk* on this evidence —
  the live probe is in `scripts/reporting_api_probe.py`).
- **Saved lists as filters**: Halo's lists have no AND/OR/grouping, so
  the portal flattens them: `GET /viewlists?showcounts=true&domain=reqs
  &type=reqs&ticketarea_id=<area>&utcoffset=<min>` → pick `list_id`s →
  `GET /Tickets?...&list_id=<L>` (one request per list, then merge and
  dedupe client-side). `ViewFilter` is optional — the filter id already
  rides on the list row.
- **Appointment writes are partial patches over one array-wrapped POST**:
  move = `{id, start_date, end_date, agent_id}`; resize =
  `{id, start_date?, end_date?}`; **complete = `{id,
  complete_status: 0, complete_notehtml, complete_timetaken}`** —
  completion rides the *appointment* (0 = done, 1 = in progress — the
  same convention our write sweep proved), never a ticket status write.
- **Minimal ticket create** (numbers may travel as strings):
  `[{tickettype_id, summary, details_html, impact, urgency,
  category_1, team, agent_id, user_id}]`; add `id` to update.
- **Appointment defaults worth knowing**: `event_type: "a"`,
  `reminderminutes: 15`, `agent_status: 1`,
  `open_appointment_status: 0`, `appointment_location: 0`; the
  appointment type id came from `lookup?lookupid=63` — a *tenant
  convention*, verify yours before copying.
- **`utcoffset` is minutes EAST of UTC** (240 = US/Eastern) — sign
  matters; the portal flips JavaScript's `getTimezoneOffset()` to get
  it.

## MCP Server (Code Mode)

`halocli serve` runs a code-mode MCP server over stdio (newline-delimited
JSON-RPC 2.0, no extra dependencies). Instead of exposing one tool per Halo
operation — which floods the model's context — it exposes exactly three tools
with rich descriptions:

| Tool | Purpose |
| --- | --- |
| `halo_search` | Discover resources/operations (registry + OpenAPI) |
| `halo_execute` | Run one REST call with server-side guardrails |
| `halo_resources` | Dump the full resource catalog when search misses |

Guardrails are enforced server-side: non-GET methods require `apply: true`
(otherwise you get a structured refusal and no request is sent), and
responses are bounded at ~40,000 characters so a large collection cannot
flood the context window.

Register it with an MCP client (example for a generic MCP config):

```json
{
  "mcpServers": {
    "halo": {
      "command": "halocli",
      "args": ["serve"]
    }
  }
}
```

`halocli --version` measures ~1.1s on cold start (Python + Typer import); the
OpenAPI spec loads lazily and is not part of that cost. The MCP server is a
long-lived process, so its startup is paid once.

## Todo And Microsoft To Do Preview

HaloCLI includes a slim Todo surface for experimenting with lightweight Halo
work items without Bifrost. Microsoft To Do access uses the shared
`mtg-microsoft-auth` backend and defaults to read-only `Tasks.Read`.

Live Microsoft To Do import requires the optional auth backend:

```powershell
python -m pip install -e ".[microsoft-todo]"
```

```powershell
$env:TODO_CLIENT_ID = "e02be6f7-063a-46a6-b2cc-109d5f51055c"
$env:TODO_SCOPES = "Tasks.Read"
halocli todo import-ms --max-records 10
```

Preview from captured JSON instead of live Graph:

```powershell
halocli todo import-ms --source-json microsoft-todos.json
```

Create a lightweight Halo Todo backed by Halo's `Appointment` API (preview
first, like every write — pass `--apply --yes` to fire):

```powershell
halocli todo add "Independent todo list front end for HaloPSA" --owner 37 --due 2026-04-26 --tag microsoft-todo --tag halo-todo --apply --yes
```

List, inspect and complete Halo todos (these are `Appointment` rows with
`is_task` — the API's own `/ToDo` table is empty on this tenant, so
`to-dos list` returns nothing while `todo list` is the real surface):

```powershell
halocli todo list                       # open tasks, server-side filters
halocli todo list --mine --max-records 50
halocli todo list --status done         # or: --status all
halocli todo get 38790
halocli todo complete 38790             # preview: shows the exact payload
halocli todo complete 38790 --apply --yes
```

Halo's completion convention (proven live,331 tasks): `complete_status`
is **0 = done, -1 = open** — `list` filters server-side with
`tasksonly`/`hidecompleted` and pages properly, so todos beyond the first
page of appointments are not silently invisible.

Run the local-first Todo HTTP API from the same HaloCLI profile:

```powershell
python -m pip install -e ".[web]"
halocli todo web --profile midtown --host 127.0.0.1 --port 8766
```

The server exposes normalized Todo JSON over Halo appointment tasks —
`/api/todos`, `/api/clients`, `/api/tickets` and `/api/me` — with interactive
docs at `/docs`. HaloPSA remains system of record; the API does not create a
local database. HaloCLI no longer bundles a browser UI (removed in 0.11.0):
`/` returns service info so any external front end can discover the endpoints.

Todo priority is currently stored as HaloCLI metadata in the backing
appointment `note_html`; it is not mapped to Halo ticket priority or a native
Halo appointment priority field.

## Bifrost Compatibility

This package does not import Bifrost. Bifrost workflows can shell out to
`halocli` when a direct HaloPSA operator path is useful, or a future optional
backend package can bridge to Bifrost-specific auth/runtime behavior.

The Bifrost workspace may keep its own Bifrost-backed helper while this package
stays portable.

## License

HaloCLI is released under the GNU General Public License v3.0. See
`LICENSE` for details.

See `THIRD_PARTY_NOTICES.md` for attribution to `netaryx/pyhalopsa`, which
served as prior art for this project.

## Windows MSI

Tagged releases build a per-machine Windows MSI that installs `halocli.exe` under `Program Files` and adds that install directory to the system PATH. Installing or uninstalling the MSI requires an elevated prompt.
