using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Threading;

namespace PlayerPingApp;

/// <summary>
/// Avalonia port of the original WinForms MainForm - same flow, cross-platform
/// (Windows/Linux/macOS via net8.0 + Avalonia.Desktop instead of
/// net8.0-windows + WinForms). Scan every known target, list by ping,
/// player picks the destination, app shows the calculated route.
/// </summary>
public sealed partial class MainWindow : Window
{
    // Same public collector behind a Cloudflare tunnel as the WinForms build.
    // NOTE: trycloudflare.com quick tunnels rotate - check the site's
    // index.html (COLLECTOR_BASE) if this stops responding.
    private const string BackendBaseUrl = "https://screens-grill-loved-opinions.trycloudflare.com";

    private readonly BackendClient _backend = new();
    private readonly LocalPingServer _localPingServer = new();
    private readonly string _uuid;

    private readonly Dictionary<string, (PlayerTarget target, double rttMs)> _samplesByKey = new();
    private readonly List<PlayerTarget> _unresponsiveTargets = new();

    public MainWindow()
    {
        InitializeComponent();

        _uuid = ClientIdentity.GetOrCreateUuid();
        _localPingServer.Start();
        RouteResultBox.Text = "UUID local: " + _uuid;

        Closing += (_, _) => _localPingServer.Stop();
    }

    private async void OnScanClicked(object? sender, RoutedEventArgs e)
    {
        FindRouteButton.IsEnabled = false;
        ServerList.IsEnabled = false;
        ServerList.ItemsSource = null;
        _samplesByKey.Clear();
        _unresponsiveTargets.Clear();
        RouteResultBox.Text = "UUID local: " + _uuid;
        ScanProgress.Value = 0;
        SetStatus("Buscando servidores conhecidos...");

        var targets = await _backend.GetTargetsAsync(BackendBaseUrl);
        if (targets.Count == 0)
        {
            SetStatus("sem dados (backend indisponível ou sem servidores conhecidos)");
            ServerList.ItemsSource = new[] { "sem dados - tente novamente em instantes." };
            FindRouteButton.IsEnabled = true;
            return;
        }

        // backend caps /player-route samples at 500 (collector/collector.py
        // do_POST, PLAYER_ROUTE_MAX_SAMPLES) - mirror it here.
        var scanTargets = targets.Take(500).ToList();

        // Pinged in batches, not fully unbounded parallel - ponytail: fixed
        // batch size, tune if 20 proves too slow/fast in practice.
        const int batchSize = 20;
        var completed = 0;
        for (var offset = 0; offset < scanTargets.Count; offset += batchSize)
        {
            var batch = scanTargets.Skip(offset).Take(batchSize).ToList();
            SetStatus($"Medindo ping... {completed}/{scanTargets.Count}");

            var batchResults = await Task.WhenAll(batch.Select(async target =>
            {
                var rtt = await QwPing.MeasureAsync(target.Ip, target.Port);
                return (target, rtt);
            }));

            foreach (var (target, rtt) in batchResults)
            {
                completed++;
                if (rtt.HasValue)
                {
                    _samplesByKey[$"{target.Ip}:{target.Port}"] = (target, rtt.Value);
                }
                else
                {
                    _unresponsiveTargets.Add(target);
                }
            }

            ScanProgress.Value = Math.Min(100, completed / (double)scanTargets.Count * 100);
        }

        if (_samplesByKey.Count == 0)
        {
            SetStatus("sem dados (nenhum servidor respondeu ao ping)");
            ServerList.ItemsSource = new[] { "nenhum servidor respondeu - verifique sua conexão." };
            FindRouteButton.IsEnabled = true;
            return;
        }

        var rows = new List<string>();
        foreach (var (_, (target, rttMs)) in _samplesByKey.OrderBy(kv => kv.Value.rttMs))
        {
            rows.Add($"{rttMs,6:F0} ms   {target.DisplayName}");
        }
        // Unresponsive targets at the end, so a server known to the mesh
        // doesn't just silently vanish - just no longer greyed via a
        // clickability flag, they're simply not clickable (no sample to
        // route to) since ServerList_SelectionChanged bails past this count.
        foreach (var target in _unresponsiveTargets.OrderBy(t => t.DisplayName))
        {
            rows.Add($"  sem resposta   {target.DisplayName}");
        }
        ServerList.ItemsSource = rows;
        ServerList.IsEnabled = true;
        SetStatus(
            $"Concluído — {_samplesByKey.Count}/{scanTargets.Count} responderam " +
            $"({_unresponsiveTargets.Count} sem resposta). Clique num servidor pra ver a rota.");
        FindRouteButton.IsEnabled = true;
    }

    private async void ServerList_SelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        var index = ServerList.SelectedIndex;
        // Rows are inserted in the same order as this OrderBy, so index
        // maps back to it; rows past _samplesByKey.Count are the
        // unresponsive tail and have no sample to route to.
        var ordered = _samplesByKey.OrderBy(kv => kv.Value.rttMs).ToList();
        if (index < 0 || index >= ordered.Count) return;

        var (_, (target, _)) = ordered[index];
        var toIpPort = $"{target.Ip}:{target.Port}";
        RouteResultBox.Text = $"Calculando rota até {target.DisplayName}...";

        var samples = _samplesByKey.Values.Select(v => (v.target.Ip, v.target.Port, v.rttMs)).ToList();
        var route = await _backend.PostRouteAsync(BackendBaseUrl, _uuid, toIpPort, samples);
        if (route is null)
        {
            RouteResultBox.Text = $"sem dados (backend não retornou rota até {target.DisplayName})";
            return;
        }

        var pathText = string.Join("  ->  ", route.Path);
        RouteResultBox.Text =
            $"MELHOR ROTA ATÉ {target.DisplayName}" + Environment.NewLine +
            $"({toIpPort})" + Environment.NewLine +
            Environment.NewLine +
            pathText + Environment.NewLine +
            Environment.NewLine +
            $"{route.Hops} hop(s)  •  {route.TotalPingMs:F0} ms total";
    }

    private void SetStatus(string text) => Dispatcher.UIThread.Post(() => StatusLabel.Text = text);
}
