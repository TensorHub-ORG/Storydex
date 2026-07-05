@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "DEV_SCRIPT=%ROOT%scripts\run_desktop_dev.bat"

if not exist "%DEV_SCRIPT%" (
  echo [Storydex Desktop] ERROR: Desktop dev script not found: %DEV_SCRIPT%
  exit /b 1
)

call "%DEV_SCRIPT%" %*
exit /b %errorlevel%
