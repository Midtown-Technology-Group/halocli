param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$distDir = Join-Path $root "dist"
$buildDir = Join-Path $root "build"

if (Test-Path $distDir) {
    Remove-Item -Recurse -Force $distDir
}
if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
}

python -m PyInstaller --clean --noconfirm (Join-Path $root "packaging\windows\halocli.spec")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $distDir "halocli.exe"))) {
    throw "PyInstaller failed to produce dist\halocli.exe."
}

# WiX is installed in CI with: dotnet tool install --global wix --version 4.*
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw "WiX CLI was not found. Install it with: dotnet tool install --global wix --version 4.*"
}
wix build `
    (Join-Path $root "packaging\windows\halocli.wxs") `
    -d Version=$Version `
    -d BinDir=$distDir `
    -o (Join-Path $distDir "halocli.msi")
