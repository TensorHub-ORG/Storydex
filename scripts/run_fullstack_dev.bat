@echo off
setlocal EnableExtensions

set "ROOT=%~dp0.."
set "BOOTSTRAP_SCRIPT=%ROOT%\scripts\bootstrap_python39.ps1"
set "PYTHON_EXE=%ROOT%\.python39\Scripts\python.exe"

echo [Storydex] Cleaning stale Storydex dev processes...
powershell -NoProfile -Command ^
  "$ports = 18080, 18081, 5173; foreach ($port in $ports) { Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} } }"

timeout /t 1 /nobreak >nul

echo [Storydex] Preparing project-local Python 3.9 runtime...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%" -InstallRequirements || goto :error

if not exist "%PYTHON_EXE%" (
  echo [Storydex] Project-local Python 3.9 was not created: %PYTHON_EXE%
  goto :error
)

echo [Storydex] Launching backend dev window (uvicorn)...
set "PYTHONNOUSERSITE=1"
start "Storydex Backend Dev" /D "%ROOT%\apps\backend" cmd /k ""%PYTHON_EXE%" -m uvicorn main:app --host 127.0.0.1 --port 18081"

echo [Storydex] Waiting for backend health check...
powershell -NoProfile -Command ^
  "$deadline = (Get-Date).AddSeconds(45); while ((Get-Date) -lt $deadline) { try { $resp = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18081/api/v1/sys/health' -TimeoutSec 2; if ($resp.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
  echo [Storydex] Backend was not ready within 45 seconds. Frontend will still start and auto-retry.
) else (
  echo [Storydex] Backend is healthy.
)

echo [Storydex] Launching frontend dev window (npm run dev)...
start "Storydex Frontend Dev" /D "%ROOT%\apps\frontend" cmd /k "npm install && npm run dev"

echo [Storydex] Dev stack started.
echo [Storydex] Frontend: http://127.0.0.1:5173
echo [Storydex] Backend : http://127.0.0.1:18081
exit /b 0

:error
echo [Storydex] Fullstack dev startup failed.
exit /b 1
