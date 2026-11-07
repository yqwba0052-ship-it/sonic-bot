@echo off
rem Lets a phone on the same PRIVATE Wi-Fi reach the bot on port 8787.
rem Windows asks for administrator approval once. Run allow_phone_access_off.cmd to undo.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
netsh advfirewall firewall add rule name="Sonic bot 8787" dir=in action=allow protocol=TCP localport=8787 profile=private
echo.
echo Done. The phone can now open http://THIS-LAPTOP-IP:8787 on the home Wi-Fi.
pause
