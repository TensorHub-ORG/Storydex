@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "START_SCRIPT=%ROOT%scripts\run_fullstack_dev.bat"

if not exist "%START_SCRIPT%" (
  echo [Storydex] Missing startup script: %START_SCRIPT%
  exit /b 1
)

call "%START_SCRIPT%" %*
exit /b %errorlevel%
