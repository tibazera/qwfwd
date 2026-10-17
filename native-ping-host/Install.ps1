$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$extensionId = (Get-Content -LiteralPath (Join-Path $projectRoot 'browser-extension/extension-id.txt') -Raw).Trim()
if ($extensionId -notmatch '^[a-p]{32}$') { throw 'Invalid extension ID' }
$publishDir = Join-Path $PSScriptRoot 'dist'
dotnet publish (Join-Path $PSScriptRoot 'NativePingHost.csproj') -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o $publishDir
if ($LASTEXITCODE -ne 0) { throw 'Publish failed' }
$installDir = Join-Path $env:LOCALAPPDATA 'QwMeshPing'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -LiteralPath (Join-Path $publishDir 'QwMeshPing.exe') -Destination $installDir -Force
$manifestPath = Join-Path $installDir 'com.qwfwd.ping.json'
$manifest = @{ name='com.qwfwd.ping'; description='QW Mesh UDP measurements'; path=(Join-Path $installDir 'QwMeshPing.exe'); type='stdio'; allowed_origins=@("chrome-extension://$extensionId/") }
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
foreach ($registryPath in @(
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\WOW6432Node\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\Chromium\NativeMessagingHosts\com.qwfwd.ping',
    'HKCU:\Software\WOW6432Node\Chromium\NativeMessagingHosts\com.qwfwd.ping'
)) {
    New-Item -Path $registryPath -Force | Out-Null
    Set-Item -Path $registryPath -Value $manifestPath
}
Write-Host "Installed helper: $installDir"
Write-Host "Load unpacked extension in chrome://extensions from: $(Join-Path $projectRoot 'browser-extension')"
if (Get-Process chrome -ErrorAction SilentlyContinue) {
    Write-Warning 'Chrome is running. Close every Chrome window and reopen it before testing Native Messaging.'
}
