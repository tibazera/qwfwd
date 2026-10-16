@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0InstallPrepared.ps1"
if errorlevel 1 echo INSTALLATION FAILED - copy the error above.
pause
