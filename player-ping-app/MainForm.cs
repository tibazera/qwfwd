namespace PlayerPingApp;

/// <summary>
/// Main visible window - replaces the tray-only ApplicationContext. Keeps a
/// tray icon as a convenience (close-to-tray, quick relaunch) but the app
/// now opens with a real window by default, per user request: a MessageBox
/// only after a ~1min scan made it look frozen with no feedback. Styled
/// with a dark theme and card layout (also per request) instead of default
/// WinForms gray - still plain BCL WinForms, no new dependency.
/// </summary>
internal sealed class MainForm : Form
{
    private const string BackendBaseUrl = "http://127.0.0.1:8730"; // TODO: point at the real deployed collector before distributing beyond local testing

    private static readonly Color BgDark = Color.FromArgb(18, 18, 24);
    private static readonly Color CardBg = Color.FromArgb(28, 28, 38);
    private static readonly Color AccentGreen = Color.FromArgb(88, 220, 150);
    private static readonly Color AccentGreenDark = Color.FromArgb(60, 160, 110);
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
    private readonly TextBox _resultBox;

    public MainForm()
    {
        _uuid = ClientIdentity.GetOrCreateUuid();

        Text = "qwfwd player ping";
        Width = 620;
        Height = 480;
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(520, 380);
        BackColor = BgDark;
        ForeColor = TextPrimary;
        Font = FontBody;
        Padding = new Padding(20);

        var headerPanel = BuildHeaderPanel();
        var cardPanel = BuildCardPanel(out _resultBox);
        var footerPanel = BuildFooterPanel(out _findRouteButton, out _statusLabel, out _progressBar);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            BackColor = BgDark,
            ColumnCount = 1,
            RowCount = 3,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.Controls.Add(headerPanel, 0, 0);
        layout.Controls.Add(cardPanel, 0, 1);
        layout.Controls.Add(footerPanel, 0, 2);
        Controls.Add(layout);

        var menu = new ContextMenuStrip();
        menu.Items.Add("Abrir", null, (_, _) => ShowMainWindow());
        menu.Items.Add("Find best route", null, OnFindBestRoute);
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
            Text = "mede seu ping real e calcula a melhor rota até um servidor QW",
            Font = FontBody,
            ForeColor = TextMuted,
            AutoSize = true,
            Location = new Point(2, 34),
        };
        panel.Controls.Add(title);
        panel.Controls.Add(subtitle);
        return panel;
    }

    private Panel BuildCardPanel(out TextBox resultBox)
    {
        var outer = new Panel { Dock = DockStyle.Fill, BackColor = BgDark, Padding = new Padding(0, 0, 0, 12) };
        var card = new RoundedPanel
        {
            Dock = DockStyle.Fill,
            BackColor = CardBg,
            CornerRadius = 12,
            Padding = new Padding(16),
        };

        resultBox = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            ScrollBars = ScrollBars.Vertical,
            BackColor = CardBg,
            ForeColor = TextPrimary,
            Font = FontMono,
            Text = "UUID local: " + _uuid + Environment.NewLine +
                   Environment.NewLine +
                   "Clique em \"Find best route\" para medir seu ping até os" + Environment.NewLine +
                   "servidores conhecidos e descobrir a rota mais rápida.",
        };
        card.Controls.Add(resultBox);
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
        findRouteButton.Click += OnFindBestRoute;

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

    private async void OnFindBestRoute(object? sender, EventArgs e)
    {
        _findRouteButton.Enabled = false;
        _resultBox.Text = string.Empty;
        _progressBar.Value = 0;
        SetStatus("Buscando servidores conhecidos...");

        var targets = await _backend.GetTargetsAsync(BackendBaseUrl);
        if (targets.Count == 0)
        {
            SetStatus("sem dados (backend indisponível ou sem servidores conhecidos)");
            _findRouteButton.Enabled = true;
            return;
        }

        // backend caps /player-route samples at 50 (collector/collector.py do_POST) — cap the scan itself, not just the post
        var scanTargets = targets.Take(50).ToList();

        var samples = new List<(string ip, int port, double rttMs)>();
        for (var i = 0; i < scanTargets.Count; i++)
        {
            var target = scanTargets[i];
            SetStatus($"Medindo ping... {i + 1}/{scanTargets.Count} ({target.Ip}:{target.Port})");
            _progressBar.Value = Math.Min(100, (int)((i + 1) / (double)scanTargets.Count * 90));

            var rtt = await QwPing.MeasureAsync(target.Ip, target.Port);
            if (rtt.HasValue)
            {
                samples.Add((target.Ip, target.Port, rtt.Value));
                AppendResultLine($"{target.Ip}:{target.Port,-8}  {rtt.Value,6:F0} ms");
            }
        }

        if (samples.Count == 0)
        {
            SetStatus("sem dados (nenhum servidor respondeu ao ping)");
            _progressBar.Value = 0;
            _findRouteButton.Enabled = true;
            return;
        }

        // Destination: cheapest directly-measured sample this round - a
        // simple, honest default for v1 ("melhor rota pra onde eu já sei
        // que o ping é bom"), not a UI for picking an arbitrary target yet.
        var closest = samples.OrderBy(s => s.rttMs).First();
        var toIpPort = $"{closest.ip}:{closest.port}";

        SetStatus($"Calculando melhor rota até {toIpPort}...");
        var route = await _backend.PostRouteAsync(BackendBaseUrl, _uuid, toIpPort, samples);
        if (route is null)
        {
            SetStatus("sem dados (backend não retornou rota)");
            _progressBar.Value = 0;
            _findRouteButton.Enabled = true;
            return;
        }

        var pathText = string.Join("  ->  ", route.Path);
        AppendResultLine("");
        AppendResultLine("========================================");
        AppendResultLine($"MELHOR ROTA ATÉ {toIpPort}");
        AppendResultLine(pathText);
        AppendResultLine($"{route.Hops} hop(s)  •  {route.TotalPingMs:F0} ms total");
        AppendResultLine("========================================");
        _progressBar.Value = 100;
        SetStatus($"Concluído — {samples.Count}/{scanTargets.Count} servidores responderam.");
        _findRouteButton.Enabled = true;
    }

    private void SetStatus(string text) => _statusLabel.Text = text;

    private void AppendResultLine(string line)
    {
        _resultBox.AppendText(line + Environment.NewLine);
    }

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
