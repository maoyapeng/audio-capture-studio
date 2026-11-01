@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install "pyinstaller>=6,<7"
if errorlevel 1 goto failed
.venv\Scripts\python.exe scripts\prepare_adb.py
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name AudioCaptureStudio --add-data "vendor/adb;vendor/adb" --collect-all sounddevice --collect-all _sounddevice_data --collect-all soundfile --collect-all telnetlib3 main.py
if errorlevel 1 goto failed
echo Build complete: dist\AudioCaptureStudio\AudioCaptureStudio.exe
echo Distribute the entire dist\AudioCaptureStudio folder.
pause
exit /b 0
:failed
echo Build failed. See messages above.
pause
exit /b 1
