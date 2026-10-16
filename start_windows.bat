@echo off
setlocal
cd /d "%~dp0"
echo [Shopy Store] Installing required packages...
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation failed. Check Python and internet connection.
  pause
  exit /b 1
)
echo [Shopy Store] Starting local server in a separate window...
start "Shopy Store - Server" cmd /k "py app.py"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5000/"
echo Store homepage: http://127.0.0.1:5000/
echo Login is optional for customers.
pause
