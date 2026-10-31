@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Run start.bat or demo.bat.
pause
exit /b 0
:failed
echo Setup failed. Install Python 3.11 or newer and check your network.
pause
exit /b 1
