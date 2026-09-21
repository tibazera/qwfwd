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

        // backend caps /player-route samples at 50 (collector/collector.py do_POST) — cap the scan itself, not just the post
        var scanTargets = targets.Take(50);

        var samples = new List<(string ip, int port, double rttMs)>();
        foreach (var target in scanTargets)
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
