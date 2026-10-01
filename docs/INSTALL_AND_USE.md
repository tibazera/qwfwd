# QW Mesh — installation and usage

This test package supports **Google Chrome on Windows x64**. It includes the Chrome extension and a small native helper that measures real QuakeWorld UDP round trips from your computer. You do not need PlayerPingApp, the .NET SDK, or a separate .NET runtime.

## 1. Download

Download **[qw-mesh-windows-test.zip](https://github.com/tibazera/qwfwd/releases/download/qw-mesh-browser-v0.1.0/qw-mesh-windows-test.zip)** from the [GitHub release](https://github.com/tibazera/qwfwd/releases/tag/qw-mesh-browser-v0.1.0).

Right-click the ZIP and select **Extract All**. Extract everything into a permanent folder, for example `Documents\QW Mesh`. Do not run the installer directly inside the ZIP.

## 2. Install the UDP helper

1. Open the extracted `native-ping-host` folder.
2. Double-click **InstallPrepared.cmd**.
3. Wait for **SUCCESS: helper installed and registered**. Press any key to close the window.

Installation is for the current Windows user and normally needs no administrator access. It copies the helper to `%LOCALAPPDATA%\QwMeshPing` and registers it with Chrome. Install it using the same Windows account you use for Chrome.

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

### “The extension did not respond”

Enable the extension, check that you loaded the correct folder, and reload the page. The extension is restricted to `https://tibazera.github.io/qwfwd/`.

### No UDP reply

Some endpoints are offline or block queries. Check that your firewall allows `QwMeshPing.exe` to send UDP, then try another destination. A timeout is not a ping estimate.

### Send useful test feedback

Include the destination address, the copied command, the site's estimated total, your in-game ping, and the exact error if any. Include your general region and ISP if useful; do not post your public IP or precise location.

## Update or uninstall

To update, extract the new ZIP, run its installer, and replace the unpacked extension in Chrome with the new folder. Reload the QW Mesh page.

To uninstall, remove the extension in `chrome://extensions`, then run this in PowerShell from the extracted package folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\native-ping-host\Uninstall.ps1
```

The helper runs when Chrome requests a measurement; no separate PlayerPingApp window needs to be started.
