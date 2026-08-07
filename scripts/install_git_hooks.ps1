[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$hook = Join-Path $repoRoot ".githooks/pre-push"
if (-not (Test-Path -LiteralPath $hook -PathType Leaf)) {
  throw "Versioned pre-push hook is missing: $hook"
}

& git -C $repoRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Not a Git worktree: $repoRoot"
}

& git -C $repoRoot config --local core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) {
  throw "Failed to configure the repository hooks path"
}

$configured = (& git -C $repoRoot config --local --get core.hooksPath).Trim()
if ($configured -ne ".githooks") {
  throw "Unexpected hooks path after installation: $configured"
}

Write-Host "Storydex Git hooks enabled for this worktree: $configured" -ForegroundColor Green
