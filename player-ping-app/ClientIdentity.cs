namespace PlayerPingApp;

internal static class ClientIdentity
{
    private static readonly string StatePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "qwfwd-player-ping",
        "client-id.txt");

    public static string GetOrCreateUuid()
    {
        try
        {
            if (File.Exists(StatePath))
            {
                var existing = File.ReadAllText(StatePath).Trim();
                if (Guid.TryParse(existing, out _))
                {
                    return existing;
                }
            }
        }
        catch (IOException)
        {
            // fall through to generate a fresh one for this run; a
            // transient read failure shouldn't block the app from working,
            // it just means this run won't persist its id if the write
            // below also fails
        }

        var fresh = Guid.NewGuid().ToString();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StatePath)!);
            File.WriteAllText(StatePath, fresh);
        }
        catch (IOException)
        {
            // best-effort persistence; the uuid still works for this run
        }
        return fresh;
    }
}
