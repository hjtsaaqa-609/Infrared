using System.Text.Json;
using System.Text.Json.Serialization;

namespace InfraredCollector.Core.Configuration;

public sealed class AppConfig
{
    public string CaptureRoot { get; set; } = "captures";
    public double Emissivity { get; set; } = 0.95;
    public byte MlxI2cAddress { get; set; } = 0x33;
    public int I2cClockStretchLevel { get; set; } = 10000;
    public int MlxReadChunkWords { get; set; } = 64;
    public bool UseRepeatedStartForRegisterRead { get; set; }
    public string TasiSerialPort { get; set; } = "";
    public int TasiBaudRate { get; set; } = 9600;
    public double TasiPollIntervalSeconds { get; set; } = 1.0;
    public int TasiSerialReadSize { get; set; } = 64;
    public int HidInputReportLength { get; set; } = 64;
    public List<TasiProbeAlias> TasiProbeAliases { get; set; } = [];

    public static AppConfig LoadOrDefault(string path)
    {
        if (!File.Exists(path)) {
            return new AppConfig();
        }

        var json = File.ReadAllText(path);
        return JsonSerializer.Deserialize<AppConfig>(json, JsonOptions()) ?? new AppConfig();
    }

    public void Save(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        File.WriteAllText(path, JsonSerializer.Serialize(this, JsonOptions()));
    }

    public static JsonSerializerOptions JsonOptions() => new()
    {
        WriteIndented = true,
        Converters = { new JsonStringEnumConverter() }
    };
}

public sealed class TasiProbeAlias
{
    public int Channel { get; set; }
    public string Name { get; set; } = "";
    public string DeviceUnderTest { get; set; } = "";
}
