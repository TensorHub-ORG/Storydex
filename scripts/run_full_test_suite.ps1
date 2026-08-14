[CmdletBinding()]
param(
  [ValidateSet("Fast", "Full", "Release")]
  [string]$Mode = "Full",
  [ValidateSet("all", "source", "backend", "frontend", "desktop", "android", "coomi")]
  [string[]]$Scope = @("all")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot "apps/backend"
$frontend = Join-Path $repoRoot "apps/frontend"
$desktop = Join-Path $repoRoot "apps/desktop"
$androidFrontend = Join-Path $repoRoot "apps/android-frontend"
$results = Join-Path $repoRoot "test-results"
$bundledPython = Join-Path $repoRoot ".python39/Scripts/python.exe"
$python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { "python" }
New-Item -ItemType Directory -Force -Path $results | Out-Null
$timingsPath = Join-Path $results "pipeline-timings.json"
$stepTimings = [System.Collections.Generic.List[object]]::new()

$selectedScope = [System.Collections.Generic.HashSet[string]]::new(
  [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($component in $Scope) {
  [void]$selectedScope.Add($component)
}
$runAllComponents = $Mode -ne "Fast" -or $selectedScope.Contains("all")
$runBackend = $runAllComponents -or $selectedScope.Contains("backend")
$runFrontend = $runAllComponents -or $selectedScope.Contains("frontend")
$runDesktop = $runAllComponents -or $selectedScope.Contains("desktop")
$runAndroid = $runAllComponents -or $selectedScope.Contains("android")
$runCoomi = $runAllComponents -or $selectedScope.Contains("coomi")

function Assert-CommandAvailable([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command is unavailable: $Name"
  }
}

function Assert-ExecutableAvailable([string]$Executable) {
  if (Test-Path -LiteralPath $Executable -PathType Leaf) {
    return
  }
  Assert-CommandAvailable $Executable
}

function Assert-NpmDependencies([string]$ProjectRoot) {
  $nodeModules = Join-Path $ProjectRoot "node_modules"
  if (-not (Test-Path -LiteralPath $nodeModules -PathType Container)) {
    throw "Node dependencies are missing in $ProjectRoot. Run npm ci in that directory."
  }
  & npm --prefix $ProjectRoot ls --depth=0 --include=dev *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Node dependencies are incomplete in $ProjectRoot. Run npm ci in that directory."
  }
}

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
$headSha = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $headSha -notmatch "^[0-9a-f]{40}$") {
  throw "Unable to resolve the current Git SHA for the Coomi runtime build"
}
$env:STORYDEX_COOMI_GIT_SHA = $headSha
$coverageMode = if ($Mode -eq "Release") { "release" } else { "advisory" }

$enabledComponents = [System.Collections.Generic.List[string]]::new()
if ($runBackend) { $enabledComponents.Add("backend") }
if ($runFrontend) { $enabledComponents.Add("frontend") }
if ($runDesktop) { $enabledComponents.Add("desktop") }
if ($runAndroid) { $enabledComponents.Add("android") }
if ($runCoomi) { $enabledComponents.Add("coomi") }
$scopeLabel = if ($runAllComponents) { "all" } else { $enabledComponents -join "," }
if (-not $scopeLabel) {
  $scopeLabel = "source"
}
Write-Host "Storydex $Mode scope: $scopeLabel" -ForegroundColor Cyan

Invoke-Step "Environment preflight" {
  Assert-CommandAvailable "git"
  Assert-CommandAvailable "node"
  Assert-ExecutableAvailable $python
  if ($runBackend) {
    & $python -c "import pytest, pytest_cov"
  }
  if ($runFrontend -or $runDesktop -or $runAndroid) {
    Assert-CommandAvailable "npm"
  }
  if ($runFrontend) {
    Assert-NpmDependencies $frontend
  }
  if ($runDesktop) {
    Assert-NpmDependencies $desktop
  }
  if ($runAndroid) {
    Assert-NpmDependencies $androidFrontend
  }
  if ($runCoomi) {
    Assert-CommandAvailable "cargo"
  }
}

Invoke-Step "Encoding policy" { node (Join-Path $repoRoot "scripts/validate_text_encoding.cjs") }
Invoke-Step "CI policy regressions" {
  node --test `
    (Join-Path $repoRoot "scripts/tests/check-coverage.test.cjs") `
    (Join-Path $repoRoot "scripts/tests/resolve-ci-scope.test.cjs") `
    (Join-Path $repoRoot "scripts/tests/ci-preflight.test.cjs")
}
Invoke-Step "Feedback receiver regressions" {
  & $python -m unittest discover -s (Join-Path $repoRoot "deploy/storydex-feedback/tests") -v
}
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
if ($runCoomi) {
  Invoke-Step "Rust Coomi desktop workspace tests" { cargo test --manifest-path (Join-Path $repoRoot "apps/desktop/agent-runtime/Cargo.toml") --locked --workspace }
  Invoke-Step "Rust Coomi Android workspace tests" { cargo test --manifest-path (Join-Path $repoRoot "apps/android/agent-runtime/Cargo.toml") --locked --workspace }
  Invoke-Step "Build Storydex Coomi desktop runtime" { cargo build --manifest-path (Join-Path $repoRoot "apps/desktop/agent-runtime/Cargo.toml") --release --locked -p storydex-coomi-bridge }
  Invoke-Step "Pinned Coomi runtime" { & $python (Join-Path $repoRoot "scripts/verify_coomi_runtime.py") }
}
if ($runBackend) {
  Invoke-Step "Python compile" { & $python -m compileall -q (Join-Path $backend "api") (Join-Path $backend "core") (Join-Path $backend "services") }
  Invoke-Step "Backend app import" {
    Push-Location $backend
    try { & $python -c "import main; assert main.app.title" } finally { Pop-Location }
  }
  Invoke-Step "Backend tests and coverage" {
    Push-Location $backend
    try {
      New-Item -ItemType Directory -Force -Path "test-results" | Out-Null
      $coomiMarker = if ($runCoomi) { @() } else { @("-m", "not coomi_runtime") }
      & $python -m pytest -q @coomiMarker --cov=api --cov=core --cov=services --cov-branch --cov-fail-under=0 --cov-report=term-missing --cov-report=json:test-results/coverage.json --cov-report=xml:test-results/coverage.xml --junitxml=test-results/pytest.xml
      $testExitCode = $LASTEXITCODE
      & node (Join-Path $repoRoot "scripts/check_coverage.cjs") --component=backend --report=test-results/coverage.json --mode=$coverageMode --test-exit-code=$testExitCode
    } finally { Pop-Location }
  }
}
if ($runFrontend) {
  Invoke-Step "Frontend type check" { npm --prefix $frontend run type-check }
  Invoke-Step "Frontend Vitest coverage" { npm --prefix $frontend run test:coverage }
  Invoke-Step "Frontend coverage ratchet" {
    $frontendCoverageReport = Join-Path $frontend "test-results/coverage/coverage-summary.json"
    & node (Join-Path $repoRoot "scripts/check_coverage.cjs") --component=frontend "--report=$frontendCoverageReport" --mode=$coverageMode
  }
  Invoke-Step "Frontend production build" { npm --prefix $frontend run build:bundle }
  Invoke-Step "Frontend Node regressions" { npm --prefix $frontend run test:regressions }
}
if ($runDesktop) {
  Invoke-Step "Desktop unit tests" { npm --prefix $desktop run test:unit }
  Invoke-Step "Desktop release configuration" { npm --prefix $desktop run check:release }
}
if ($runAndroid) {
  Invoke-Step "Android frontend production build" { npm --prefix $androidFrontend run build }
  Invoke-Step "Android random mechanics regressions" { npm --prefix $androidFrontend run test:random }
}

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
