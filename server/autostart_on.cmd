@echo off
rem Starts the bot server each time you sign in to Windows (your user only, no administrator).
rem For safety the bot itself stays stopped after a restart: press "Start" in the app.
schtasks /create /tn "Sonic bot server" /tr "\"%~dp0start_server.cmd\"" /sc onlogon /rl limited /f
pause
