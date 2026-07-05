@echo off
setlocal EnableExtensions

set "ROOT=%~dp0.."
set "BOOTSTRAP_SCRIPT=%ROOT%\scripts\bootstrap_python39.ps1"
set "PYTHON_EXE=%ROOT%\.python39\Scripts\python.exe"
set "FRONTEND_DIR=%ROOT%\apps\frontend"
set "DESKTOP_DIR=%ROOT%\apps\desktop"

echo [Storydex] Desktop dev bootstrap...
echo.

echo [Storydex] Cleaning stale dev processes (ports 5173, 18081)...
taskkill /F /IM electron.exe >nul 2>&1
powershell -NoProfile -Command "$ports = 5173, 18081; foreach ($port in $ports) { Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} } }"
powershell -NoProfile -Command "Start-Sleep -Seconds 2"

echo.
echo [Storydex] Preparing project-local Python 3.9 runtime...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%" -InstallRequirements || goto :error

if not exist "%PYTHON_EXE%" (
  echo [Storydex] ERROR: Project-local Python 3.9 was not created.
  echo [Storydex] Expected: %PYTHON_EXE%
  echo [Storydex] Try deleting the .python39 folder and re-running.
  goto :error
)

echo.
call :ensure_node_deps "%FRONTEND_DIR%" "frontend" || goto :error
call :ensure_node_deps "%DESKTOP_DIR%" "desktop" || goto :error

set "PYTHONNOUSERSITE=1"
set "PYTHONHOME="
set "STORYDEX_PYTHON=%PYTHON_EXE%"
set "STORYDEX_EMBED_PYTHON=%ROOT%\.python39"
echo.
echo [Storydex] Using backend python: %STORYDEX_PYTHON%

if /I "%~1"=="--prepare-only" (
  echo.
  echo [Storydex] Desktop dev dependencies are ready ^(--prepare-only^).
  exit /b 0
)

echo.
echo [Storydex] Building latest frontend bundle...
cd /d "%DESKTOP_DIR%" || goto :error
call npm run build:frontend || goto :error

echo.
echo [Storydex] Syncing latest desktop app assets...
call npm run sync:assets || goto :error

echo.
echo [Storydex] Launching desktop development app...
echo [Storydex] Frontend: http://127.0.0.1:5173
echo [Storydex] Backend : http://127.0.0.1:18081
echo.

cd /d "%DESKTOP_DIR%" || goto :error
call npm run dev
exit /b %errorlevel%

:error
echo.
echo [Storydex] Desktop dev startup failed.
echo [Storydex] Check the error messages above for details.
exit /b 1

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
