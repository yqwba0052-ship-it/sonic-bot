@echo off
rem Undoes autostart_on.cmd.
schtasks /delete /tn "Sonic bot server" /f
pause
