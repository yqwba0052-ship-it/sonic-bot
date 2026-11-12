@echo off
rem Read-only connection test of the Binance key in server\.env (no orders, no changes).
cd /d "%~dp0"
.venv\Scripts\python.exe -m sonic.check_account
pause
