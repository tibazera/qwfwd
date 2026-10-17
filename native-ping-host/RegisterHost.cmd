@echo off
setlocal
set "MANIFEST=%LOCALAPPDATA%\QwMeshPing\com.qwfwd.ping.json"
if not exist "%MANIFEST%" (
  echo QW Mesh helper manifest not found: %MANIFEST%
  pause
  exit /b 1
)
for %%K in ("Google\Chrome" "Chromium") do (
  reg add "HKCU\Software\%%~K\NativeMessagingHosts\com.qwfwd.ping" /ve /t REG_SZ /d "%MANIFEST%" /f /reg:32 >nul || goto :error
  reg add "HKCU\Software\%%~K\NativeMessagingHosts\com.qwfwd.ping" /ve /t REG_SZ /d "%MANIFEST%" /f /reg:64 >nul || goto :error
)
echo QW Mesh native host registered successfully.
echo Close every Chrome window, reopen Chrome, and retry.
pause
exit /b 0
:error
echo Failed to register QW Mesh native host.
pause
exit /b 1
