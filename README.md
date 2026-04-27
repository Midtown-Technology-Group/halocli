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

## Examples

```powershell
halocli auth test
halocli auth discover --tenant-url https://yourtenant.halopsa.com
halocli tickets list --open --max-records 25
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

Raw write-capable requests require both `--apply` and `--yes`:

```powershell
halocli raw POST /Tickets --data payload.json --apply --yes
```

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

Run the local-first Todo web UI from the same HaloCLI profile:

```powershell
python -m pip install -e ".[web]"
halocli todo web --profile midtown --host 127.0.0.1 --port 8766
```

The web UI serves a compact three-pane task triage surface over Halo appointment
tasks: quick capture, Inbox/Today/Upcoming/Blocked/Completed views, search,
keyboard selection, completion, detail editing, notes, and source metadata for
imported Microsoft To Do items. HaloPSA remains the system of record; the UI API
returns normalized Todo JSON and does not create a local database.

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
