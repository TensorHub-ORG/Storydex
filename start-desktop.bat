@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "DEV_SCRIPT=%ROOT%scripts\run_desktop_dev.bat"

if not exist "%DEV_SCRIPT%" (
  echo [Storydex Desktop] ERROR: Desktop dev script not found: %DEV_SCRIPT%
  pause
  exit /b 1
)

call "%DEV_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo [Storydex Desktop] Startup failed with exit code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
