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
$bundledPython = Join-Path $repoRoot ".python39/Scripts/python.exe"
$python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { "python" }
New-Item -ItemType Directory -Force -Path $results | Out-Null
$timingsPath = Join-Path $results "pipeline-timings.json"
$stepTimings = [System.Collections.Generic.List[object]]::new()

function Invoke-Step([string]$Name, [scriptblock]$Action) {
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
    $stepTimings.Add([pscustomobject]@{
      name = $Name
      status = $status
      durationSeconds = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
      finishedAt = (Get-Date).ToUniversalTime().ToString("o")
    })
    $stepTimings | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $timingsPath -Encoding UTF8
    Write-Host ("[{0}] {1} in {2:n2}s" -f $status.ToUpperInvariant(), $Name, $timer.Elapsed.TotalSeconds) -ForegroundColor $(if ($status -eq "passed") { "Green" } else { "Red" })
  }
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:STORYDEX_DISABLE_NETWORK = "1"
$env:STORYDEX_TESTING = "1"
$coverageMode = if ($Mode -eq "Release") { "release" } else { "ci" }

Invoke-Step "Encoding policy" { node (Join-Path $repoRoot "scripts/validate_text_encoding.cjs") }
Invoke-Step "Coverage gate parser regressions" { node --test (Join-Path $repoRoot "scripts/tests/check-coverage.test.cjs") }
Invoke-Step "Conflict markers" {
  $conflicts = & git -C $repoRoot grep -n -E '^(<<<<<<< .+|=======|>>>>>>> .+)$' -- . `
    ':(exclude)apps/desktop/app/**' `
    ':(exclude)apps/desktop/vendor/**' `
    ':(exclude)apps/desktop/release/**' `
    ':(exclude)releases/**'
  $searchCode = $LASTEXITCODE
  if ($searchCode -gt 1) { throw "Conflict-marker scan failed with exit code $searchCode" }
  if ($conflicts) { $conflicts | Write-Host; throw "Conflict markers found" }
  $global:LASTEXITCODE = 0
}
$packageVersion = (Get-Content -Raw -LiteralPath (Join-Path $desktop "package.json") | ConvertFrom-Json).version
Invoke-Step "Version consistency" { node (Join-Path $repoRoot "scripts/validate_version_consistency.cjs") $(if ($Mode -eq "Release") { "--expected=$packageVersion" }) }
Invoke-Step "Rust Coomi workspace tests" { cargo test --manifest-path (Join-Path $repoRoot "vendor/coomi-rs/Cargo.toml") --locked --workspace }
Invoke-Step "Build Storydex Coomi runtime" { cargo build --manifest-path (Join-Path $repoRoot "vendor/coomi-rs/Cargo.toml") --release --locked -p storydex-coomi-bridge }
Invoke-Step "Pinned Coomi runtime" { & $python (Join-Path $repoRoot "scripts/verify_coomi_runtime.py") }
Invoke-Step "Python compile" { & $python -m compileall -q (Join-Path $backend "api") (Join-Path $backend "core") (Join-Path $backend "services") }
Invoke-Step "Backend tests and coverage" {
  Push-Location $backend
  try {
    New-Item -ItemType Directory -Force -Path "test-results" | Out-Null
    & $python -m pytest -q --cov=api --cov=core --cov=services --cov-branch --cov-fail-under=0 --cov-report=term-missing --cov-report=json:test-results/coverage.json --cov-report=xml:test-results/coverage.xml --junitxml=test-results/pytest.xml
    $testExitCode = $LASTEXITCODE
    & node (Join-Path $repoRoot "scripts/check_coverage.cjs") --component=backend --report=test-results/coverage.json --mode=$coverageMode --test-exit-code=$testExitCode
  } finally { Pop-Location }
}
Invoke-Step "Frontend type check" { npm --prefix $frontend run type-check }
Invoke-Step "Frontend Vitest coverage" { npm --prefix $frontend run test:coverage }
Invoke-Step "Frontend coverage ratchet" {
  $frontendCoverageReport = Join-Path $frontend "test-results/coverage/coverage-summary.json"
  & node (Join-Path $repoRoot "scripts/check_coverage.cjs") --component=frontend "--report=$frontendCoverageReport" --mode=$coverageMode
}
Invoke-Step "Frontend production build" { npm --prefix $frontend run build:bundle }
Invoke-Step "Frontend Node regressions" { npm --prefix $frontend run test:regressions }
Invoke-Step "Desktop unit tests" { npm --prefix $desktop run test:unit }
Invoke-Step "Desktop release configuration" { npm --prefix $desktop run check:release }

if ($Mode -eq "Full") {
  Invoke-Step "Prepare desktop package assets" { npm --prefix $desktop run prepare:package:assets }
  Invoke-Step "Desktop directory package" { npm --prefix $desktop run build:desktop:prepared }
  Invoke-Step "Packaged asset validation" { npm --prefix $desktop run check:packaged }
  Invoke-Step "Electron packaged smoke" { npm --prefix $desktop run test:smoke }
}
if ($Mode -eq "Release") {
  Invoke-Step "Prepare desktop package assets" { npm --prefix $desktop run prepare:package:assets }
  Invoke-Step "Windows installer" { npm --prefix $desktop run package:win:prepared }
  Invoke-Step "Installer and updater assets" { node (Join-Path $desktop "scripts/validate-packaged-assets.cjs") "--release=$(Join-Path $desktop 'release')" }
  Invoke-Step "Electron packaged E2E" { npm --prefix $desktop run test:e2e }
  Invoke-Step "Local release bundle" { & (Join-Path $repoRoot "scripts/prepare_release_bundle.ps1") -Version $packageVersion }
}
Invoke-Step "Git whitespace check" { git -C $repoRoot diff --check }
Write-Host "`n== Pipeline timing summary ==" -ForegroundColor Cyan
$stepTimings | Format-Table -AutoSize name, status, durationSeconds
Write-Host "Timing details: $timingsPath"
Write-Host "`nStorydex $Mode test suite passed." -ForegroundColor Green
