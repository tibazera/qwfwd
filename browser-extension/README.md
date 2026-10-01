# QW Mesh UDP Ping

See the [English installation and usage guide](https://github.com/tibazera/qwfwd/blob/feat/mesh-routing/docs/INSTALL_AND_USE.md) for the Windows x64 package, setup, route testing, troubleshooting, and uninstall instructions.

The extension uses Chrome Native Messaging and is restricted to the QW Mesh website. The packaged native helper replaces the need to run PlayerPingApp for website measurements.

## Build the helper from source

With .NET SDK 8 installed, run:

```powershell
dotnet publish native-ping-host/NativePingHost.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o native-ping-host/dist
```

Then run `native-ping-host/InstallPrepared.cmd` and load this extension folder in Chrome.
