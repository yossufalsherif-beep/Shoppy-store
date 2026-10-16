@echo off
cd /d "%~dp0"
echo Read-only Safka import validation. NO database changes or order submissions.
py safka_import.py --from-api
pause
