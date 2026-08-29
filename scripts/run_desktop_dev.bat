@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0.."
set "FRONTEND_DIR=%ROOT%\apps\frontend"
set "DESKTOP_DIR=%ROOT%\apps\desktop"
set "RUNTIME_MANIFEST=%DESKTOP_DIR%\agent-runtime\Cargo.toml"
set "RUNTIME_AGENTD_EXE=%DESKTOP_DIR%\agent-runtime\target\debug\storydex-agentd.exe"
set "RUNTIME_BRIDGE_EXE=%DESKTOP_DIR%\agent-runtime\target\debug\storydex-coomi-bridge.exe"

echo [Storydex] Desktop dev bootstrap...
echo.

echo [Storydex] Cleaning stale frontend dev process (port 5173)...
powershell -NoProfile -Command "$connection = Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue; $connection | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} }" >nul 2>&1
powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul 2>&1

echo [Storydex] Cleaning stale Rust runtime processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$targets = @($env:RUNTIME_AGENTD_EXE, $env:RUNTIME_BRIDGE_EXE) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).ToLowerInvariant() }; for ($pass = 0; $pass -lt 3; $pass++) { $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop); $orphans = @(); foreach ($process in $processes) { if ([string]::IsNullOrWhiteSpace($process.ExecutablePath)) { continue }; $path = [IO.Path]::GetFullPath($process.ExecutablePath).ToLowerInvariant(); if ($targets -notcontains $path) { continue }; $parent = $processes | Where-Object { $_.ProcessId -eq $process.ParentProcessId } | Select-Object -First 1; if ($null -eq $parent) { $orphans += $process } }; if ($orphans.Count -eq 0) { break }; foreach ($process in $orphans) { Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop; Write-Output ('[Storydex] Stopped orphan Rust runtime process PID {0}: {1}' -f $process.ProcessId, $process.ExecutablePath) }; Start-Sleep -Milliseconds 200 }; $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop); $blocked = @(); foreach ($process in $processes) { if ([string]::IsNullOrWhiteSpace($process.ExecutablePath)) { continue }; $path = [IO.Path]::GetFullPath($process.ExecutablePath).ToLowerInvariant(); if ($targets -notcontains $path) { continue }; $parent = $processes | Where-Object { $_.ProcessId -eq $process.ParentProcessId } | Select-Object -First 1; if ($null -ne $parent) { $blocked += $process } }; if ($blocked.Count -gt 0) { $blocked | ForEach-Object { Write-Output ('[Storydex] ERROR: Rust runtime process is still running (PID {0}, parent PID {1}): {2}' -f $_.ProcessId, $_.ParentProcessId, $_.ExecutablePath) }; exit 2 }; Start-Sleep -Milliseconds 500" || goto :error

echo [Storydex] Checking Rust toolchain...
where cargo >nul 2>&1
if errorlevel 1 (
  echo [Storydex] ERROR: cargo was not found on PATH.
  echo [Storydex] Install the Rust toolchain and restart this launcher.
  goto :error
)

echo.
call :ensure_node_deps "%FRONTEND_DIR%" "frontend" || goto :error
call :ensure_node_deps "%DESKTOP_DIR%" "desktop" || goto :error

if not exist "%RUNTIME_MANIFEST%" (
  echo [Storydex] ERROR: Rust sidecar manifest not found: %RUNTIME_MANIFEST%
  goto :error
)

echo.
echo [Storydex] Building Storydex Rust runtime...
cd /d "%DESKTOP_DIR%" || goto :error
call cargo build --manifest-path "%RUNTIME_MANIFEST%" --locked -p storydex-agentd -p storydex-coomi-bridge || goto :error
if not exist "%RUNTIME_AGENTD_EXE%" (
  echo [Storydex] ERROR: Rust runtime build did not create:
  echo [Storydex] %RUNTIME_AGENTD_EXE%
  goto :error
)
if not exist "%RUNTIME_BRIDGE_EXE%" (
  echo [Storydex] ERROR: Rust runtime build did not create:
  echo [Storydex] %RUNTIME_BRIDGE_EXE%
  goto :error
)

if /I "%~1"=="--prepare-only" (
  echo.
  echo [Storydex] Desktop dev dependencies and Rust runtime are ready ^(--prepare-only^).
  exit /b 0
)

echo.
echo [Storydex] Launching Tauri desktop development app...
echo [Storydex] Frontend: http://127.0.0.1:5173
echo [Storydex] Backend : dynamically assigned by storydex-agentd
echo.

call npm run dev
if errorlevel 1 goto :error
exit /b 0

:error
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" set "EXIT_CODE=1"
echo.
echo [Storydex] Desktop dev startup failed.
echo [Storydex] Check the error messages above for details.
exit /b %EXIT_CODE%

:ensure_node_deps
set "NPM_DIR=%~1"
set "NPM_LABEL=%~2"
set "NPM_STAMP=%NPM_DIR%\node_modules\.storydex-deps.sha256"

if not exist "%NPM_DIR%\package.json" (
  echo [Storydex] ERROR: Missing %NPM_LABEL% package.json: %NPM_DIR%\package.json
  exit /b 1
)

if exist "%NPM_DIR%\node_modules" (
  if exist "%NPM_STAMP%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$dir=$env:NPM_DIR; $stamp=$env:NPM_STAMP; $files=@('package.json','package-lock.json') | ForEach-Object { Join-Path $dir $_ } | Where-Object { Test-Path $_ }; $current=($files | Sort-Object | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join ':'; $saved=(Get-Content -Raw -LiteralPath $stamp).Trim(); if ($saved -eq $current) { exit 0 } exit 2"
    if not errorlevel 1 (
      echo [Storydex] %NPM_LABEL% npm dependencies unchanged; skipping npm install.
      exit /b 0
    )
  ) else (
    cd /d "%NPM_DIR%" || exit /b 1
    call npm ls --depth=0 --silent >nul 2>nul
    if not errorlevel 1 (
      call :write_node_deps_stamp "%NPM_DIR%" || exit /b 1
      echo [Storydex] %NPM_LABEL% npm dependencies already installed; skipping npm install.
      exit /b 0
    )
  )
)

echo [Storydex] Installing %NPM_LABEL% npm dependencies...
cd /d "%NPM_DIR%" || exit /b 1
call npm install --prefer-offline --no-audit --fund=false || (
  echo [Storydex] ERROR: npm install failed for %NPM_LABEL%.
  exit /b 1
)
call :write_node_deps_stamp "%NPM_DIR%" || exit /b 1
exit /b 0

:write_node_deps_stamp
set "NPM_DIR=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$dir=$env:NPM_DIR; $stamp=Join-Path (Join-Path $dir 'node_modules') '.storydex-deps.sha256'; $files=@('package.json','package-lock.json') | ForEach-Object { Join-Path $dir $_ } | Where-Object { Test-Path $_ }; $current=($files | Sort-Object | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join ':'; New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stamp) | Out-Null; Set-Content -LiteralPath $stamp -Value $current -Encoding ASCII"
exit /b %errorlevel%
