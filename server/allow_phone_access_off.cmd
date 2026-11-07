@echo off
rem Removes the firewall rule added by allow_phone_access.cmd.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
netsh advfirewall firewall delete rule name="Sonic bot 8787"
pause
