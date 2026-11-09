@echo off
rem Read-only check of the Binance DEMO account keys in server\.env (no orders, no changes).
cd /d "%~dp0"
.venv\Scripts\python.exe -m sonic.check_demo
pause
