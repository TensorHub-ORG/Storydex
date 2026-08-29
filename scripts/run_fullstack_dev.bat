@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0.."
echo [Storydex] 已切换到 Rust/Tauri 默认开发入口。
echo [Storydex] Python/FastAPI 旧全栈脚本不再作为产品启动路径。
call "%ROOT%\scripts\run_desktop_dev.bat" %*
exit /b %errorlevel%
