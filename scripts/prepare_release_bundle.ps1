[CmdletBinding()]
param(
  [string]$Version = "0.3.3",
  [string]$SourceDirectory = "",
  [string]$DestinationDirectory = "",
  [string]$TestSummary = "Full and Release suites passed"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourceDirectory) { $SourceDirectory = Join-Path $repoRoot "apps/desktop/release" }
$SourceDirectory = [IO.Path]::GetFullPath($SourceDirectory)
if (-not $DestinationDirectory) {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $DestinationDirectory = Join-Path $repoRoot "releases/release_${stamp}_v$Version"
}
$DestinationDirectory = [IO.Path]::GetFullPath($DestinationDirectory)
New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null

$setupName = "StorydexSetup-x64-$Version.exe"
$names = @($setupName, "$setupName.blockmap", "latest.yml")
foreach ($name in $names) {
  $source = Join-Path $SourceDirectory $name
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing release source: $source" }
  Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDirectory $name) -Force
}

$unpacked = Join-Path $SourceDirectory "win-unpacked"
if (-not (Test-Path -LiteralPath $unpacked -PathType Container)) { throw "Missing win-unpacked: $unpacked" }
Compress-Archive -LiteralPath $unpacked -DestinationPath (Join-Path $DestinationDirectory "Storydex-win-unpacked.zip") -CompressionLevel Optimal -Force
$notes = Join-Path $repoRoot "apps/desktop/build/release-notes-v$Version.md"
if (-not (Test-Path -LiteralPath $notes -PathType Leaf)) { throw "Missing release notes: $notes" }
Copy-Item -LiteralPath $notes -Destination (Join-Path $DestinationDirectory "RELEASE_NOTES.md") -Force

$checksumTargets = @($setupName, "$setupName.blockmap", "Storydex-win-unpacked.zip", "latest.yml", "RELEASE_NOTES.md")
$checksumLines = foreach ($name in $checksumTargets) {
  $hash = Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $DestinationDirectory $name)
  "$($hash.Hash)  $name"
}
Set-Content -LiteralPath (Join-Path $DestinationDirectory "SHA256SUMS.txt") -Value $checksumLines -Encoding ASCII

node (Join-Path $repoRoot "scripts/generate_release_metadata.cjs") "--release-dir=$DestinationDirectory" "--version=$Version" "--test-summary=$TestSummary"
if ($LASTEXITCODE -ne 0) { throw "Release metadata generation failed" }
$checksumLines = Get-ChildItem -LiteralPath $DestinationDirectory -File | Where-Object Name -ne "SHA256SUMS.txt" | Sort-Object Name | ForEach-Object {
  $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName
  "$($hash.Hash)  $($_.Name)"
}
Set-Content -LiteralPath (Join-Path $DestinationDirectory "SHA256SUMS.txt") -Value $checksumLines -Encoding ASCII
node (Join-Path $repoRoot "apps/desktop/scripts/validate-packaged-assets.cjs") "--unpacked=$unpacked" "--release=$DestinationDirectory"
if ($LASTEXITCODE -ne 0) { throw "Release bundle validation failed" }

$zip = Join-Path $DestinationDirectory "Storydex-win-unpacked.zip"
$probe = Join-Path ([IO.Path]::GetTempPath()) ("storydex-release-probe-" + [Guid]::NewGuid().ToString("N"))
try {
  Expand-Archive -LiteralPath $zip -DestinationPath $probe -Force
  if (-not (Get-ChildItem -LiteralPath $probe -Filter Storydex.exe -Recurse -File)) { throw "Portable ZIP does not contain Storydex.exe" }
} finally {
  if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Recurse -Force }
}
Write-Host "Release bundle ready: $DestinationDirectory"
