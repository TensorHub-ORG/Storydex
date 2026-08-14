[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$suite = Join-Path $repoRoot "scripts/run_full_test_suite.ps1"
$scopeResolver = Join-Path $repoRoot "scripts/resolve_ci_scope.cjs"
$schemaVersion = 2

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
if (-not (Test-Path -LiteralPath $scopeResolver -PathType Leaf)) {
  throw "Storydex CI scope resolver is missing: $scopeResolver"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Storydex pre-push scope resolution requires Node.js"
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

$upstreamSha = [string](& git -C $repoRoot rev-parse --verify --quiet '@{upstream}^{commit}' 2>$null)
$upstreamExitCode = $LASTEXITCODE
$baseSha = ""
if ($upstreamExitCode -eq 0 -and $upstreamSha.Trim() -match "^[0-9a-f]{40}$") {
  $baseSha = [string](& git -C $repoRoot merge-base $headSha $upstreamSha.Trim())
  if ($LASTEXITCODE -ne 0 -or $baseSha.Trim() -notmatch "^[0-9a-f]{40}$") {
    $baseSha = ""
  } else {
    $baseSha = $baseSha.Trim()
  }
}

$changedFilesPath = [System.IO.Path]::GetTempFileName()
try {
  $forceAllScope = -not $baseSha
  $changedFiles = @()
  if (-not $forceAllScope) {
    $changedFiles = @(& git -C $repoRoot diff --name-only --no-renames $baseSha $headSha --)
    if ($LASTEXITCODE -ne 0) {
      throw "Unable to resolve changed files between $baseSha and $headSha"
    }
  }
  [System.IO.File]::WriteAllLines(
    $changedFilesPath,
    [string[]]$changedFiles,
    [System.Text.UTF8Encoding]::new($false)
  )
  $resolverArguments = @($scopeResolver, "--files-from", $changedFilesPath)
  if ($forceAllScope) {
    $resolverArguments += @("--force-all", "true")
  }
  $scopeJson = & node @resolverArguments
  if ($LASTEXITCODE -ne 0 -or -not $scopeJson) {
    throw "Storydex CI scope resolution failed"
  }
  $scope = $scopeJson | ConvertFrom-Json
} finally {
  Remove-Item -LiteralPath $changedFilesPath -Force -ErrorAction SilentlyContinue
}

$scopeNames = [System.Collections.Generic.List[string]]::new()
foreach ($component in @("backend", "frontend", "desktop", "coomi")) {
  if ([bool]$scope.$component) {
    $scopeNames.Add($component)
  }
}
if ($scopeNames.Count -eq 0) {
  $scopeNames.Add("source")
}
$baseIdentity = if ($baseSha) { $baseSha } else { "none" }
$scopeKey = $scopeNames -join ","
Write-Host "Storydex pre-push scope: $scopeKey ($($scope.reason), $($scope.changedCount) changed files)" -ForegroundColor Cyan

if (-not $Force -and (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
  try {
    $marker = Get-Content -Raw -LiteralPath $markerPath | ConvertFrom-Json
    if (
      [int]$marker.schemaVersion -eq $schemaVersion -and
      [string]$marker.headSha -eq $headSha -and
      [string]$marker.mode -eq "Fast" -and
      [string]$marker.baseSha -eq $baseIdentity -and
      [string]$marker.scope -eq $scopeKey
    ) {
      Write-Host "Storydex pre-push CI already passed for $headSha" -ForegroundColor Green
      exit 0
    }
  } catch {
    Write-Warning "Ignoring an invalid CI certification marker: $($_.Exception.Message)"
  }
}

Write-Host "Running Storydex pre-push CI for $headSha" -ForegroundColor Cyan
& $suite -Mode Fast -Scope $scopeNames.ToArray()
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
  baseSha = $baseIdentity
  scope = $scopeKey
  reason = [string]$scope.reason
  changedCount = [int]$scope.changedCount
  completedAt = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding UTF8

Write-Host "Storydex pre-push CI passed and certified $headSha" -ForegroundColor Green
