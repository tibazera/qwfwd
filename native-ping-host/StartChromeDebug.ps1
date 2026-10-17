$ErrorActionPreference = 'Stop'
$chrome = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
$log = Join-Path $env:LOCALAPPDATA 'QwMeshPing\chrome_debug.log'
if (Get-Process chrome -ErrorAction SilentlyContinue) {
    throw 'Close every Chrome window first, then run this script again.'
}
Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
$env:CHROME_LOG_FILE = $log
Start-Process -FilePath $chrome -ArgumentList @(
    '--enable-logging',
    '--v=1',
    'https://tibazera.github.io/qwfwd/'
)
Write-Host "Chrome diagnostic log: $log"
