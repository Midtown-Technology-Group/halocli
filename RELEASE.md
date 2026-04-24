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
git tag v0.3.2
git push origin v0.3.2
```

Pushing a `v*.*.*` tag runs the release workflow, builds the wheel and source
distribution, generates a CycloneDX SBOM, runs `pip-audit`, and attaches the
artifacts to a GitHub Release.

## Install From A Release Tag

```powershell
pipx install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.3.2
uv tool install git+https://github.com/Midtown-Technology-Group/halocli.git@v0.3.2
```

Use the tagged install form for demos and managed rollout scripts so everyone
gets the same bits.
