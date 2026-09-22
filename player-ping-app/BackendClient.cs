using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PlayerPingApp;

internal sealed record PlayerTargetGeo(
    [property: JsonPropertyName("hostname")] string? Hostname,
    [property: JsonPropertyName("country")] string? Country,
    [property: JsonPropertyName("city")] string? City);

internal sealed record PlayerTarget(
    [property: JsonPropertyName("ip")] string Ip,
    [property: JsonPropertyName("port")] int Port,
    [property: JsonPropertyName("geo")] PlayerTargetGeo? Geo)
{
    /// <summary>Human-readable label for the destination picker - falls back
    /// gracefully when geo data is missing (a node the collector hasn't
    /// resolved a hostname for yet) rather than showing a blank entry.</summary>
    public string DisplayName =>
        Geo?.Hostname is { Length: > 0 } name
            ? $"{name} ({Geo.City ?? Geo.Country ?? "?"})"
            : $"{Ip}:{Port}";
}

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
            // Collector's minimal BaseHTTPRequestHandler reads exactly Content-Length bytes and
            // rejects chunked/absent-length bodies. PostAsJsonAsync's JsonContent sends chunked
            // (no Content-Length), so use StringContent, which sets Content-Length explicitly.
            var json = JsonSerializer.Serialize(payload);
            var content = new StringContent(json, Encoding.UTF8, "application/json");
            var response = await Http.PostAsync($"{baseUrl}/player-route?to={toIpPort}", content);
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
