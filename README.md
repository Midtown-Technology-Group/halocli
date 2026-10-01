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
tickets, clients, agents, teams, users, kb, sites, assets, actions, statuses,
priorities, categories, ticket-types, slas, appointments, contracts, invoices,
opportunities, projects, suppliers, items, quotations, releases, reports,
webhooks, workdays, software-licences, crm-notes, top-levels, expenses,
timesheets, attachments
```

Each resource supports:

```powershell
halocli <resource> list --param key=value --max-records 25
halocli <resource> get ID
```

`list` stops at **500 records** by default and says so: HaloPSA tenants can be
large (`/Tickets` on our own instance holds 137k records — an unbounded fetch
would take ~18 minutes and look like a hang). When the ceiling is hit the
payload carries `"truncated": true`, `"total_available"`, and a `hint`; a
complete result never carries them. Pass `--all` to fetch every record, or set
`--max-records`/`--max-pages` explicitly. `--all` cannot be combined with those
two flags.

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

Ten resources (tickets, actions, clients, sites, assets, agents,
appointments, statuses, priorities, kb) also have write metadata and
first-class write commands. `contracts` stays read-only by deliberate
choice, not a missing route: the spec does document `POST /ClientContract`
(standard Halo upsert), but client contracts carry billing machinery and
the semantics would narrow to *client* contracts only (`POST
/SupplierContract` is a different, permission-gated entity), so promotion
waits for an explicit operator decision. Writes
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

## Endpoint Discovery

Find the right endpoint without leaving the terminal. This searches both the
resource registry and the vendored HaloPSA OpenAPI spec (927 paths) offline:

```powershell
halocli search invoice
halocli search "site" --limit 5
```

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

Create a lightweight Halo Todo backed by Halo's `Appointment` API:

```powershell
halocli todo add "Independent todo list front end for HaloPSA" --owner 37 --due 2026-04-26 --tag microsoft-todo --tag halo-todo
```

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
