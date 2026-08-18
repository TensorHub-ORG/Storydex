$ErrorActionPreference = "Stop"

$previewRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$repoRoot = (Resolve-Path (Join-Path $previewRoot "..\..\.." )).Path
$runtimeManifest = Join-Path $repoRoot "apps\desktop\agent-runtime\Cargo.toml"
$runtimeBinary = Join-Path $repoRoot "apps\desktop\agent-runtime\target\release\storydex-agentd.exe"
$binariesRoot = Join-Path $previewRoot "binaries"
$frontendRoot = Join-Path $repoRoot "apps\frontend"

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust cargo is required to build the Tauri preview sidecar."
}
if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
    throw "Rust rustc is required to resolve the Tauri preview sidecar target triple."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is required only for the Vue build; it is never copied into the candidate runtime assets."
}

Write-Host "[Storydex] Building isolated Rust sidecar for Tauri preview..."
& cargo build --manifest-path $runtimeManifest --release --locked -p storydex-agentd
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runtimeBinary)) {
    throw "storydex-agentd release build did not produce $runtimeBinary"
}

$targetLine = & rustc -vV | Select-String -Pattern "^host:" | Select-Object -First 1
if ($null -eq $targetLine) {
    throw "Unable to resolve the Rust host target triple."
}
$targetTriple = $targetLine.ToString() -replace "^host:\s*", ""

New-Item -ItemType Directory -Force -Path $binariesRoot | Out-Null
$sidecarPath = Join-Path $binariesRoot "storydex-agentd-$targetTriple.exe"
Copy-Item -LiteralPath $runtimeBinary -Destination $sidecarPath -Force

Write-Host "[Storydex] Building Vue assets for the Tauri preview..."
& npm --prefix $frontendRoot run build
if ($LASTEXITCODE -ne 0) {
    throw "Vue build failed; Tauri preview packaging stopped before bundling."
}

Write-Host "[Storydex] Prepared sidecar: $sidecarPath"
Write-Host "[Storydex] Prepared frontend: $(Join-Path $frontendRoot 'dist')"
