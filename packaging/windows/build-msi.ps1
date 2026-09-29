param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

# $ErrorActionPreference = "Stop" deliberately: it guards the cmdlet calls
# below (Remove-Item, Test-Path failures should abort). Native tools are
# invoked through Invoke-Native so their *logging* cannot abort the build:
# PyInstaller writes INFO lines to stderr, which Windows PowerShell 5.1 turns
# into terminating errors if the caller pipes 2>&1 (CI uses pwsh 7, where that
# does not happen; the failure showed up in a local 5.1 session).
$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$distDir = Join-Path $root "dist"
$buildDir = Join-Path $root "build"

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @ArgumentList 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    # Re-emit captured lines as plain text: an ErrorRecord piped onward would
    # re-trigger Stop semantics at the caller's redirection boundary.
    foreach ($line in $output) {
        Write-Host ("{0}" -f $line)
    }
    return $code
}

if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
}
# Remove only PyInstaller/WiX outputs, not the whole dist/ directory: a local
# preflight builds the wheel, sdist and SBOM into dist/ first, and wiping it
# silently destroyed those (CI is unaffected -- a fresh checkout has no dist/).
foreach ($stale in @("halocli.exe", "halocli.msi", "halocli.wixpdb")) {
    $stalePath = Join-Path $distDir $stale
    if (Test-Path $stalePath) {
        Remove-Item -Force $stalePath
    }
}

$pyiExit = Invoke-Native -FilePath "python" -ArgumentList @(
    "-m", "PyInstaller", "--clean", "--noconfirm",
    (Join-Path $root "packaging\windows\halocli.spec")
)
$exePath = Join-Path $distDir "halocli.exe"
if ($pyiExit -ne 0 -or -not (Test-Path $exePath)) {
    throw "PyInstaller failed to produce dist\halocli.exe (exit $pyiExit)."
}

# WiX is installed in CI with: dotnet tool install --global wix --version 4.*
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw "WiX CLI was not found. Install it with: dotnet tool install --global wix --version 4.*"
}

$wixExit = Invoke-Native -FilePath "wix" -ArgumentList @(
    "build",
    (Join-Path $root "packaging\windows\halocli.wxs"),
    "-d", "Version=$Version",
    "-d", "BinDir=$distDir",
    "-o", (Join-Path $distDir "halocli.msi")
)
$msiPath = Join-Path $distDir "halocli.msi"
if ($wixExit -ne 0 -or -not (Test-Path $msiPath)) {
    throw "WiX failed to produce dist\halocli.msi (exit $wixExit)."
}

# --- Smoke test the frozen exe -------------------------------------------------
# Every MSI from 0.5.0 to 0.8.0 shipped broken: halocli_launcher.py resolves the
# console script through importlib.metadata.entry_points(), which reads
# halocli-*.dist-info, but collect_data_files() does not bundle dist-info. The
# exe therefore exited 1 with "Console script entry point not found" on every
# command. copy_metadata("halocli") in halocli.spec fixes it; this test stops it
# from regressing, because the build otherwise succeeds and ships a dead binary.
#
# Run through Invoke-Native: if the exe is broken it writes its error to
# stderr, and a bare 2>&1 under Stop would surface a NativeCommandError that
# hides the real message instead of this one.
$smokeExit = Invoke-Native -FilePath $exePath -ArgumentList @("--version")
$smokeOut = (& $exePath --version | Out-String)
if ($smokeExit -ne 0) {
    throw "Smoke test failed: halocli.exe --version exited $smokeExit (frozen entry point unresolved?)."
}
# Exact line match, not substring: a substring test for "halocli 0.8.1" would
# also accept "halocli 0.8.10" and publish an MSI whose binary version differs
# from the requested one.
$versionLine = ""
foreach ($line in ($smokeOut -split "`r?`n")) {
    if ($line.Trim() -ne "") { $versionLine = $line.Trim(); break }
}
if ($versionLine -ne "halocli $Version") {
    throw "Smoke test failed: expected first output line 'halocli $Version', got: $smokeOut"
}
Write-Host "Smoke test passed: $versionLine"

Write-Host "Built halocli.msi $Version ($([math]::Round((Get-Item $msiPath).Length / 1MB, 1)) MB)"
