using System.Net;
using System.Text;
using System.Text.Json;

namespace PlayerPingApp;

/// <summary>
/// Minimal local HTTP bridge so the public site (gh-pages, HTTPS) can use
/// this app's real UDP ping instead of STUN/geographic estimate, when the
/// app happens to be open on the player's machine. Validated by spike: a
/// page served over real HTTPS can fetch() http://127.0.0.1 without
/// mixed-content or Private Network Access blocking it (Chrome, tested
/// 2026-09-21). See docs/superpowers/specs/2026-09-21-site-app-local-bridge-design.md.
///
/// Single endpoint, CORS-open (same posture as the public collector - the
/// data returned, a round-trip time to an already-public mesh address, is
/// not sensitive): GET /ping?target=ip:port -> {"rtt_ms": N} or
/// {"error": "..."}.
/// </summary>
internal sealed class LocalPingServer
{
    private const string Prefix = "http://127.0.0.1:5757/";

    private readonly HttpListener _listener = new();
    private CancellationTokenSource? _cts;

    /// <summary>
    /// Starts listening in the background. If the port is already taken
    /// (another instance of this app, or an unrelated process), logs to
    /// the console and returns without throwing - the rest of the app
    /// (manual scan flow) must keep working regardless.
    /// </summary>
    public void Start()
    {
        try
        {
            _listener.Prefixes.Add(Prefix);
            _listener.Start();
        }
        catch (HttpListenerException ex)
        {
            Console.WriteLine($"[LocalPingServer] could not bind {Prefix}: {ex.Message}");
            return;
        }

        _cts = new CancellationTokenSource();
        _ = RunAsync(_cts.Token);
    }

    public void Stop()
    {
        _cts?.Cancel();
        if (_listener.IsListening)
        {
            _listener.Stop();
        }
        _listener.Close();
    }

    private async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested && _listener.IsListening)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync();
            }
            catch (Exception ex) when (ex is HttpListenerException or ObjectDisposedException)
            {
                return; // listener was stopped
            }

            _ = HandleRequestAsync(context); // fire-and-forget, each request independent
        }
    }

    private async Task HandleRequestAsync(HttpListenerContext context)
    {
        var response = context.Response;
        response.AddHeader("Access-Control-Allow-Origin", "*");
        response.ContentType = "application/json";

        try
        {
            if (context.Request.Url?.AbsolutePath != "/ping")
            {
                await WriteJsonAsync(response, 404, new { error = "not found" });
                return;
            }

            var target = context.Request.QueryString["target"];
            if (!TryParseTarget(target, out var ip, out var port))
            {
                await WriteJsonAsync(response, 400, new { error = "usage: /ping?target=ip:port" });
                return;
            }

            var rtt = await QwPing.MeasureAsync(ip, port);
            if (rtt.HasValue)
            {
                await WriteJsonAsync(response, 200, new { rtt_ms = rtt.Value });
            }
            else
            {
                await WriteJsonAsync(response, 200, new { error = "timeout" });
            }
        }
        catch (Exception ex)
        {
            // Any unexpected failure still gets a response - an unclosed
            // HttpListenerContext leaks a connection and can eventually
            // starve the listener.
            try
            {
                await WriteJsonAsync(response, 500, new { error = ex.Message });
            }
            catch
            {
                // response already broken; nothing more to do
            }
        }
    }

    private static bool TryParseTarget(string? target, out string ip, out int port)
    {
        ip = "";
        port = 0;
        if (string.IsNullOrEmpty(target)) return false;
        var separator = target.LastIndexOf(':');
        if (separator <= 0 || separator == target.Length - 1) return false;

        var ipPart = target[..separator];
        var portPart = target[(separator + 1)..];
        if (!IPAddress.TryParse(ipPart, out _)) return false;
        if (!int.TryParse(portPart, out port) || port is <= 0 or > 65535) return false;

        ip = ipPart;
        return true;
    }

    private static async Task WriteJsonAsync(HttpListenerResponse response, int statusCode, object payload)
    {
        response.StatusCode = statusCode;
        var json = JsonSerializer.Serialize(payload);
        var bytes = Encoding.UTF8.GetBytes(json);
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes);
        response.OutputStream.Close();
    }
}
