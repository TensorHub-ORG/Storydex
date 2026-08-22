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
$publicKey = [string]$env:STORYDEX_TAURI_UPDATER_PUBKEY
if ([string]::IsNullOrWhiteSpace($publicKey) -or $publicKey -eq "__STORYDEX_TAURI_UPDATER_PUBKEY__") {
    throw "STORYDEX_TAURI_UPDATER_PUBKEY must contain the production Tauri updater public key. Keep the matching private key outside the repository."
}
$config.plugins.updater.pubkey = $publicKey.Trim()
$privateKey = [string]$env:TAURI_SIGNING_PRIVATE_KEY
${privateKeyPath} = [string]$env:TAURI_SIGNING_PRIVATE_KEY_PATH
if ([string]::IsNullOrWhiteSpace($privateKey) -and [string]::IsNullOrWhiteSpace(${privateKeyPath})) {
    throw "TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH must provide the updater private key. Production keys must remain outside the repository."
}
if (-not [string]::IsNullOrWhiteSpace(${privateKeyPath}) -and -not (Test-Path -LiteralPath ${privateKeyPath} -PathType Leaf)) {
    throw "TAURI_SIGNING_PRIVATE_KEY_PATH must point to an existing private key file: ${privateKeyPath}"
}
if ([string]::IsNullOrWhiteSpace($privateKey) -and -not [string]::IsNullOrWhiteSpace(${privateKeyPath})) {
    $privateKey = (Get-Content -Raw -LiteralPath ${privateKeyPath}).Trim()
    if ([string]::IsNullOrWhiteSpace($privateKey)) {
        throw "TAURI_SIGNING_PRIVATE_KEY_PATH points to an empty private key file: ${privateKeyPath}"
    }
    $env:TAURI_SIGNING_PRIVATE_KEY = $privateKey
}
$certificateThumbprint = [string]$env:STORYDEX_WINDOWS_CERTIFICATE_THUMBPRINT
if (-not [string]::IsNullOrWhiteSpace($certificateThumbprint)) {
    $config.bundle.windows | Add-Member -NotePropertyName certificateThumbprint -NotePropertyValue $certificateThumbprint.Trim() -Force
    $config.bundle.windows | Add-Member -NotePropertyName digestAlgorithm -NotePropertyValue "sha256" -Force
    $config.bundle.windows | Add-Member -NotePropertyName timestampUrl -NotePropertyValue "http://timestamp.digicert.com" -Force
}
$config.build.beforeBuildCommand = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encodedPrepareCommand"
$config.bundle | Add-Member -NotePropertyName externalBin -NotePropertyValue @(
    "binaries/storydex-agentd",
    "binaries/storydex-coomi-bridge"
) -Force
$config.bundle | Add-Member -NotePropertyName resources -NotePropertyValue @{ "resources/mingit" = "mingit" } -Force
$generatedConfig = $config | ConvertTo-Json -Depth 20
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($generatedConfigPath, $generatedConfig, $utf8WithoutBom)

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust cargo is required to package the Tauri preview."
}
$tauriCli = $null
if (-not [string]::IsNullOrWhiteSpace($env:STORYDEX_TAURI_CLI)) {
    $tauriCli = [System.IO.Path]::GetFullPath($env:STORYDEX_TAURI_CLI)
    if (-not (Test-Path -LiteralPath $tauriCli -PathType Leaf)) {
        throw "STORYDEX_TAURI_CLI must point to an existing Tauri CLI executable or wrapper: $tauriCli"
    }
}
elseif (-not (Test-Path -LiteralPath (Join-Path $desktopRoot "node_modules\.bin\tauri.cmd") -PathType Leaf)) {
    throw "The pinned @tauri-apps/cli dependency is missing. Run npm ci in apps/desktop before packaging."
}

Write-Host "[Storydex] Packaging isolated Tauri preview..."
Push-Location $previewRoot
try {
    if ($null -ne $tauriCli) {
        & $tauriCli build --ci --config $generatedConfigPath
    }
    else {
        & npx --no-install tauri build --ci --config $generatedConfigPath
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri preview packaging failed."
    }
}
finally {
    Pop-Location
}

$candidateRoot = (Resolve-Path (Join-Path $desktopRoot "candidate")).Path
$stagingRoot = [System.IO.Path]::GetFullPath((Join-Path $candidateRoot "staging"))
$candidatePrefix = $candidateRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $stagingRoot.StartsWith($candidatePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Candidate staging path escaped the isolated candidate directory: $stagingRoot"
}
& node (Join-Path $desktopRoot "scripts\prepare-tauri-artifacts.cjs")
if ($LASTEXITCODE -ne 0) {
    throw "Tauri artifact preparation failed."
}

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

$portableZip = Join-Path $desktopRoot "release\Storydex-win-portable.zip"
if (Test-Path -LiteralPath $portableZip) {
    Remove-Item -LiteralPath $portableZip -Force
}
Compress-Archive -Path (Join-Path $stagingRoot "*") -DestinationPath $portableZip -CompressionLevel Optimal

& node (Join-Path $desktopRoot "scripts\validate-tauri-release-assets.cjs")
if ($LASTEXITCODE -ne 0) {
    throw "Tauri release asset validation failed."
}

Write-Host "[Storydex] Staged Tauri Stable runtime: $stagingRoot"
Write-Host "[Storydex] Prepared Tauri release assets: $(Join-Path $desktopRoot 'release')"
