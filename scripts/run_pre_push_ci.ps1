[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$encodingCheck = Join-Path $repoRoot "scripts/validate_text_encoding.cjs"
$versionCheck = Join-Path $repoRoot "scripts/validate_version_consistency.cjs"

function Invoke-CheckedStep([string]$Name, [scriptblock]$Action) {
  Write-Host "== $Name ==" -ForegroundColor Cyan
  $global:LASTEXITCODE = 0
  & $Action
  if ($LASTEXITCODE -ne 0) {
    throw "$Name failed with exit code $LASTEXITCODE"
  }
}

& git -C $repoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Not a Git worktree: $repoRoot"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Storydex basic pre-push checks require Node.js"
}
if (-not (Test-Path -LiteralPath $encodingCheck -PathType Leaf)) {
  throw "Encoding check is missing: $encodingCheck"
}
if (-not (Test-Path -LiteralPath $versionCheck -PathType Leaf)) {
  throw "Version consistency check is missing: $versionCheck"
}

if ($Force) {
  Write-Warning "The full local pre-push gate has been retired; -Force still runs only basic checks."
}

Write-Host "Running lightweight Storydex pre-push checks. Component test suites run in GitHub Actions." -ForegroundColor Cyan

Invoke-CheckedStep "Encoding policy" {
  & node $encodingCheck
}

Invoke-CheckedStep "Conflict markers" {
  $conflicts = & git -C $repoRoot grep -n -E '^(<<<<<<< .+|=======|>>>>>>> .+)$' HEAD -- . `
    ':(exclude)apps/desktop/app/**' `
    ':(exclude)apps/desktop/vendor/**' `
    ':(exclude)apps/desktop/release/**' `
    ':(exclude)releases/**'
  $searchCode = $LASTEXITCODE
  if ($searchCode -gt 1) {
    throw "Conflict-marker scan failed with exit code $searchCode"
  }
  if ($conflicts) {
    $conflicts | Write-Host
    throw "Conflict markers found"
  }
  $global:LASTEXITCODE = 0
}

Invoke-CheckedStep "Version consistency" {
  & node $versionCheck
}

Invoke-CheckedStep "Git whitespace" {
  $upstreamSha = [string](& git -C $repoRoot rev-parse --verify --quiet '@{upstream}^{commit}' 2>$null)
  $upstreamExitCode = $LASTEXITCODE
  if ($upstreamExitCode -eq 0 -and $upstreamSha.Trim() -match "^[0-9a-f]{40}$") {
    $baseSha = [string](& git -C $repoRoot merge-base HEAD $upstreamSha.Trim())
    if ($LASTEXITCODE -ne 0 -or $baseSha.Trim() -notmatch "^[0-9a-f]{40}$") {
      throw "Unable to resolve the upstream merge base"
    }
    & git -C $repoRoot diff --check $baseSha.Trim() HEAD --
  } else {
    & git -C $repoRoot show --check --format= HEAD
  }
}

Write-Host "Storydex basic pre-push checks passed." -ForegroundColor Green
