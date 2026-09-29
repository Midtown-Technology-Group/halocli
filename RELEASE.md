# Release Checklist

This project ships installable console-tool releases from GitHub tags. PyPI can
be added later with trusted publishing, but the minimum release path does not
require local package-upload tokens.

## Local Preflight

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pip install --upgrade pip setuptools wheel
python -m pytest -q
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
python -m build
python -m twine check dist/*.tar.gz dist/*.whl
pip-audit --progress-spinner off --skip-editable .
cyclonedx-py environment --output-format JSON --output-file dist/halocli-sbom.cdx.json
```

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
python -m pip install pyinstaller
dotnet tool install --global wix --version 4.*
./packaging/windows/build-msi.ps1 -Version 0.8.1
```

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
