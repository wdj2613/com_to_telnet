@echo off
rem COM-Telnet gateway: build exe files (uses the .venv virtual environment)
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1" %*
if errorlevel 1 (
    echo.
    echo [error] Build failed. See messages above.
)
pause
