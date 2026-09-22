namespace PlayerPingApp;

/// <summary>
/// Main visible window - replaces the tray-only ApplicationContext. Keeps a
/// tray icon as a convenience (close-to-tray, quick relaunch) but the app
/// now opens with a real window by default, per user request: a MessageBox
/// only after a ~1min scan made it look frozen with no feedback. Styled
/// with a dark theme and card layout (also per request) instead of default
/// WinForms gray - still plain BCL WinForms, no new dependency.
///
/// Flow: scan pings every known target, then shows a clickable list of
/// servers (by name) sorted by ping - the PLAYER picks the destination,
/// the app no longer auto-picks "whatever has the lowest ping" (that hid
/// the point of the feature: choosing WHERE to play and seeing the mesh
/// route to get there, not just finding the closest server).
/// </summary>
internal sealed class MainForm : Form
{
    // Public collector behind a Cloudflare tunnel, same one the published
    // site (gh-pages) already uses. NOTE: trycloudflare.com quick tunnels
    // are not stable long-term URLs - they can rotate if the tunnel is
    // restarted server-side. If this stops responding, check the current
    // tunnel URL the site's index.html uses (COLLECTOR_BASE) and update here.
    private const string BackendBaseUrl = "https://screens-grill-loved-opinions.trycloudflare.com";

    private static readonly Color BgDark = Color.FromArgb(18, 18, 24);
    private static readonly Color CardBg = Color.FromArgb(28, 28, 38);
    private static readonly Color ListHover = Color.FromArgb(40, 40, 52);
    private static readonly Color AccentGreen = Color.FromArgb(88, 220, 150);
    private static readonly Color TextPrimary = Color.FromArgb(235, 235, 240);
    private static readonly Color TextMuted = Color.FromArgb(150, 150, 165);
    private static readonly Font FontTitle = new("Segoe UI Semibold", 15f, FontStyle.Bold);
    private static readonly Font FontBody = new("Segoe UI", 9.5f);
    private static readonly Font FontMono = new("Consolas", 9.5f);

    private readonly NotifyIcon _trayIcon;
    private readonly BackendClient _backend = new();
    private readonly string _uuid;

    private readonly Button _findRouteButton;
    private readonly Label _statusLabel;
    private readonly ProgressBar _progressBar;
    private readonly ListBox _serverList;
    private readonly TextBox _routeResultBox;

    // Ping results kept between the scan and the click on a server, keyed
    // by "ip:port" - the ListBox only holds display strings, this map is
    // what turns a click back into an addressable sample.
    private readonly Dictionary<string, (PlayerTarget target, double rttMs)> _samplesByKey = new();

    // Targets that timed out this scan - shown greyed-out and unclickable
    // at the end of the list, so a server known to the mesh but silent
    // right now doesn't just vanish without explanation.
    private readonly List<PlayerTarget> _unresponsiveTargets = new();

    // Row -> responded-or-not, rebuilt every render of _serverList so
    // ServerList_SelectedIndexChanged can refuse clicks on unresponsive rows.
    private readonly List<bool> _rowIsClickable = new();

    public MainForm()
    {
        _uuid = ClientIdentity.GetOrCreateUuid();

        Text = "qwfwd player ping";
        Width = 640;
        Height = 560;
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(540, 420);
        BackColor = BgDark;
        ForeColor = TextPrimary;
        Font = FontBody;
        Padding = new Padding(20);

        var headerPanel = BuildHeaderPanel();
        var listPanel = BuildServerListPanel(out _serverList);
        var routePanel = BuildRouteResultPanel(out _routeResultBox);
        var footerPanel = BuildFooterPanel(out _findRouteButton, out _statusLabel, out _progressBar);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            BackColor = BgDark,
            ColumnCount = 1,
            RowCount = 4,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 55));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 45));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.Controls.Add(headerPanel, 0, 0);
        layout.Controls.Add(listPanel, 0, 1);
        layout.Controls.Add(routePanel, 0, 2);
        layout.Controls.Add(footerPanel, 0, 3);
        Controls.Add(layout);

        var menu = new ContextMenuStrip();
        menu.Items.Add("Abrir", null, (_, _) => ShowMainWindow());
        menu.Items.Add("Find best route", null, OnScanClicked);
        menu.Items.Add("Exit", null, OnExit);

        _trayIcon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            ContextMenuStrip = menu,
            Visible = true,
            Text = "qwfwd player ping",
        };
        _trayIcon.DoubleClick += (_, _) => ShowMainWindow();
    }

    private Panel BuildHeaderPanel()
    {
        var panel = new Panel { Dock = DockStyle.Fill, Height = 64, BackColor = BgDark, Padding = new Padding(0, 0, 0, 12) };

        var title = new Label
        {
            Text = "qwfwd  •  player ping",
            Font = FontTitle,
            ForeColor = TextPrimary,
            AutoSize = true,
            Location = new Point(0, 4),
        };
        var subtitle = new Label
        {
            Text = "escaneia seu ping real, escolha um servidor e veja a rota calculada até ele",
            Font = FontBody,
            ForeColor = TextMuted,
            AutoSize = true,
            Location = new Point(2, 34),
        };
        panel.Controls.Add(title);
        panel.Controls.Add(subtitle);
        return panel;
    }

    private Panel BuildServerListPanel(out ListBox serverList)
    {
        var outer = new Panel { Dock = DockStyle.Fill, BackColor = BgDark, Padding = new Padding(0, 0, 0, 8) };
        var card = new RoundedPanel
        {
            Dock = DockStyle.Fill,
            BackColor = CardBg,
            CornerRadius = 12,
            Padding = new Padding(4),
        };

        serverList = new ListBox
        {
            Dock = DockStyle.Fill,
            BorderStyle = BorderStyle.None,
            BackColor = CardBg,
            ForeColor = TextPrimary,
            Font = FontMono,
            DrawMode = DrawMode.OwnerDrawFixed,
            ItemHeight = 22,
        };
        serverList.DrawItem += ServerList_DrawItem;
        serverList.SelectedIndexChanged += ServerList_SelectedIndexChanged;
        serverList.Items.Add("Clique em \"Find best route\" para escanear os servidores conhecidos.");
        serverList.Enabled = false;

        card.Controls.Add(serverList);
        outer.Controls.Add(card);
        return outer;
    }

    private void ServerList_DrawItem(object? sender, DrawItemEventArgs e)
    {
        e.DrawBackground();
        if (e.Index < 0) return;

        var isClickable = e.Index >= _rowIsClickable.Count || _rowIsClickable[e.Index];
        var isSelected = isClickable && (e.State & DrawItemState.Selected) == DrawItemState.Selected;
        using var bg = new SolidBrush(isSelected ? ListHover : CardBg);
        e.Graphics.FillRectangle(bg, e.Bounds);

        var text = _serverList.Items[e.Index]?.ToString() ?? "";
        using var textBrush = new SolidBrush(isClickable ? TextPrimary : TextMuted);
        e.Graphics.DrawString(text, e.Font ?? FontMono, textBrush, e.Bounds.Left + 8, e.Bounds.Top + 3);
    }

    private Panel BuildRouteResultPanel(out TextBox routeResultBox)
    {
        var outer = new Panel { Dock = DockStyle.Fill, BackColor = BgDark, Padding = new Padding(0, 8, 0, 12) };
        var card = new RoundedPanel
        {
            Dock = DockStyle.Fill,
            BackColor = CardBg,
            CornerRadius = 12,
            Padding = new Padding(16),
        };

        routeResultBox = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            ScrollBars = ScrollBars.Vertical,
            BackColor = CardBg,
            ForeColor = TextPrimary,
            Font = FontMono,
            Text = "UUID local: " + _uuid,
        };
        card.Controls.Add(routeResultBox);
        outer.Controls.Add(card);
        return outer;
    }

    private Panel BuildFooterPanel(out Button findRouteButton, out Label statusLabel, out ProgressBar progressBar)
    {
        var panel = new Panel { Dock = DockStyle.Fill, Height = 72, BackColor = BgDark };

        findRouteButton = new AccentButton
        {
            Text = "Find best route",
            Location = new Point(0, 4),
            Size = new Size(170, 36),
        };
        findRouteButton.Click += OnScanClicked;

        progressBar = new ProgressBar
        {
            Location = new Point(0, 48),
            Size = new Size(panel.Width, 6),
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
            Style = ProgressBarStyle.Continuous,
            Minimum = 0,
            Maximum = 100,
            Value = 0,
        };

        statusLabel = new Label
        {
            Text = "Pronto.",
            ForeColor = TextMuted,
            Font = FontBody,
            AutoSize = false,
            Location = new Point(184, 10),
            Size = new Size(panel.Width - 184, 24),
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
        };

        panel.Controls.Add(findRouteButton);
        panel.Controls.Add(statusLabel);
        panel.Controls.Add(progressBar);
        return panel;
    }

    private void ShowMainWindow()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    private async void OnScanClicked(object? sender, EventArgs e)
    {
        _findRouteButton.Enabled = false;
        _serverList.Enabled = false;
        _serverList.Items.Clear();
        _samplesByKey.Clear();
        _unresponsiveTargets.Clear();
        _rowIsClickable.Clear();
        _routeResultBox.Text = "UUID local: " + _uuid;
        _progressBar.Value = 0;
        SetStatus("Buscando servidores conhecidos...");

        var targets = await _backend.GetTargetsAsync(BackendBaseUrl);
        if (targets.Count == 0)
        {
            SetStatus("sem dados (backend indisponível ou sem servidores conhecidos)");
            _serverList.Items.Add("sem dados - tente novamente em instantes.");
            _findRouteButton.Enabled = true;
            return;
        }

        // backend caps /player-route samples at 500 (collector/collector.py
        // do_POST, PLAYER_ROUTE_MAX_SAMPLES) — mirror it here so the scan
        // itself never queues more pings than the final POST can accept.
        var scanTargets = targets.Take(500).ToList();

        // Pinged in batches (not fully unbounded parallel) to avoid opening
        // hundreds of UDP sockets simultaneously - ponytail: fixed batch
        // size, tune if 20 proves too slow/fast in practice.
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

            _progressBar.Value = Math.Min(100, (int)(completed / (double)scanTargets.Count * 100));
        }

        if (_samplesByKey.Count == 0)
        {
            SetStatus("sem dados (nenhum servidor respondeu ao ping)");
            _serverList.Items.Add("nenhum servidor respondeu - verifique sua conexão.");
            _findRouteButton.Enabled = true;
            return;
        }

        foreach (var (key, (target, rttMs)) in _samplesByKey.OrderBy(kv => kv.Value.rttMs))
        {
            _serverList.Items.Add($"{rttMs,6:F0} ms   {target.DisplayName}");
            _rowIsClickable.Add(true);
        }
        // Unresponsive targets shown at the end, greyed-out and unclickable
        // - so a server known to the mesh (like one that just doesn't
        // answer on this network right now) doesn't just silently vanish.
        foreach (var target in _unresponsiveTargets.OrderBy(t => t.DisplayName))
        {
            _serverList.Items.Add($"  sem resposta   {target.DisplayName}");
            _rowIsClickable.Add(false);
        }
        _serverList.Enabled = true;
        SetStatus(
            $"Concluído — {_samplesByKey.Count}/{scanTargets.Count} responderam " +
            $"({_unresponsiveTargets.Count} sem resposta). Clique num servidor pra ver a rota.");
        _findRouteButton.Enabled = true;
    }

    private async void ServerList_SelectedIndexChanged(object? sender, EventArgs e)
    {
        var index = _serverList.SelectedIndex;
        if (index < 0 || _samplesByKey.Count == 0) return;
        if (index >= _rowIsClickable.Count || !_rowIsClickable[index])
        {
            // Clicked an unresponsive row - nothing to route to, leave the
            // last real result (if any) on screen instead of clearing it.
            return;
        }

        // Items are inserted in the same order as _samplesByKey.OrderBy(...)
        // below, so the index maps back to the same ordered sequence.
        var ordered = _samplesByKey.OrderBy(kv => kv.Value.rttMs).ToList();
        if (index >= ordered.Count) return;
        var (_, (target, _)) = ordered[index];

        var toIpPort = $"{target.Ip}:{target.Port}";
        _routeResultBox.Text = $"Calculando rota até {target.DisplayName}...";

        var samples = _samplesByKey.Values.Select(v => (v.target.Ip, v.target.Port, v.rttMs)).ToList();
        var route = await _backend.PostRouteAsync(BackendBaseUrl, _uuid, toIpPort, samples);
        if (route is null)
        {
            _routeResultBox.Text = $"sem dados (backend não retornou rota até {target.DisplayName})";
            return;
        }

        var pathText = string.Join("  ->  ", route.Path);
        _routeResultBox.Text =
            $"MELHOR ROTA ATÉ {target.DisplayName}" + Environment.NewLine +
            $"({toIpPort})" + Environment.NewLine +
            Environment.NewLine +
            pathText + Environment.NewLine +
            Environment.NewLine +
            $"{route.Hops} hop(s)  •  {route.TotalPingMs:F0} ms total";
    }

    private void SetStatus(string text) => _statusLabel.Text = text;

    private void OnExit(object? sender, EventArgs e)
    {
        _trayIcon.Visible = false;
        Application.Exit();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        // Closing the window (X) minimizes to tray instead of exiting — the
        // app keeps running in the background, matching the original
        // tray-first design; only the tray menu's "Exit" truly quits.
        if (e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            Hide();
            return;
        }
        _trayIcon.Visible = false;
        base.OnFormClosing(e);
    }
}

/// <summary>Panel with rounded corners via GraphicsPath clip region - plain GDI+, no dependency.</summary>
internal sealed class RoundedPanel : Panel
{
    public int CornerRadius { get; set; } = 10;

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        using var path = RoundedRect(ClientRectangle, CornerRadius);
        Region = new Region(path);
    }

    private static System.Drawing.Drawing2D.GraphicsPath RoundedRect(Rectangle bounds, int radius)
    {
        var path = new System.Drawing.Drawing2D.GraphicsPath();
        var d = radius * 2;
        path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
}

/// <summary>Flat accent-colored button with rounded corners and hover state - plain owner-draw, no dependency.</summary>
internal sealed class AccentButton : Button
{
    private static readonly Color Normal = Color.FromArgb(88, 220, 150);
    private static readonly Color Hover = Color.FromArgb(108, 235, 170);
    private static readonly Color Pressed = Color.FromArgb(60, 160, 110);
    private static readonly Color TextColor = Color.FromArgb(14, 20, 18);

    public AccentButton()
    {
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        FlatAppearance.MouseOverBackColor = Hover;
        FlatAppearance.MouseDownBackColor = Pressed;
        BackColor = Normal;
        ForeColor = TextColor;
        Font = new Font("Segoe UI Semibold", 9.5f, FontStyle.Bold);
        Cursor = Cursors.Hand;
    }

    protected override void OnPaint(PaintEventArgs pevent)
    {
        base.OnPaint(pevent);
        using var path = new System.Drawing.Drawing2D.GraphicsPath();
        var d = Height; // radius = half height -> pill shape
        path.AddArc(0, 0, d, d, 90, 180);
        path.AddArc(Width - d, 0, d, d, 270, 180);
        path.CloseFigure();
        Region = new Region(path);
    }
}
