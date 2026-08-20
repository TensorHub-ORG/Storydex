[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$agentManifestPath = Join-Path $repoRoot "apps/desktop/agent-runtime/Cargo.toml"
$tauriManifestPath = Join-Path $repoRoot "apps/desktop/tauri-preview/Cargo.toml"
$configPath = Join-Path $repoRoot "apps/desktop/agent-runtime/deny.toml"
$expectedVersion = "cargo-deny 0.20.2"

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw "Required command is unavailable: cargo"
}

$actualVersion = (& cargo deny --version).Trim()
if ($LASTEXITCODE -ne 0) {
  throw "Unable to run cargo deny"
}
if ($actualVersion -ne $expectedVersion) {
  throw "cargo-deny version mismatch: expected '$expectedVersion', got '$actualVersion'"
}

Write-Host "Rust dependency audit tool: $actualVersion" -ForegroundColor Cyan
Write-Host "RustSec advisory source: https://github.com/RustSec/advisory-db" -ForegroundColor Cyan

& cargo deny `
  --manifest-path $agentManifestPath `
  --config $configPath `
  --locked `
  check advisories licenses sources
if ($LASTEXITCODE -ne 0) {
  throw "Rust Agent dependency audit failed with exit code $LASTEXITCODE"
}

& cargo deny `
  --manifest-path $tauriManifestPath `
  --config $configPath `
  --locked `
  --target x86_64-pc-windows-msvc `
  check advisories licenses sources
if ($LASTEXITCODE -ne 0) {
  throw "Tauri Windows dependency audit failed with exit code $LASTEXITCODE"
}

$cargoHome = if ($env:CARGO_HOME) {
  $env:CARGO_HOME
} else {
  Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cargo"
}
$databaseRoot = Join-Path $cargoHome "advisory-dbs"
$database = Get-ChildItem -LiteralPath $databaseRoot -Directory -ErrorAction SilentlyContinue |
  Where-Object {
    $remote = & git -C $_.FullName remote get-url origin 2>$null
    $LASTEXITCODE -eq 0 -and $remote -eq "https://github.com/RustSec/advisory-db"
  } |
  Select-Object -First 1

if ($database) {
  $revision = (& git -C $database.FullName rev-parse HEAD).Trim()
  if ($LASTEXITCODE -eq 0 -and $revision -match "^[0-9a-f]{40}$") {
    Write-Host "RustSec advisory revision: $revision" -ForegroundColor Cyan
  }
}
