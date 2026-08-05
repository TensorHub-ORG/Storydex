[CmdletBinding()]
param(
  [string]$Version = "$((Get-Content -Raw -LiteralPath (Join-Path ($PSScriptRoot | Split-Path -Parent) "apps/desktop/package.json") | ConvertFrom-Json).version)",
  [string]$SourceDirectory = "",
  [string]$DestinationDirectory = "",
  [string]$TestSummary = "Full and Release suites passed",
  [ValidateSet("Fastest", "Optimal")]
  [string]$CompressionLevel = "Fastest"
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
$bundleTimings = [System.Collections.Generic.List[object]]::new()

function Invoke-BundleStep([string]$Name, [scriptblock]$Action) {
  Write-Host "`n== $Name ==" -ForegroundColor Cyan
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  $status = "passed"
  try {
    $global:LASTEXITCODE = 0
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
  } catch {
    $status = "failed"
    throw
  } finally {
    $timer.Stop()
    $bundleTimings.Add([pscustomobject]@{
      name = $Name
      status = $status
      durationSeconds = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
    })
    Write-Host ("[{0}] {1} in {2:n2}s" -f $status.ToUpperInvariant(), $Name, $timer.Elapsed.TotalSeconds) -ForegroundColor $(if ($status -eq "passed") { "Green" } else { "Red" })
  }
}

$setupName = "StorydexSetup-x64-$Version.exe"
$names = @($setupName, "$setupName.blockmap", "latest.yml")
Invoke-BundleStep "Copy installer and updater assets" {
  foreach ($name in $names) {
    $source = Join-Path $SourceDirectory $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing release source: $source" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationDirectory $name) -Force
  }
}

$unpacked = Join-Path $SourceDirectory "win-unpacked"
if (-not (Test-Path -LiteralPath $unpacked -PathType Container)) { throw "Missing win-unpacked: $unpacked" }
$zip = Join-Path $DestinationDirectory "Storydex-win-unpacked.zip"
Invoke-BundleStep "Create portable ZIP" {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
  $archiveCompressionLevel = [Enum]::Parse([IO.Compression.CompressionLevel], $CompressionLevel)
  [IO.Compression.ZipFile]::CreateFromDirectory(
    $unpacked,
    $zip,
    $archiveCompressionLevel,
    $false
  )
}

$notes = Join-Path $repoRoot "apps/desktop/build/release-notes-v$Version.md"
if (-not (Test-Path -LiteralPath $notes -PathType Leaf)) { throw "Missing release notes: $notes" }
Invoke-BundleStep "Copy release notes" {
  Copy-Item -LiteralPath $notes -Destination (Join-Path $DestinationDirectory "RELEASE_NOTES.md") -Force
}

Invoke-BundleStep "Generate release metadata and checksums" {
  node (Join-Path $repoRoot "scripts/generate_release_metadata.cjs") "--release-dir=$DestinationDirectory" "--version=$Version" "--test-summary=$TestSummary"
}

Invoke-BundleStep "Validate packaged assets" {
  node (Join-Path $repoRoot "apps/desktop/scripts/validate-packaged-assets.cjs") "--unpacked=$unpacked" "--release=$DestinationDirectory"
}

Invoke-BundleStep "Verify portable ZIP index" {
  $sourceFiles = @(Get-ChildItem -LiteralPath $unpacked -Recurse -File)
  $expectedEntries = @{}
  foreach ($file in $sourceFiles) {
    $relative = $file.FullName.Substring($unpacked.Length).TrimStart([char[]]"\/").Replace("\", "/")
    $expectedEntries[$relative] = $file.Length
  }

  $archive = [IO.Compression.ZipFile]::OpenRead($zip)
  try {
    $actualEntries = @{}
    foreach ($entry in $archive.Entries) {
      if ([string]::IsNullOrEmpty($entry.Name)) { continue }
      $entryName = $entry.FullName.TrimStart([char[]]"/\").Replace("\", "/")
      if ($actualEntries.ContainsKey($entryName)) { throw "Portable ZIP contains duplicate entry: $entryName" }
      $actualEntries[$entryName] = $entry.Length
    }
    if ($actualEntries.Count -ne $expectedEntries.Count) {
      throw "Portable ZIP file count mismatch: expected $($expectedEntries.Count), actual $($actualEntries.Count)"
    }
    foreach ($entryName in $expectedEntries.Keys) {
      if (-not $actualEntries.ContainsKey($entryName)) { throw "Portable ZIP is missing: $entryName" }
      if ($actualEntries[$entryName] -ne $expectedEntries[$entryName]) { throw "Portable ZIP size mismatch: $entryName" }
    }
    if (-not $actualEntries.ContainsKey("Storydex.exe")) { throw "Portable ZIP does not contain Storydex.exe" }
  } finally {
    $archive.Dispose()
  }
}

Write-Host "`n== Release bundle timing summary ==" -ForegroundColor Cyan
$bundleTimings | Format-Table -AutoSize name, status, durationSeconds
Write-Host "Release bundle ready: $DestinationDirectory"
