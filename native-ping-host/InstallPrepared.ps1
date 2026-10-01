$ErrorActionPreference = 'Stop'
$binary = Join-Path $PSScriptRoot 'dist\QwMeshPing.exe'
if (!(Test-Path -LiteralPath $binary)) { throw "Missing prepared helper: $binary" }
$installDir = Join-Path $env:LOCALAPPDATA 'QwMeshPing'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -LiteralPath $binary -Destination (Join-Path $installDir 'QwMeshPing.exe') -Force
$extensionId = (Get-Content -Raw (Join-Path $PSScriptRoot '..\browser-extension\extension-id.txt')).Trim()
if ($extensionId -notmatch '^[a-p]{32}$') { throw 'Invalid extension ID' }
$manifestPath = Join-Path $installDir 'com.qwfwd.ping.json'
$manifest = @{name='com.qwfwd.ping'; description='QW Mesh UDP measurements'; path=(Join-Path $installDir 'QwMeshPing.exe'); type='stdio'; allowed_origins=@("chrome-extension://$extensionId/")}
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, $view)
    $key = $root.CreateSubKey('Software\Google\Chrome\NativeMessagingHosts\com.qwfwd.ping')
    $key.SetValue('', $manifestPath, [Microsoft.Win32.RegistryValueKind]::String)
    if ($key.GetValue('') -ne $manifestPath) { throw 'Registration verification failed' }
    $key.Dispose()
    $root.Dispose()
}
Write-Host "SUCCESS: helper installed and registered at $installDir" -ForegroundColor Green
Write-Host 'Refresh the QW Mesh site and select a server.'
