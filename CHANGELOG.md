# Changelog

All notable changes to HaloCLI are documented here.

HaloCLI uses semantic-ish versioning while it is young: patch releases are
small fixes and packaging polish, minor releases may add commands or change
operator workflows, and major releases are reserved for breaking CLI behavior.

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
