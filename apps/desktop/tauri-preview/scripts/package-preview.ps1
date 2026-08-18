$ErrorActionPreference = "Stop"

$previewRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$desktopRoot = (Resolve-Path (Join-Path $previewRoot ".." )).Path
$baseConfigPath = Join-Path $previewRoot "tauri.conf.json"
$generatedConfigPath = Join-Path $previewRoot "tauri.generated.conf.json"
$prepareScriptPath = Join-Path $previewRoot "scripts\prepare-preview.ps1"
$escapedPrepareScriptPath = $prepareScriptPath.Replace("'", "''")
$prepareCommand = "& '$escapedPrepareScriptPath'"
$encodedPrepareCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($prepareCommand))

$config = Get-Content -Raw -LiteralPath $baseConfigPath | ConvertFrom-Json
$config.build.beforeBuildCommand = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encodedPrepareCommand"
$config.bundle | Add-Member -NotePropertyName externalBin -NotePropertyValue @("binaries/storydex-agentd") -Force
$generatedConfig = $config | ConvertTo-Json -Depth 20
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($generatedConfigPath, $generatedConfig, $utf8WithoutBom)

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust cargo is required to package the Tauri preview."
}
$tauriCli = $null
if (-not [string]::IsNullOrWhiteSpace($env:STORYDEX_TAURI_CLI)) {
    $tauriCli = [System.IO.Path]::GetFullPath($env:STORYDEX_TAURI_CLI)
    if (-not (Test-Path -LiteralPath $tauriCli -PathType Leaf) -or
        -not [System.IO.Path]::GetFileName($tauriCli).Equals("cargo-tauri.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "STORYDEX_TAURI_CLI must point to an existing cargo-tauri.exe: $tauriCli"
    }
}

Write-Host "[Storydex] Packaging isolated Tauri preview..."
Push-Location $previewRoot
try {
    if ($null -ne $tauriCli) {
        & $tauriCli build --config $generatedConfigPath
    }
    else {
        & cargo tauri build --config $generatedConfigPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri preview packaging failed."
    }
}
finally {
    Pop-Location
}

$releaseRoot = Join-Path $previewRoot "target\release"
$previewExecutable = Join-Path $releaseRoot "storydex-tauri-preview.exe"
$sidecarExecutable = Join-Path $releaseRoot "storydex-agentd.exe"
foreach ($requiredFile in @($previewExecutable, $sidecarExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Tauri preview packaging did not produce $requiredFile"
    }
}

$candidateRoot = (Resolve-Path (Join-Path $desktopRoot "candidate")).Path
$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $candidateRoot "staging"))
$candidatePrefix = $candidateRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $stagingRoot.StartsWith($candidatePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Candidate staging path escaped the isolated candidate directory: $stagingRoot"
}
if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stagingRoot | Out-Null
Copy-Item -LiteralPath $previewExecutable -Destination $stagingRoot
Copy-Item -LiteralPath $sidecarExecutable -Destination $stagingRoot

$previousCandidateRoot = $env:STORYDEX_RUST_CANDIDATE_ROOT
try {
    $env:STORYDEX_RUST_CANDIDATE_ROOT = $stagingRoot
    & npm --prefix $desktopRoot run check:rust-candidate
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri preview candidate asset policy failed."
    }
}
finally {
    $env:STORYDEX_RUST_CANDIDATE_ROOT = $previousCandidateRoot
}

Write-Host "[Storydex] Staged isolated Tauri preview: $stagingRoot"
