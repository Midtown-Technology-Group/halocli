# Changelog

All notable changes to HaloCLI are documented here.

HaloCLI uses semantic-ish versioning while it is young: patch releases are
small fixes and packaging polish, minor releases may add commands or change
operator workflows, and major releases are reserved for breaking CLI behavior.

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
