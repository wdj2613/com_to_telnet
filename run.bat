@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo [setup] Creating virtual environment .venv ...
python -m venv .venv
if errorlevel 1 goto error

echo [setup] Installing requirements ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

:run
".venv\Scripts\python.exe" "com_telnet_bridge.py" %*
exit /b %errorlevel%

:error
echo.
echo [error] Cannot prepare environment. Install Python 3.8+ and make sure "python" is in PATH.
pause
exit /b 1
