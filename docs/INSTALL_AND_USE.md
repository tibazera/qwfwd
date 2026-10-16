# QW Mesh — installation and usage

These test packages support **Google Chrome on Windows x64 and Linux x64**. It includes the Chrome extension and a small native helper that measures real QuakeWorld UDP round trips from your computer. You do not need PlayerPingApp, the .NET SDK, or a separate .NET runtime.

## 1. Download

Download **[qw-mesh-windows-test.zip](https://github.com/tibazera/qwfwd/releases/download/qw-mesh-browser-v0.1.0/qw-mesh-windows-test.zip)** from the [GitHub release](https://github.com/tibazera/qwfwd/releases/tag/qw-mesh-browser-v0.1.0).

For Linux, download **[qw-mesh-linux-x64-test.tar.gz](https://github.com/tibazera/qwfwd/releases/download/qw-mesh-browser-v0.1.0/qw-mesh-linux-x64-test.tar.gz)** from the same release.

On Windows, right-click the ZIP and select **Extract All**. Extract everything into a permanent folder, for example `Documents\QW Mesh`. Do not run the installer directly inside the ZIP.

## 2. Install the UDP helper

### Windows x64

1. Open the extracted `native-ping-host` folder.
2. Double-click **InstallPrepared.cmd**.
3. Wait for **SUCCESS: helper installed and registered**. Press any key to close the window.

Installation is for the current Windows user and normally needs no administrator access. It copies the helper to `%LOCALAPPDATA%\QwMeshPing` and registers it with Chrome. Install it using the same Windows account you use for Chrome.

### Linux x64

Use a regular desktop installation of Google Chrome or Chromium on a glibc-based Linux distribution, such as Debian or Ubuntu. The installer requires Python 3. ARM, Alpine/musl, and Snap/Flatpak browser installations are not supported by this package.

```bash
tar -xzf qw-mesh-linux-x64-test.tar.gz
cd qw-mesh-linux-x64-test
sh native-ping-host/InstallLinux.sh
```

Run this as your normal desktop user, **without sudo**. Wait for **SUCCESS**. The helper is installed in `~/.local/lib/qw-mesh-ping`. The installer registers it for Chrome and Chromium and checks that the executable starts successfully. No .NET installation is needed.

For a browser launched with a custom user data directory, pass that directory:

```bash
sh native-ping-host/InstallLinux.sh /absolute/path/to/browser-user-data
```

Then follow the extension and usage steps below in your Linux browser. Keep the extracted extension folder on disk.

## 3. Load the Chrome extension

1. Open `chrome://extensions` in Google Chrome.
2. Enable **Developer mode** in the top-right corner.
3. Click **Load unpacked**.
4. Select the extracted **browser-extension** folder, which contains `manifest.json`.
5. Confirm **QW Mesh UDP Ping** is enabled. Its ID should be `fabelkeohcbikapfaoeflaabbjpobgcl`.

Keep the extension folder in the same location. This is a manual test installation; it is not a Chrome Web Store release.

## 4. Measure a route

1. Open **[QW Mesh](https://tibazera.github.io/qwfwd/)**. If the page was already open, reload it after installing.
2. Click a city on the map, then select the destination QuakeWorld server in the server list.
3. Measurements start automatically. Watch the UDP progress counter and the number of replies. Some endpoints time out, so a full scan can take several minutes.
4. The site measures your connection to the confirmed proxies and to the destination directly. It then combines your measurements with the mesh's measured connections to calculate routes.
5. Read **Recommended routes**, ordered by the displayed estimated total RTT, lowest first. Each proxy route shows the proxy chain and its measured legs.
6. Click **copy route**, open your QuakeWorld client's console, paste the command, and press Enter.

Proxy routes use `cl_proxyaddr …; connect …`. Direct routes use only `connect …`. **If your client still has an earlier proxy configured, run `cl_proxyaddr ""` before using a direct route.** Your client must support the proxy chain shown in the command.

If the browser offers location access, it is optional and only helps draw your position on the map. It does not determine the UDP ping or choose your entry proxy.

## 5. Understand the results

- **you → entry / local UDP:** RTT measured from your own computer to the entry proxy.
- **mesh → server:** the measured legs reported by the proxy network.
- **Estimated total:** the sum of these legs. It is **not an end-to-end measurement through the complete proxy chain**, and the ping displayed in the game can differ.
- **Direct:** a UDP measurement from your computer to the game server.
- **Addresses checked / confirmed proxies:** the discovery list includes game servers too. Only endpoints answering the proxy protocol are confirmed as proxies.

The local helper currently takes one RTT sample per endpoint; it does not measure your first hop's jitter or packet loss. Mesh jitter and loss are available where the proxies report them. Results describe the measured network at that time and do not guarantee the best route across every possible Internet proxy.

## Troubleshooting

### “Specified native messaging host not found”

1. Run `native-ping-host\InstallPrepared.cmd` again using your normal Windows account and check for **SUCCESS**.
2. Check the extension ID and that it is enabled in `chrome://extensions`.
3. Fully exit Chrome, reopen it, and reload the QW Mesh page. If Chrome remains running in the background, exit that instance too.
4. If it still fails, report the exact error, your Chrome version, and the installer output in a [GitHub issue](https://github.com/tibazera/qwfwd/issues).

On Linux, rerun `sh native-ping-host/InstallLinux.sh` without sudo, check for SUCCESS, and restart your browser. Use a regular desktop browser installation. If you use a custom user data directory, pass it to the installer.

### “The extension did not respond”

Enable the extension, check that you loaded the correct folder, and reload the page. The extension is restricted to `https://tibazera.github.io/qwfwd/`.

### No UDP reply

Some endpoints are offline or block queries. Check that your firewall allows `QwMeshPing.exe` (Windows) or `QwMeshPing` (Linux) to send UDP, then try another destination. A timeout is not a ping estimate.

### Your position does not appear on the map

Location is optional and only draws your position and the line to the first proxy. It does not calculate ping, select proxies, or identify your city through UDP.

1. Reload the site with **Ctrl+F5** and click **Allow location** in the header.
2. If Chrome asks for permission, allow it. If permission is already granted, Chrome will not display another prompt.
3. If the site reports **Access blocked**, open the icon beside the address bar, select **Site settings**, and set **Location** to **Allow**.
4. If the site reports **Chrome already allows location. Waiting for the system position**, Chrome has permission but the operating system has not supplied coordinates. Check the system location settings below.

The site stops waiting after approximately 16 seconds. Permission alone does not guarantee that the computer can determine its position. UDP measurements and route selection continue to work even if location is unavailable.

#### Windows: check location settings and the service

Open the location settings from PowerShell:

```powershell
Start-Process "ms-settings:privacy-location"
```

Enable **Location services** and, if shown, **Let desktop apps access your location**. The wording may differ between Windows versions.

If location remains unavailable, check the Geolocation Service:

```powershell
Get-Service lfsvc -ErrorAction SilentlyContinue |
    Select-Object Name, Status, StartType
```

If `StartType` is **Disabled**, open a separate **PowerShell as administrator** and run:

```powershell
Set-Service -Name lfsvc -StartupType Manual
Start-Service -Name lfsvc
Get-Service lfsvc | Select-Object Name, Status, StartType
Start-Process "ms-settings:privacy-location"
```

Immediately after these commands, the expected result is **Running / Manual**. Enable location in the settings page too: starting the service does not grant location consent. Restart Chrome and retry **Allow location** on the site.

Administrator access is only needed for changing the service, not for installing the QW Mesh helper. If the service is missing, a command fails, or location is managed by your organization, report the exact output; do not delete registry entries or override organization policies.

Command reference: [Microsoft Start-Service documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-service).

#### Linux

Allow location for the site in Chrome or Chromium and check the desktop environment's location/privacy settings, if available. Linux desktops differ in the location providers they expose. A working UDP helper does not guarantee that browser geolocation is available.

### Send useful test feedback

Include the destination address, the copied command, the site's estimated total, your in-game ping, and the exact error if any. Include your general region and ISP if useful; do not post your public IP or precise location.

## Update or uninstall

To update, extract the new ZIP or Linux tar.gz, run its installer, and replace the unpacked extension in Chrome with the new folder. Reload the QW Mesh page.

To uninstall, remove the extension in `chrome://extensions`, then run this in PowerShell from the extracted package folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\native-ping-host\Uninstall.ps1
```

The helper runs when Chrome requests a measurement; no separate PlayerPingApp window needs to be started.

On Linux, uninstall the helper with `sh native-ping-host/UninstallLinux.sh`. If you installed for a custom browser user data directory, pass the same directory to the uninstall script.
