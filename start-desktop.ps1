# Storydex Desktop Launcher
# Delegates to the shared desktop dev bootstrap so .bat and .ps1 stay in sync.

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$devScript = Join-Path $PSScriptRoot "scripts\run_desktop_dev.bat"
if (-not (Test-Path $devScript)) {
    Write-Host "[Storydex Desktop] ERROR: Desktop dev script not found: $devScript" -ForegroundColor Red
    exit 1
}

& $devScript @args
exit $LASTEXITCODE
