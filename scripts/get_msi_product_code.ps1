param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

$resolvedPath = Resolve-Path $Path
$installer = New-Object -ComObject WindowsInstaller.Installer
try {
    $database = $installer.GetType().InvokeMember(
        "OpenDatabase",
        "InvokeMethod",
        $null,
        $installer,
        @($resolvedPath.Path, 0)
    )
} catch {
    throw "Failed to open MSI database at '$($resolvedPath.Path)': $($_.Exception.Message)"
}
$view = $database.OpenView("SELECT Value FROM Property WHERE Property = 'ProductCode'")
# Discard Execute()'s return value: it emits an empty string, so the script
# used to output TWO objects. The release workflow then interpolated that
# array and PowerShell joined the elements with a space, producing
# " {C664...}" -- a ProductCode with a leading space in the winget manifest.
# Emit exactly one value so the capture stays a scalar.
$null = $view.Execute()
$record = $view.Fetch()
if ($null -eq $record) {
    throw "ProductCode not found in MSI: $($resolvedPath.Path)"
}
$record.StringData(1)
