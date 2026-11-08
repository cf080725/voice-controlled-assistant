@echo off
rem Start the assistant (tray). Keep this file ASCII-only: cmd misparses UTF-8 text in LF batch files.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Not installed yet. Please double-click the install script first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0canaan.py"
