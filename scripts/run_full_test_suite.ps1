[CmdletBinding()]
param(
  [ValidateSet("Fast", "Full", "Release")]
  [string]$Mode = "Full"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot "apps/backend"
$frontend = Join-Path $repoRoot "apps/frontend"
$desktop = Join-Path $repoRoot "apps/desktop"
$results = Join-Path $repoRoot "test-results"
New-Item -ItemType Directory -Force -Path $results | Out-Null

function Invoke-Step([string]$Name, [scriptblock]$Action) {
  Write-Host "`n== $Name ==" -ForegroundColor Cyan
  & $Action
  if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:STORYDEX_DISABLE_NETWORK = "1"
$env:STORYDEX_TESTING = "1"

Invoke-Step "Encoding policy" { node (Join-Path $repoRoot "scripts/validate_text_encoding.cjs") }
Invoke-Step "Conflict markers" {
  $conflicts = & rg -n '^(<<<<<<< .+|=======|>>>>>>> .+)$' `
    --glob '!apps/desktop/app/python-env/**' `
    --glob '!apps/desktop/vendor/**' `
    --glob '!apps/desktop/release/**' `
    --glob '!releases/**' `
    $repoRoot
  $searchCode = $LASTEXITCODE
  if ($searchCode -gt 1) { throw "Conflict-marker scan failed with exit code $searchCode" }
  if ($conflicts) { $conflicts | Write-Host; throw "Conflict markers found" }
  $global:LASTEXITCODE = 0
}
Invoke-Step "Version consistency" { node (Join-Path $repoRoot "scripts/validate_version_consistency.cjs") $(if ($Mode -eq "Release") { "--expected=0.3.7" }) }
Invoke-Step "Python compile" { python -m compileall -q (Join-Path $backend "api") (Join-Path $backend "core") (Join-Path $backend "services") }
Invoke-Step "Backend tests and coverage" {
  Push-Location $backend
  try {
    New-Item -ItemType Directory -Force -Path "test-results" | Out-Null
    python -m pytest -q --cov=api --cov=core --cov=services --cov-branch --cov-report=term-missing --cov-report=json:test-results/coverage.json --cov-report=xml:test-results/coverage.xml --junitxml=test-results/pytest.xml
    if ($LASTEXITCODE -eq 0) { python tests/assert_coverage.py test-results/coverage.json }
  } finally { Pop-Location }
}
Invoke-Step "Frontend type check" { npm --prefix $frontend run type-check }
Invoke-Step "Frontend Vitest coverage" { npm --prefix $frontend run test:coverage }
Invoke-Step "Frontend Node regressions" { npm --prefix $frontend run test:regressions }
Invoke-Step "Frontend production build" { npm --prefix $frontend run build }
Invoke-Step "Desktop unit tests" { npm --prefix $desktop run test:unit }
Invoke-Step "Desktop release configuration" { npm --prefix $desktop run check:release }

if ($Mode -ne "Fast") {
  Invoke-Step "Desktop directory package" { npm --prefix $desktop run build:desktop }
  Invoke-Step "Packaged asset validation" { npm --prefix $desktop run check:packaged }
  Invoke-Step "Electron packaged E2E" { npm --prefix $desktop run test:e2e }
}
if ($Mode -eq "Release") {
  Invoke-Step "Windows installer" { npm --prefix $desktop run package:win }
  Invoke-Step "Installer and updater assets" { node (Join-Path $desktop "scripts/validate-packaged-assets.cjs") "--release=$(Join-Path $desktop 'release')" }
  Invoke-Step "Local release bundle" { & (Join-Path $repoRoot "scripts/prepare_release_bundle.ps1") -Version "0.3.7" }
}
Invoke-Step "Git whitespace check" { git -C $repoRoot diff --check }
Write-Host "`nStorydex $Mode test suite passed." -ForegroundColor Green
