@echo off
setlocal EnableExtensions

set "ROOT=%~dp0.."
set "BOOTSTRAP_SCRIPT=%ROOT%\scripts\bootstrap_python39.ps1"
set "PYTHON_EXE=%ROOT%\.python39\Scripts\python.exe"

echo [Storydex] Desktop build bootstrap...

echo [Storydex] Preparing project-local Python 3.9 runtime...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%" -InstallRequirements || goto :error

if not exist "%PYTHON_EXE%" (
  echo [Storydex] Project-local Python 3.9 was not created: %PYTHON_EXE%
  goto :error
)

cd /d "%ROOT%\apps\frontend" || goto :error
call npm install || goto :error

cd /d "%ROOT%\apps\desktop" || goto :error
call npm install || goto :error

set "PYTHONNOUSERSITE=1"
set "STORYDEX_PYTHON=%PYTHON_EXE%"
set "STORYDEX_EMBED_PYTHON=%ROOT%\.python39"
echo [Storydex] Using backend python: %STORYDEX_PYTHON%
echo [Storydex] Building desktop app (win-unpacked)...

call npm run build:desktop || goto :error

echo [Storydex] Desktop build finished.
echo [Storydex] Output: apps\desktop\release\win-unpacked
exit /b 0

:error
echo [Storydex] Desktop build failed.
exit /b 1
