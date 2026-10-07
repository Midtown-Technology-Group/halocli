# Release Checklist

This project ships installable console-tool releases from GitHub tags. PyPI can
be added later with trusted publishing, but the minimum release path does not
require local package-upload tokens.

## Local Preflight

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
python -m pytest -q
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
python -m build
python -m twine check dist/*.tar.gz dist/*.whl
pip-audit --progress-spinner off --skip-editable .
cyclonedx-py environment --output-format JSON --output-file dist/halocli-sbom.cdx.json
```

The editable install is not optional: the packaging tests read the installed
distribution metadata (`importlib.metadata`), which a source-tree-only run
cannot see.

Also run a console smoke test from an installed package:

```powershell
python -m pip install --upgrade --force-reinstall .
halocli --version
halocli --help
halocli auth discover --help
halocli raw POST /Tickets --data "{}"
```

The raw write command should exit nonzero and refuse the request unless
`--apply --yes` is present.

## Tag A Release

```powershell
git tag v0.5.0
git push origin v0.5.0
```

Pushing a `v*.*.*` tag runs the release workflow, builds the wheel and source
distribution, generates a CycloneDX SBOM, runs `pip-audit`, and attaches the
artifacts to a GitHub Release.

## Resuming An Interrupted Release

`scripts/release.py` is idempotent: re-run the same command and completed
steps are detected and skipped instead of repeated.

- **Tag:** created on HEAD, or reused after verifying it already sits on HEAD
  and the tree under it declares the same `pyproject.toml` version. A tag on
  any other commit, or declaring a different version, aborts the run.
- **Tag push:** skipped when `origin` already holds the tag on the same
  commit. An origin tag on a different commit aborts rather than moving a
  published release tag.
- **MSI dispatch:** skipped when release `vX.Y.Z` already carries the
  `halocli.msi` asset (a read-only `gh release view` check). A missing
  release means a fresh run and the build is dispatched exactly once.
- **Waits:** every poll loop prints timestamped (`[YYYY-MM-DDTHH:MM:SSZ]`)
  progress, so a long silent-looking wait is visibly alive.

Script wait budgets (defaults in `scripts/release.py`, not CI/harness
limits — the release workflows declare no `timeout-minutes` of their own):

| Wait | Budget | Poll interval |
|---|---|---|
| Release workflow / feed deploy | 1800s | 20s |
| Winget-releaser PR appearance | 3600s | 15s |
| MSI dispatch run | 900s | 10s outer / 20s inner |
| Feed serving the version | 10 attempts | 10s (+30s request timeout) |

Offline regression coverage lives in `tests/test_release.py`: timestamped
wait output, tag/push reuse vs. mismatch aborts, MSI skip-on-resume, and the
manifest-mismatch abort — all with mocked `git`/`gh`, no network.

## Build The Windows MSI

The MSI is a separate workflow. It packages `halocli.exe` as a PyInstaller
onefile binary — the Python interpreter, all dependencies, the vendored spec
and `halocli-*.dist-info` are bundled inside it, so **the target machine needs
no Python runtime, no venv and no `pip`**. The WiX installer drops it into
`Program Files\Midtown Technology Group\Halocli` and appends that directory to
the system PATH.

Two things worth knowing:

- **Gating is tag-vs-release, not event-derived.** Releases are created by the
  Release workflow using `GITHUB_TOKEN`, and token-created releases do not fire
  `release: published`. So the MSI workflow checks whether a release *exists*
  for the tag: if yes it attaches `halocli.msi` to the release and dispatches
  mtg-winget; if no it uploads the MSI as a workflow artifact only. Triggering
  it by hand with `workflow_dispatch` therefore behaves identically to the
  automatic path.
- **`build-msi.ps1` smoke-tests the binary it just built** (`halocli.exe
  --version` must print the expected version). Without that check a frozen exe
  that cannot resolve its own entry point still builds cleanly and ships dead:
  that is exactly what happened to every MSI from 0.5.0 to 0.8.0.

Dispatch it manually after tagging (the tag push alone will not start it):

```powershell
gh workflow run "Build MSI Release" -f version=0.8.1
gh run watch --exit-status        # then confirm the asset landed
gh release view v0.8.1 --json assets --jq ".assets[].name"
```

Building it locally requires the same tools CI uses (PyInstaller is installed
by the workflow, not by `pip install .`):

```powershell
python -m pip install . pyinstaller
dotnet tool install --global wix --version 4.*
./packaging/windows/build-msi.ps1 -Version 0.9.0
```

The version **must match `pyproject.toml`** — the smoke test enforces it, and
`copy_metadata()` bundles whatever `halocli-*.dist-info` the environment has,
so a stale install fails the build rather than shipping a mismatched binary.

**Build from an environment without dev extras.** `todo_web.py` imports
fastapi whenever it is installed, so a venv with `.[dev]` produces an exe
around 20 MB with fastapi/starlette/uvicorn/pytest frozen inside it, while
CI's clean `pip install . pyinstaller` produces 13.7 MB. Release artifacts
always come from CI for that reason.

Install the project as well as PyInstaller — the build reads
`halocli-*.dist-info` from the environment (that is what `copy_metadata`
bundles), so `halocli.exe --version` cannot pass otherwise. This mirrors what
CI runs.

`build-msi.ps1` is safe under both PowerShell 5.1 and pwsh 7: native tools
(PyInstaller, WiX) are invoked through a wrapper that keeps their stderr
logging from becoming terminating errors when you pipe `2>&1`.


## Install From A Release Tag

```powershell
pipx install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.5.0
uv tool install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.5.0
```

Use the tagged install form for demos and managed rollout scripts so everyone
gets the same bits.
