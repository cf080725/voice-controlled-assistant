@echo off
rem One-click install. Keep this file ASCII-only: cmd misparses UTF-8 text in LF batch files.
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" %*
echo.
pause
