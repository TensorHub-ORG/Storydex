@echo off
setlocal EnableExtensions

set "ROOT=%~dp0.."
set "BACKEND_DIR=%ROOT%\apps\backend"
set "FRONTEND_DIST=%ROOT%\apps\frontend\dist"
set "BOOTSTRAP_SCRIPT=%ROOT%\scripts\bootstrap_python39.ps1"
set "PYTHON_EXE=%ROOT%\.python39\Scripts\python.exe"

if not exist "%FRONTEND_DIST%\index.html" (
  echo [Storydex] Missing frontend static build: apps\frontend\dist\index.html
  echo [Storydex] Please run scripts\build_frontend.bat first.
  exit /b 1
)

echo [Storydex] Preparing project-local Python 3.9 runtime...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%" -InstallRequirements || goto :error

if not exist "%PYTHON_EXE%" (
  echo [Storydex] Project-local Python 3.9 was not created: %PYTHON_EXE%
  goto :error
)

cd /d "%BACKEND_DIR%" || goto :error

set "SERVE_FRONTEND_STATIC=true"
set "FRONTEND_DIST_DIR=%FRONTEND_DIST%"
set "PYTHONNOUSERSITE=1"

echo [Storydex] Starting backend static server with uvicorn...
echo [Storydex] URL: http://127.0.0.1:18080
"%PYTHON_EXE%" -m uvicorn main:app --host 127.0.0.1 --port 18080
exit /b %errorlevel%

:error
echo [Storydex] Backend static startup failed.
exit /b 1
