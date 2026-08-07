[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$suite = Join-Path $repoRoot "scripts/run_full_test_suite.ps1"
$schemaVersion = 1

function Invoke-GitQuietCheck([string[]]$Arguments, [string]$DirtyMessage) {
  & git -C $repoRoot @Arguments
  $exitCode = $LASTEXITCODE
  if ($exitCode -eq 1) {
    throw $DirtyMessage
  }
  if ($exitCode -ne 0) {
    throw "git $($Arguments -join ' ') failed with exit code $exitCode"
  }
}

& git -C $repoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Not a Git worktree: $repoRoot"
}
if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) {
  throw "Storydex CI suite is missing: $suite"
}

Invoke-GitQuietCheck -Arguments @("diff", "--quiet", "--ignore-submodules", "--") `
  -DirtyMessage "Tracked working-tree changes must be committed or reverted before CI certification."
Invoke-GitQuietCheck -Arguments @("diff", "--cached", "--quiet", "--ignore-submodules", "--") `
  -DirtyMessage "Staged changes must be committed before CI certification."

$headSha = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $headSha -notmatch "^[0-9a-f]{40}$") {
  throw "Unable to resolve the current commit SHA"
}
$markerPath = (& git -C $repoRoot rev-parse --path-format=absolute --git-path storydex-ci-preflight.json).Trim()
if ($LASTEXITCODE -ne 0 -or -not $markerPath) {
  throw "Unable to resolve the CI certification marker path"
}

if (-not $Force -and (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
  try {
    $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
    if (
      [int]$marker.schemaVersion -eq $schemaVersion -and
      [string]$marker.headSha -eq $headSha -and
      [string]$marker.mode -eq "Fast"
    ) {
      Write-Host "Storydex pre-push CI already passed for $headSha" -ForegroundColor Green
      exit 0
    }
  } catch {
    Write-Warning "Ignoring an invalid CI certification marker: $($_.Exception.Message)"
  }
}

Write-Host "Running Storydex pre-push CI for $headSha" -ForegroundColor Cyan
& $suite -Mode Fast
if ($LASTEXITCODE -ne 0) {
  throw "Storydex Fast test suite failed with exit code $LASTEXITCODE"
}

Invoke-GitQuietCheck -Arguments @("diff", "--quiet", "--ignore-submodules", "--") `
  -DirtyMessage "The CI suite modified tracked files; inspect and commit the resulting changes."
Invoke-GitQuietCheck -Arguments @("diff", "--cached", "--quiet", "--ignore-submodules", "--") `
  -DirtyMessage "The CI suite left staged changes; inspect them before pushing."

$markerDirectory = Split-Path -Parent $markerPath
New-Item -ItemType Directory -Force -Path $markerDirectory | Out-Null
[pscustomobject]@{
  schemaVersion = $schemaVersion
  headSha = $headSha
  mode = "Fast"
  completedAt = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8

Write-Host "Storydex pre-push CI passed and certified $headSha" -ForegroundColor Green
