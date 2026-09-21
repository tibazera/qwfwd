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
