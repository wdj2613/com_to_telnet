@echo off
rem COM-Telnet gateway: build the exe (uses the .venv virtual environment)
rem   build_exe.bat        -> dist\COM_To_Telnet.exe + 说明.txt (GUI only, double-click and go)
rem   build_exe.bat -All   -> also build the CLI exe and the two onedir portable folders
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1" %*
if errorlevel 1 (
    echo.
    echo [error] Build failed. See messages above.
)
pause
