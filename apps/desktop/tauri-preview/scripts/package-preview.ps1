$ErrorActionPreference = "Stop"

$previewRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$baseConfigPath = Join-Path $previewRoot "tauri.conf.json"
$generatedConfigPath = Join-Path $previewRoot "tauri.generated.conf.json"

$config = Get-Content -Raw -LiteralPath $baseConfigPath | ConvertFrom-Json
$config.bundle | Add-Member -NotePropertyName externalBin -NotePropertyValue @("binaries/storydex-agentd") -Force
$generatedConfig = $config | ConvertTo-Json -Depth 20
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($generatedConfigPath, $generatedConfig, $utf8WithoutBom)

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust cargo is required to package the Tauri preview."
}

Write-Host "[Storydex] Packaging isolated Tauri preview..."
Push-Location $previewRoot
try {
    & cargo tauri build --config $generatedConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri preview packaging failed."
    }
}
finally {
    Pop-Location
}
