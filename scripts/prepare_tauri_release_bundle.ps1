[CmdletBinding()]
param(
  [string]$Version = "$((Get-Content -Raw -LiteralPath (Join-Path ($PSScriptRoot | Split-Path -Parent) "apps/desktop/package.json") | ConvertFrom-Json).version)",
  [string]$SourceDirectory = "",
  [string]$DestinationDirectory = "",
  [string]$TestSummary = "Tauri quality gate passed"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourceDirectory) { $SourceDirectory = Join-Path $repoRoot "apps/desktop/release" }
if (-not $DestinationDirectory) { $DestinationDirectory = Join-Path $repoRoot "release-assets" }
$SourceDirectory = [IO.Path]::GetFullPath($SourceDirectory)
$DestinationDirectory = [IO.Path]::GetFullPath($DestinationDirectory)
New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null

$setupName = "StorydexSetup-x64-$Version.exe"
$names = @($setupName, "$setupName.sig", "latest.json", "Storydex-win-portable.zip")
foreach ($name in $names) {
  $source = Join-Path $SourceDirectory $name
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing Tauri release source: $source" }
  Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDirectory $name) -Force
}

$notes = Join-Path $repoRoot "apps/desktop/build/release-notes-v$Version.md"
if (-not (Test-Path -LiteralPath $notes -PathType Leaf)) { throw "Missing release notes: $notes" }
Copy-Item -LiteralPath $notes -Destination (Join-Path $DestinationDirectory "RELEASE_NOTES.md") -Force

node (Join-Path $repoRoot "scripts/generate_release_metadata.cjs") `
  "--release-dir=$DestinationDirectory" `
  "--version=$Version" `
  "--test-summary=$TestSummary"
if ($LASTEXITCODE -ne 0) { throw "Tauri release metadata generation failed." }

Write-Host "Tauri release bundle ready: $DestinationDirectory"
