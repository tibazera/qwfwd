using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using PlayerPingApp;

var logPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "QwMeshPing", "host.log");
void Log(string message) {
    try { File.AppendAllText(logPath, $"{DateTime.UtcNow:O} {message}{Environment.NewLine}"); } catch { }
}
Log($"started args={args.Length}");

using var input = Console.OpenStandardInput();
using var output = Console.OpenStandardOutput();
var header = new byte[4];
while (true) {
    // One serial queue bounds socket count and keeps stdout frames atomic.
    if (await input.ReadAsync(header.AsMemory(0, 1)) == 0) break;
    try {
        await input.ReadExactlyAsync(header.AsMemory(1));
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > 4096) return 1;
        var body = new byte[length];
        await input.ReadExactlyAsync(body);
        using var document = JsonDocument.Parse(body);
        var request = document.RootElement;
        var id = request.GetProperty("id").GetString();
        if (id is null || id.Length > 64) return 1;
        object response;
        var op = request.GetProperty("op").GetString();
        if (op == "health") response = new { id, ok = true, version = "0.1.0" };
        else if (op == "ping" && request.TryGetProperty("target", out var target) &&
                 ParsePublicTarget(target.GetString(), out var ip, out var port)) {
            var rtt = await QwPing.MeasureAsync(ip, port, 1500);
            response = rtt.HasValue ? new { id, rtt_ms = rtt.Value } : (object)new { id, error = "udp_timeout" };
        } else response = new { id, error = "invalid_target_or_operation" };
        var bytes = JsonSerializer.SerializeToUtf8Bytes(response);
        BinaryPrimitives.WriteInt32LittleEndian(header, bytes.Length);
        await output.WriteAsync(header);
        await output.WriteAsync(bytes);
        await output.FlushAsync();
    } catch (Exception ex) when (ex is EndOfStreamException or JsonException or
             InvalidOperationException or KeyNotFoundException or IOException) {
        Log($"protocol_error {ex.GetType().Name}: {ex.Message}");
        return 1;
    }
}
Log("stdin_closed");
return 0;

static bool ParsePublicTarget(string? target, out string ip, out int port) {
    ip = ""; port = 0;
    var parts = target?.Split(':');
    if (parts?.Length != 2 || !int.TryParse(parts[1], out port) || port is < 1 or > 65535 ||
        !IPAddress.TryParse(parts[0], out var address) || address.AddressFamily != AddressFamily.InterNetwork) return false;
    var b = address.GetAddressBytes();
    // Reject private, loopback, link-local, multicast, reserved and documentation ranges.
    if (b[0] is 0 or 10 or 127 || b[0] >= 224 ||
        (b[0] == 100 && b[1] is >= 64 and <= 127) ||
        (b[0] == 169 && b[1] == 254) || (b[0] == 172 && b[1] is >= 16 and <= 31) ||
        (b[0] == 192 && (b[1] is 0 or 168 || (b[1] == 88 && b[2] == 99))) ||
        (b[0] == 198 && (b[1] is 18 or 19 || (b[1] == 51 && b[2] == 100))) ||
        (b[0] == 203 && b[1] == 0 && b[2] == 113)) return false;
    ip = address.ToString(); return true;
}
