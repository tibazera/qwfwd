# Player Ping App (Windows tray, C#) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows tray application, in a new folder `player-ping-app/` at the repo root, that measures the player's real UDP ping to a list of QW servers (fetched from the backend's `/player-targets`) and asks the backend for the best route via `/player-route`, showing the result in a small window.

**Architecture:** Single-project .NET console/WinForms app. A `QwPing` class sends the native QW `getchallenge` OOB UDP packet and measures RTT per server (no external ping library — this is the QW wire protocol itself). A `BackendClient` class wraps the two HTTP calls with `System.Net.Http.HttpClient`. `TrayApp` (WinForms `NotifyIcon` + `Form`) wires a UUID persisted in `%APPDATA%`, a menu action that runs targets → ping → route → display, end to end.

**Tech Stack:** C#, .NET 8, WinForms (`System.Windows.Forms`), `System.Net.Http`, `System.Net.Sockets` (raw `UdpClient`). No NuGet dependency beyond the .NET SDK's own libraries — everything needed is in the BCL.

**Spec:** `docs/superpowers/specs/2026-09-20-player-ping-app-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-20-player-ping-backend.md` (this app calls `/player-targets` and `/player-route`, implemented there — those endpoints should exist and be runnable locally before Task 4 here).

## Global Constraints

- No new NuGet package — BCL only (spec: "protótipo, sem instalador/assinatura de código por enquanto"; minimal footprint keeps that true).
- UUID generated on first run, persisted locally (not a login), reused on every request — per spec.
- Every network call (UDP ping, HTTP) must have a short timeout and never throw an unhandled exception into the UI thread — a timeout or backend-down state shows "sem dados", never crashes the tray (spec: "Erros de rede... nunca bloqueia o tray").
- Ping is native QW UDP (`getchallenge`), not ICMP — per spec decision.
- No automatic ezQuake integration in this app (out of scope per spec) — results are shown in a plain result window only.

---

## File Structure

- **Create: `player-ping-app/PlayerPingApp.csproj`** — project file, `net8.0-windows`, `UseWindowsForms=true`, output type `WinExe`.
- **Create: `player-ping-app/QwPing.cs`** — `QwPing` static class: builds/sends the `getchallenge` OOB packet, measures RTT, one server at a time.
- **Create: `player-ping-app/BackendClient.cs`** — `BackendClient` class: `GetTargetsAsync()`, `PostRouteAsync()`.
- **Create: `player-ping-app/ClientIdentity.cs`** — `ClientIdentity` static class: `GetOrCreateUuid()`, reads/writes `%APPDATA%\qwfwd-player-ping\client-id.txt`.
- **Create: `player-ping-app/TrayApp.cs`** — `TrayApp : ApplicationContext`: `NotifyIcon`, menu item "Find best route", orchestrates the flow, shows result in a `MessageBox` (simplest possible result window for v1).
- **Create: `player-ping-app/Program.cs`** — entry point, `[STAThread] Main()`, starts `TrayApp` via `Application.Run(new TrayApp())`.
- **Create: `player-ping-app/README.md`** — build/run instructions (`dotnet build`, `dotnet run`), and a note that this is a prototype without code signing.

---

### Task 1: Project scaffold that builds and runs an empty tray icon

**Files:**
- Create: `player-ping-app/PlayerPingApp.csproj`
- Create: `player-ping-app/Program.cs`
- Create: `player-ping-app/TrayApp.cs`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `TrayApp` class (empty shell, just a `NotifyIcon` with a "Exit" menu item) — later tasks add the "Find best route" menu item and its handler to this same class.

- [ ] **Step 1: Create the project file**

Create `player-ping-app/PlayerPingApp.csproj`:

```xml
<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <OutputType>WinExe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <UseWindowsForms>true</UseWindowsForms>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <AssemblyName>PlayerPingApp</AssemblyName>
    <RootNamespace>PlayerPingApp</RootNamespace>
  </PropertyGroup>

</Project>
```

- [ ] **Step 2: Create the entry point**

Create `player-ping-app/Program.cs`:

```csharp
namespace PlayerPingApp;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new TrayApp());
    }
}
```

- [ ] **Step 3: Create the tray shell**

Create `player-ping-app/TrayApp.cs`:

```csharp
namespace PlayerPingApp;

internal sealed class TrayApp : ApplicationContext
{
    private readonly NotifyIcon _trayIcon;

    public TrayApp()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Exit", null, OnExit);

        _trayIcon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            ContextMenuStrip = menu,
            Visible = true,
            Text = "qwfwd player ping",
        };
    }

    private void OnExit(object? sender, EventArgs e)
    {
        _trayIcon.Visible = false;
        Application.Exit();
    }
}
```

- [ ] **Step 4: Build and run manually to verify**

Run: `cd player-ping-app && dotnet build`
Expected: build succeeds, no errors.

Run: `dotnet run` (from `player-ping-app/`)
Expected: an icon appears in the Windows system tray; right-click shows "Exit"; clicking it closes the app. (Manual verification — no automated UI test for a tray icon in v1, per plan's testing approach in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add player-ping-app/PlayerPingApp.csproj player-ping-app/Program.cs player-ping-app/TrayApp.cs
git commit -m "feat(player-ping-app): scaffold tray app project

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `ClientIdentity` — persisted UUID

**Files:**
- Create: `player-ping-app/ClientIdentity.cs`
- Test: manual (see Step 3) — no test framework added in v1 per spec ("sem framework de teste formal"); this class has no branching logic worth an assert-based check beyond what manual verification covers.

**Interfaces:**
- Consumes: nothing.
- Produces: `ClientIdentity.GetOrCreateUuid() -> string` — Task 5's `TrayApp` flow calls this once per "Find best route" click (or caches it in a field after first call).

- [ ] **Step 1: Write the implementation**

Create `player-ping-app/ClientIdentity.cs`:

```csharp
namespace PlayerPingApp;

internal static class ClientIdentity
{
    private static readonly string StatePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "qwfwd-player-ping",
        "client-id.txt");

    public static string GetOrCreateUuid()
    {
        try
        {
            if (File.Exists(StatePath))
            {
                var existing = File.ReadAllText(StatePath).Trim();
                if (Guid.TryParse(existing, out _))
                {
                    return existing;
                }
            }
        }
        catch (IOException)
        {
            // fall through to generate a fresh one for this run; a
            // transient read failure shouldn't block the app from working,
            // it just means this run won't persist its id if the write
            // below also fails
        }

        var fresh = Guid.NewGuid().ToString();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StatePath)!);
            File.WriteAllText(StatePath, fresh);
        }
        catch (IOException)
        {
            // best-effort persistence; the uuid still works for this run
        }
        return fresh;
    }
}
```

- [ ] **Step 2: Verify manually**

Add a temporary line in `TrayApp`'s constructor: `MessageBox.Show(ClientIdentity.GetOrCreateUuid());`, run with `dotnet run`, confirm a valid GUID shows up, confirm `%APPDATA%\qwfwd-player-ping\client-id.txt` was created with that same value, run again and confirm the same GUID is shown (persistence works). Remove the temporary `MessageBox.Show` line afterward.

- [ ] **Step 3: Commit**

```bash
git add player-ping-app/ClientIdentity.cs
git commit -m "feat(player-ping-app): add persisted client uuid

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `QwPing` — native QW UDP ping

**Files:**
- Create: `player-ping-app/QwPing.cs`

**Interfaces:**
- Consumes: nothing (uses only `System.Net.Sockets.UdpClient`).
- Produces: `QwPing.MeasureAsync(string ip, int port, int timeoutMs = 1500) -> Task<double?>` — returns RTT in milliseconds, or `null` on timeout/error. Task 5's `TrayApp` flow calls this once per target returned by `/player-targets`.

The QW out-of-band `getchallenge` request is the same 4-byte `0xFFFFFFFF` prefix used throughout the QW protocol (see `collector/protocol.py`'s `OOB` constant for the Python-side equivalent) followed by the literal string `"getchallenge"`. Any reply at all from the target (regardless of its content) confirms the round trip — we only need RTT, not the challenge value itself, so the reply is not parsed further.

- [ ] **Step 1: Write the implementation**

Create `player-ping-app/QwPing.cs`:

```csharp
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace PlayerPingApp;

internal static class QwPing
{
    private static readonly byte[] GetChallengePacket =
        new byte[] { 0xFF, 0xFF, 0xFF, 0xFF }.Concat(Encoding.ASCII.GetBytes("getchallenge")).ToArray();

    /// <summary>
    /// Sends one QW getchallenge OOB packet to ip:port and measures RTT to
    /// the first reply. Returns null on timeout or any socket error - the
    /// caller treats that target as simply unmeasured for this round,
    /// never as a reason to abort the whole scan.
    /// </summary>
    public static async Task<double?> MeasureAsync(string ip, int port, int timeoutMs = 1500)
    {
        try
        {
            using var client = new UdpClient();
            client.Client.ReceiveTimeout = timeoutMs;
            var endpoint = new IPEndPoint(IPAddress.Parse(ip), port);

            var started = DateTime.UtcNow;
            await client.SendAsync(GetChallengePacket, GetChallengePacket.Length, endpoint);

            using var cts = new CancellationTokenSource(timeoutMs);
            var receiveTask = client.ReceiveAsync();
            var completed = await Task.WhenAny(receiveTask, Task.Delay(timeoutMs, cts.Token));
            if (completed != receiveTask)
            {
                return null; // timed out
            }

            var elapsed = DateTime.UtcNow - started;
            return elapsed.TotalMilliseconds;
        }
        catch (Exception ex) when (ex is SocketException or FormatException or ObjectDisposedException)
        {
            return null;
        }
    }
}
```

- [ ] **Step 2: Verify manually against a real server**

Add a temporary call in `TrayApp` (e.g. in the constructor, or a debug menu item):

```csharp
var rtt = await QwPing.MeasureAsync("87.98.128.174", 27502); // any known-good QW server, adjust as needed
MessageBox.Show(rtt.HasValue ? $"{rtt.Value:F0} ms" : "no reply");
```

Run and confirm a plausible RTT (or "no reply" for an unreachable IP). Remove the temporary call afterward.

- [ ] **Step 3: Commit**

```bash
git add player-ping-app/QwPing.cs
git commit -m "feat(player-ping-app): add native QW UDP ping measurement

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `BackendClient` — `/player-targets` and `/player-route` HTTP calls

**Files:**
- Create: `player-ping-app/BackendClient.cs`

**Interfaces:**
- Consumes: nothing new (uses `System.Net.Http.HttpClient`, `System.Text.Json`).
- Produces:
  - `record PlayerTarget(string Ip, int Port)`
  - `record RouteResult(double TotalPingMs, int Hops, List<string> Path)`
  - `BackendClient.GetTargetsAsync(string baseUrl) -> Task<List<PlayerTarget>>` (empty list on any failure)
  - `BackendClient.PostRouteAsync(string baseUrl, string uuid, string toIpPort, List<(string ip, int port, double rttMs)> samples) -> Task<RouteResult?>` (`null` on any failure or non-200)

  Task 5's `TrayApp` flow calls both, in that order, with the target list from the first feeding the ping loop that produces the samples for the second.

- [ ] **Step 1: Write the implementation**

Create `player-ping-app/BackendClient.cs`:

```csharp
using System.Net.Http.Json;
using System.Text.Json.Serialization;

namespace PlayerPingApp;

internal sealed record PlayerTarget(
    [property: JsonPropertyName("ip")] string Ip,
    [property: JsonPropertyName("port")] int Port);

internal sealed record PlayerTargetsResponse(
    [property: JsonPropertyName("targets")] List<PlayerTarget> Targets);

internal sealed record RouteResult(
    [property: JsonPropertyName("total_ping_ms")] double TotalPingMs,
    [property: JsonPropertyName("hops")] int Hops,
    [property: JsonPropertyName("path")] List<string> Path);

internal sealed class BackendClient
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(5) };

    public async Task<List<PlayerTarget>> GetTargetsAsync(string baseUrl)
    {
        try
        {
            var response = await Http.GetFromJsonAsync<PlayerTargetsResponse>($"{baseUrl}/player-targets");
            return response?.Targets ?? new List<PlayerTarget>();
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or NotSupportedException)
        {
            return new List<PlayerTarget>();
        }
    }

    public async Task<RouteResult?> PostRouteAsync(
        string baseUrl,
        string uuid,
        string toIpPort,
        List<(string ip, int port, double rttMs)> samples)
    {
        var payload = new
        {
            uuid,
            samples = samples.Select(s => new { ip = s.ip, port = s.port, rtt_ms = s.rttMs }),
        };
        try
        {
            var response = await Http.PostAsJsonAsync($"{baseUrl}/player-route?to={toIpPort}", payload);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }
            return await response.Content.ReadFromJsonAsync<RouteResult>();
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or NotSupportedException)
        {
            return null;
        }
    }
}
```

- [ ] **Step 2: Verify manually against the backend**

With the backend running locally (`python collector/collector.py`, per the backend plan's Final Verification), add a temporary call in `TrayApp`:

```csharp
var client = new BackendClient();
var targets = await client.GetTargetsAsync("http://127.0.0.1:8730");
MessageBox.Show($"{targets.Count} targets, first: {(targets.Count > 0 ? targets[0].Ip : "none")}");
```

Run and confirm a non-empty target list (once the collector has completed at least one collection cycle). Remove the temporary call afterward.

- [ ] **Step 3: Commit**

```bash
git add player-ping-app/BackendClient.cs
git commit -m "feat(player-ping-app): add backend HTTP client for targets/route

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the full flow into the tray menu

**Files:**
- Modify: `player-ping-app/TrayApp.cs`

**Interfaces:**
- Consumes: `ClientIdentity.GetOrCreateUuid()` (Task 2), `QwPing.MeasureAsync()` (Task 3), `BackendClient.GetTargetsAsync()`/`PostRouteAsync()` (Task 4).
- Produces: nothing consumed elsewhere — this is the app's terminal orchestration.

- [ ] **Step 1: Replace `TrayApp.cs` with the full flow**

```csharp
namespace PlayerPingApp;

internal sealed class TrayApp : ApplicationContext
{
    private const string BackendBaseUrl = "http://127.0.0.1:8730"; // TODO: point at the real deployed collector before distributing beyond local testing

    private readonly NotifyIcon _trayIcon;
    private readonly BackendClient _backend = new();
    private readonly string _uuid;

    public TrayApp()
    {
        _uuid = ClientIdentity.GetOrCreateUuid();

        var menu = new ContextMenuStrip();
        menu.Items.Add("Find best route", null, OnFindBestRoute);
        menu.Items.Add("Exit", null, OnExit);

        _trayIcon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            ContextMenuStrip = menu,
            Visible = true,
            Text = "qwfwd player ping",
        };
    }

    private async void OnFindBestRoute(object? sender, EventArgs e)
    {
        var targets = await _backend.GetTargetsAsync(BackendBaseUrl);
        if (targets.Count == 0)
        {
            MessageBox.Show("sem dados (backend indisponível ou sem servidores conhecidos)", "qwfwd player ping");
            return;
        }

        var samples = new List<(string ip, int port, double rttMs)>();
        foreach (var target in targets)
        {
            var rtt = await QwPing.MeasureAsync(target.Ip, target.Port);
            if (rtt.HasValue)
            {
                samples.Add((target.Ip, target.Port, rtt.Value));
            }
        }

        if (samples.Count == 0)
        {
            MessageBox.Show("sem dados (nenhum servidor respondeu ao ping)", "qwfwd player ping");
            return;
        }

        // Destination: cheapest directly-measured sample this round - a
        // simple, honest default for v1 ("melhor rota pra onde eu já sei
        // que o ping é bom"), not a UI for picking an arbitrary target yet.
        var closest = samples.OrderBy(s => s.rttMs).First();
        var toIpPort = $"{closest.ip}:{closest.port}";

        var route = await _backend.PostRouteAsync(BackendBaseUrl, _uuid, toIpPort, samples);
        if (route is null)
        {
            MessageBox.Show("sem dados (backend não retornou rota)", "qwfwd player ping");
            return;
        }

        var pathText = string.Join(" -> ", route.Path);
        MessageBox.Show(
            $"Melhor rota pra {toIpPort}:\n{pathText}\n{route.Hops} hop(s), {route.TotalPingMs:F0} ms total",
            "qwfwd player ping");
    }

    private void OnExit(object? sender, EventArgs e)
    {
        _trayIcon.Visible = false;
        Application.Exit();
    }
}
```

- [ ] **Step 2: Run end to end manually**

With the backend running locally and at least one collection cycle completed, run `dotnet run` from `player-ping-app/`, click the tray icon, choose "Find best route". Expected: a `MessageBox` appears showing a path and total ping, or an honest "sem dados" message if the backend/targets are unavailable — never an unhandled exception or crash.

- [ ] **Step 3: Commit**

```bash
git add player-ping-app/TrayApp.cs
git commit -m "feat(player-ping-app): wire full ping-and-route flow into tray menu

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: README with build/run instructions

**Files:**
- Create: `player-ping-app/README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by code — documentation only.

- [ ] **Step 1: Write the README**

Create `player-ping-app/README.md`:

```markdown
# qwfwd player ping app (protótipo)

App de bandeja (tray) pra Windows que mede seu ping UDP real (protocolo QW
nativo, `getchallenge`) até os servidores conhecidos pelo coletor qwfwd, e
pede ao backend a melhor rota calculada com esses dados.

Protótipo: sem instalador, sem assinatura de código (o Windows Defender
pode alertar no primeiro uso — normal para um binário não assinado).

## Requisitos

- .NET 8 SDK (Windows)
- Um coletor `collector/collector.py` rodando (local ou remoto) — ver
  `docs/superpowers/plans/2026-09-20-player-ping-backend.md`

## Build e execução

```
cd player-ping-app
dotnet build
dotnet run
```

O ícone aparece na bandeja do sistema. Clique direito → "Find best route"
mede o ping até os servidores conhecidos e mostra a melhor rota numa
janela.

## Configuração

A URL do backend está fixa em `TrayApp.cs` (`BackendBaseUrl`,
`http://127.0.0.1:8730` por padrão) — ajuste antes de distribuir pra
outros testadores apontando pro coletor real.

## Escopo (v1)

- Sem integração automática com ezQuake.
- Sem autenticação (identidade é um UUID local, não uma conta).
- Rota calculada é pessoal — não alimenta a malha compartilhada.

Ver spec completo:
`docs/superpowers/specs/2026-09-20-player-ping-app-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add player-ping-app/README.md
git commit -m "docs(player-ping-app): add build/run README

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Final Verification

- [ ] `cd player-ping-app && dotnet build` succeeds with no warnings-as-errors issues.
- [ ] With the backend running locally (`python collector/collector.py`), run the app, trigger "Find best route", confirm a route shows up.
- [ ] Kill the backend process, trigger "Find best route" again, confirm the app shows "sem dados" instead of crashing.
- [ ] Confirm `%APPDATA%\qwfwd-player-ping\client-id.txt` persists the same UUID across two separate `dotnet run` invocations.
