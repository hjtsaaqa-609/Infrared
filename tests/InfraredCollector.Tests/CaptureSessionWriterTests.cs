using InfraredCollector.Core.Capture;
using InfraredCollector.Core.Configuration;
using InfraredCollector.Core.Mlx;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class CaptureSessionWriterTests
{
    [Fact]
    public void WritesMacCompatibleMlxAndTasiFiles()
    {
        using var temp = new TempDir();
        var timestamp = DateTimeOffset.UtcNow;
        var eeprom = new ushort[] { 0x1234, 0xABCD };
        var frameData = Enumerable.Range(0, Mlx90640Constants.FrameDataWords).Select(i => (ushort)i).ToArray();
        var temperature = Enumerable.Repeat(28.0f, Mlx90640Constants.PixelWords).ToArray();
        string session;

        using (var writer = new CaptureSessionWriter(temp.Path, new AppConfig(), new { test = true })) {
            writer.WriteMlxEeprom("left", eeprom);
            writer.WriteMlxSubpage(timestamp, "left", 0, "uid", 1, 0x0009, 0x1B01, 4, 0x0030, "raw-i2c", "status-bit", frameData);
            writer.WriteMlxFrame(timestamp, "left", 0, "uid", 1, 34.0f, temperature);
            var rawTasi = BuildRealtimeFrame();
            writer.WriteTasiSerialFrame(timestamp, rawTasi, TasiSerialProtocol.Parse(rawTasi));

            session = Directory.GetDirectories(temp.Path).Single();
        }

        Assert.StartsWith("win_dual_mlx_tasi_", Path.GetFileName(session), StringComparison.Ordinal);
        Assert.Equal(new byte[] { 0x34, 0x12, 0xCD, 0xAB }, File.ReadAllBytes(Path.Combine(session, "raw", "left_eeprom.u16le")));
        Assert.Equal(Mlx90640Constants.FrameDataWords * 2, new FileInfo(Path.Combine(session, "raw", "left_frameData.u16le")).Length);
        Assert.Equal(Mlx90640Constants.PixelWords * 4, new FileInfo(Path.Combine(session, "temp", "left_to.f32le")).Length);
        Assert.Equal(Mlx90640Constants.PixelWords, new FileInfo(Path.Combine(session, "temp", "left_infrared_thermal.bin")).Length);
        Assert.Equal(72, File.ReadAllBytes(Path.Combine(session, "temp", "left_infrared_thermal.bin"))[0]);
        Assert.Contains("robot_thermal_u8_offset_bytes", File.ReadAllText(Path.Combine(session, "left_mlx_frames.csv")));
        Assert.Contains("channel1_c", File.ReadAllText(Path.Combine(session, "tasi_serial_frames.csv")));
        Assert.Contains("left,", File.ReadAllText(Path.Combine(session, "joined_summary.csv")));
    }

    private static byte[] BuildRealtimeFrame()
    {
        var withoutChecksum = new byte[] { 0x55, 0xAA, 0x01, 0x0B, 0xF6, 0x00, 0x00, 0x01, 0x0A, 0x01, 0x14, 0x01 };
        return withoutChecksum.Concat(new[] { (byte)(withoutChecksum.Sum(b => b) & 0xFF) }).ToArray();
    }

    private sealed class TempDir : IDisposable
    {
        public TempDir()
        {
            Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "InfraredCollectorTests_" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path);
        }

        public string Path { get; }

        public void Dispose()
        {
            if (Directory.Exists(Path)) {
                Directory.Delete(Path, recursive: true);
            }
        }
    }
}
