@echo off
setlocal

set "ROOT=%~dp0.."

cd /d "%ROOT%\apps\frontend" || goto :error

echo [Storydex] Installing frontend dependencies...
call npm install || goto :error

echo [Storydex] Building frontend bundle...
call npm run build || goto :error

echo [Storydex] Frontend build finished: apps\frontend\dist
exit /b 0

:error
echo [Storydex] Frontend build failed.
exit /b 1


