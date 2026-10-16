@echo off
cd /d "%~dp0"
echo Read-only Safka public API check. NO orders are sent.
py safka_probe.py
pause
