@echo off
cd /d "%~dp0"
echo This will import live Safka catalog and shipping into Shopy local database.
echo It will NOT delete manually added products or submit ANY Safka orders.
py safka_import.py --from-api --apply
pause
