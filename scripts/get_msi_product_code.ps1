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
$view.Execute()
$record = $view.Fetch()
if ($null -eq $record) {
    throw "ProductCode not found in MSI: $($resolvedPath.Path)"
}
$record.StringData(1)
