using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace PlayerPingApp;

internal static class QwPing
{
    private static readonly byte[] GetChallengePacket =
        new byte[] { 0xFF, 0xFF, 0xFF, 0xFF }.Concat(Encoding.ASCII.GetBytes("getchallenge\n")).ToArray();

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

            client.Connect(endpoint);
            var started = Stopwatch.StartNew();
            await client.SendAsync(GetChallengePacket);
            using var cts = new CancellationTokenSource(timeoutMs);
            while (true)
            {
                var reply = await client.ReceiveAsync(cts.Token);
                var data = reply.Buffer;
                // QW challenge: OOB header followed by S2C_CHALLENGE ('c').
                if (reply.RemoteEndPoint.Equals(endpoint) && data.Length >= 6 &&
                    data[0] == 255 && data[1] == 255 && data[2] == 255 && data[3] == 255 &&
                    data[4] == (byte)'c' && (data[5] == (byte)'-' || data[5] >= (byte)'0' && data[5] <= (byte)'9'))
                    return started.Elapsed.TotalMilliseconds;
            }
        }
        catch (Exception ex) when (ex is SocketException or FormatException or ObjectDisposedException or OperationCanceledException)
        {
            return null;
        }
    }
}
