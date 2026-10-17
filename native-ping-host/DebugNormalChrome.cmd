@echo off
setlocal
set "LOG=%LOCALAPPDATA%\QwMeshPing\chrome-normal-debug.log"
reg query "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping" /ve /reg:32 > "%LOCALAPPDATA%\QwMeshPing\registry-normal.txt" 2>&1
reg query "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping" /ve /reg:64 >> "%LOCALAPPDATA%\QwMeshPing\registry-normal.txt" 2>&1
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --enable-logging --v=1 --log-file="%LOG%" "https://tibazera.github.io/qwfwd/?diagnostic=normal"
