$ErrorActionPreference = 'Stop'
foreach ($registryPath in @(
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\WOW6432Node\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\Chromium\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\WOW6432Node\Chromium\NativeMessagingHosts\com.qwfwd.ping'
)) {
    if (Test-Path -LiteralPath $registryPath) { Remove-Item -LiteralPath $registryPath }
}
$installDir = Join-Path $env:LOCALAPPDATA 'QwMeshPing'
foreach ($name in @('QwMeshPing.exe', 'com.qwfwd.ping.json')) {
    $file = Join-Path $installDir $name
    if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
}
Write-Host 'Helper removed. Remove the extension through chrome://extensions.'
