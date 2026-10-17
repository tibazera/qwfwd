using System.Net;
using System.Net.Sockets;
using PlayerPingApp;

using var server = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0));
var port = ((IPEndPoint)server.Client.LocalEndPoint!).Port;
var pending = QwPing.MeasureAsync("127.0.0.1", port, 500);
var request = await server.ReceiveAsync();
if (request.Buffer[^1] != 10) throw new Exception("QW newline missing");
await server.SendAsync(new byte[] { 1, 2, 3 }, request.RemoteEndPoint);
await Task.Delay(30);
if (pending.IsCompleted) throw new Exception("Invalid reply accepted");
await server.SendAsync(new byte[] {255,255,255,255,(byte)'c',(byte)'1',0}, request.RemoteEndPoint);
if (await pending is not > 0) throw new Exception("Valid challenge rejected");
if (await QwPing.MeasureAsync("127.0.0.1", port, 50) is not null)
    throw new Exception("Timeout counted as ping");
Console.WriteLine("PASS: QW packet, invalid reply ignored, valid challenge measured, timeout");
