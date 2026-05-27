using System.Text;
using System.Text.Json;
using InfraredCollector.Core.Configuration;
using InfraredCollector.Core.Mlx;
using InfraredCollector.Core.Util;

namespace InfraredCollector.Core.Capture;

public sealed class CaptureSessionWriter : IDisposable
{
    private const float RobotThermalOffsetC = 44.0f;

    private readonly object _sync = new();
    private readonly StreamWriter _tasiCsv;
    private readonly StreamWriter _joinedCsv;
    private readonly Dictionary<string, StreamWriter> _subpageCsvWriters = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, StreamWriter> _frameCsvWriters = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, BinaryWriter> _frameDataWriters = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, BinaryWriter> _temperatureWriters = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, BinaryWriter> _robotThermalWriters = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _layoutWritten = new(StringComparer.OrdinalIgnoreCase);
    private readonly BinaryWriter _tasiRawWriter;
    private BinaryWriter? _tasiHidRawWriter;
    private TasiSerialRecord? _latestTasi;
    private bool _disposed;

    public CaptureSessionWriter(string captureRoot, AppConfig config, object sessionMetadata)
    {
        SessionDirectory = Path.Combine(captureRoot, "win_dual_mlx_tasi_" + DateTime.Now.ToString("yyyyMMdd_HHmmss"));
        RawDirectory = Path.Combine(SessionDirectory, "raw");
        TemperatureDirectory = Path.Combine(SessionDirectory, "temp");
        Directory.CreateDirectory(RawDirectory);
        Directory.CreateDirectory(TemperatureDirectory);

        File.WriteAllText(
            Path.Combine(SessionDirectory, "session.json"),
            JsonSerializer.Serialize(
                new
                {
                    createdEast8 = East8Clock.Now(),
                    kind = "win_dual_mlx_tasi",
                    config,
                    sessionMetadata,
                    rawFiles = DefaultRawFiles()
                },
                AppConfig.JsonOptions()));

        _tasiCsv = OpenCsvInSession("tasi_serial_frames.csv");
        _joinedCsv = OpenCsvInSession("joined_summary.csv");
        _tasiRawWriter = OpenBinary(Path.Combine(RawDirectory, "tasi_serial_frames.bin"));

        _tasiCsv.WriteLine(Csv.Row(
            "timestamp_east8",
            "raw_offset_bytes",
            "frame_length",
            "command",
            "checksum_ok",
            "channel1_c",
            "channel2_c",
            "channel3_c",
            "channel4_c",
            "model",
            "version",
            "raw_hex"));
        _joinedCsv.WriteLine(Csv.Row(
            "mlx_channel",
            "mlx_timestamp_east8",
            "mlx_subpage",
            "mlx_to_offset_bytes",
            "mlx_robot_thermal_u8_offset_bytes",
            "mlx_ta_c",
            "mlx_min_c",
            "mlx_max_c",
            "mlx_avg_c",
            "mlx_center_c",
            "tasi_timestamp_east8",
            "tasi_age_ms",
            "tasi_channel1_c",
            "tasi_channel2_c",
            "tasi_channel3_c",
            "tasi_channel4_c",
            "tasi_raw_offset_bytes",
            "tasi_checksum_ok"));
    }

    public string SessionDirectory { get; }
    public string RawDirectory { get; }
    public string TemperatureDirectory { get; }

    public void WriteMlxEeprom(string channel, ushort[] eepromWords)
    {
        var safe = SafeChannel(channel);
        lock (_sync) {
            WriteMlxLayoutFiles(safe);
            var binPath = Path.Combine(RawDirectory, $"{safe}_eeprom.u16le");
            using (var writer = OpenBinary(binPath)) {
                foreach (var word in eepromWords) {
                    writer.Write(word);
                }
            }

            using var csv = new StreamWriter(
                File.Open(Path.Combine(RawDirectory, $"{safe}_eeprom.csv"), FileMode.Create, FileAccess.Write, FileShare.Read),
                new UTF8Encoding(false));
            csv.WriteLine(Csv.Row("word_index", "address_hex", "word_hex", "word_dec"));
            for (var i = 0; i < eepromWords.Length; i++) {
                var address = Mlx90640Constants.EepromStartAddress + i;
                var word = eepromWords[i];
                csv.WriteLine(Csv.Row(i, $"0x{address:X4}", $"0x{word:X4}", word));
            }
        }
    }

    public MlxSubpageRecord WriteMlxSubpage(
        DateTimeOffset timestampUtc,
        string channel,
        uint usbIndex,
        string boardUid,
        int subPage,
        ushort status,
        ushort control,
        int polls,
        ushort statusAfterClear,
        string statusClearMethod,
        string subPageSource,
        ushort[] frameData)
    {
        var safe = SafeChannel(channel);
        lock (_sync) {
            WriteMlxLayoutFiles(safe);
            var writer = GetFrameDataWriter(safe);
            var offset = writer.BaseStream.Position;
            foreach (var value in frameData) {
                writer.Write(value);
            }
            writer.Flush();

            var timestampEast8 = East8Clock.ToEast8(timestampUtc);
            var record = new MlxSubpageRecord(timestampEast8, safe, usbIndex, boardUid, subPage, status, control, polls, statusAfterClear, statusClearMethod, subPageSource, offset, frameData.Length);
            var csv = GetSubpageCsv(safe);
            csv.WriteLine(Csv.Row(
                East8Clock.Format(timestampEast8), subPage,
                $"0x{status:X4}", $"0x{control:X4}", polls, $"0x{statusAfterClear:X4}", statusClearMethod, subPageSource, offset, frameData.Length));
            csv.Flush();
            return record;
        }
    }

    public MlxFrameSummary WriteMlxFrame(DateTimeOffset timestampUtc, string channel, uint usbIndex, string boardUid, int subPage, float ambientTemperature, float[] temperature)
    {
        var safe = SafeChannel(channel);
        lock (_sync) {
            WriteMlxLayoutFiles(safe);
            var temperatureWriter = GetTemperatureWriter(safe);
            var temperatureOffset = temperatureWriter.BaseStream.Position;
            foreach (var value in temperature) {
                temperatureWriter.Write(value);
            }
            temperatureWriter.Flush();

            var robotBytes = ToRobotThermalBytes(temperature);
            var robotWriter = GetRobotThermalWriter(safe);
            var robotOffset = robotWriter.BaseStream.Position;
            robotWriter.Write(robotBytes);
            robotWriter.Flush();
            File.WriteAllBytes(Path.Combine(TemperatureDirectory, $"{safe}_infrared_thermal_latest.bin"), robotBytes);

            var timestampEast8 = East8Clock.ToEast8(timestampUtc);
            var summary = Summarize(timestampEast8, safe, usbIndex, boardUid, subPage, temperatureOffset, ambientTemperature, temperature, robotOffset, robotBytes.Length);
            var csv = GetFrameCsv(safe);
            csv.WriteLine(Csv.Row(
                East8Clock.Format(timestampEast8),
                subPage,
                summary.TemperatureOffsetBytes,
                summary.RobotThermalOffsetBytes,
                summary.RobotThermalBytes,
                summary.PixelCount,
                summary.AmbientTemperature,
                summary.Min,
                summary.Max,
                summary.Average,
                summary.Center));
            csv.Flush();

            WriteJoinedRow(summary);
            return summary;
        }
    }

    public TasiSerialRecord WriteTasiSerialFrame(DateTimeOffset timestampUtc, byte[] raw, TasiSerialFrame parsed)
    {
        lock (_sync) {
            var offset = _tasiRawWriter.BaseStream.Position;
            _tasiRawWriter.Write(raw.Length);
            _tasiRawWriter.Write(raw);
            _tasiRawWriter.Flush();

            var timestampEast8 = East8Clock.ToEast8(timestampUtc);
            var record = new TasiSerialRecord(
                timestampEast8,
                offset,
                raw.Length,
                parsed.Command,
                parsed.ChecksumOk,
                parsed.ChannelsC,
                parsed.Model,
                parsed.Version,
                Convert.ToHexString(raw).ToLowerInvariant());
            _latestTasi = record;

            _tasiCsv.WriteLine(Csv.Row(
                East8Clock.Format(timestampEast8),
                offset,
                raw.Length,
                $"0x{parsed.Command:X2}",
                parsed.ChecksumOk,
                ChannelValue(parsed.ChannelsC, 0),
                ChannelValue(parsed.ChannelsC, 1),
                ChannelValue(parsed.ChannelsC, 2),
                ChannelValue(parsed.ChannelsC, 3),
                parsed.Model,
                parsed.Version,
                record.RawHex));
            _tasiCsv.Flush();
            return record;
        }
    }

    public TasiRawRecord WriteTasiRaw(DateTimeOffset timestampUtc, byte[] report, int reportLength)
    {
        lock (_sync) {
            _tasiHidRawWriter ??= OpenBinary(Path.Combine(RawDirectory, "tasi_hid_reports.bin"));
            var offset = _tasiHidRawWriter.BaseStream.Position;
            _tasiHidRawWriter.Write(reportLength);
            _tasiHidRawWriter.Write(report, 0, reportLength);
            _tasiHidRawWriter.Flush();

            var raw = Convert.ToHexString(report.AsSpan(0, reportLength)).ToLowerInvariant();
            return new TasiRawRecord(East8Clock.ToEast8(timestampUtc), offset, reportLength, raw, "raw_hid_unparsed");
        }
    }

    public void Dispose()
    {
        if (_disposed) {
            return;
        }

        lock (_sync) {
            foreach (var writer in _subpageCsvWriters.Values) {
                writer.Dispose();
            }
            foreach (var writer in _frameCsvWriters.Values) {
                writer.Dispose();
            }
            foreach (var writer in _frameDataWriters.Values) {
                writer.Dispose();
            }
            foreach (var writer in _temperatureWriters.Values) {
                writer.Dispose();
            }
            foreach (var writer in _robotThermalWriters.Values) {
                writer.Dispose();
            }
            _tasiCsv.Dispose();
            _joinedCsv.Dispose();
            _tasiRawWriter.Dispose();
            _tasiHidRawWriter?.Dispose();
            _disposed = true;
        }
    }

    private static Dictionary<string, string> DefaultRawFiles()
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["tasiSerialFramesBin"] = "raw/tasi_serial_frames.bin",
            ["tasiSerialFramesCsv"] = "tasi_serial_frames.csv",
            ["joinedSummaryCsv"] = "joined_summary.csv"
        };
        foreach (var channel in new[] { "left", "right" }) {
            result[$"{channel}EepromU16Le"] = $"raw/{channel}_eeprom.u16le";
            result[$"{channel}EepromCsv"] = $"raw/{channel}_eeprom.csv";
            result[$"{channel}FrameDataU16Le"] = $"raw/{channel}_frameData.u16le";
            result[$"{channel}FrameDataLayoutJson"] = $"raw/{channel}_frameData.layout.json";
            result[$"{channel}TemperatureF32Le"] = $"temp/{channel}_to.f32le";
            result[$"{channel}TemperatureLayoutJson"] = $"temp/{channel}_to.layout.json";
            result[$"{channel}RobotThermalU8Bin"] = $"temp/{channel}_infrared_thermal.bin";
            result[$"{channel}RobotThermalLatestU8Bin"] = $"temp/{channel}_infrared_thermal_latest.bin";
            result[$"{channel}RobotThermalLayoutJson"] = $"temp/{channel}_infrared_thermal.layout.json";
            result[$"{channel}SubpagesCsv"] = $"{channel}_mlx_subpages.csv";
            result[$"{channel}FramesCsv"] = $"{channel}_mlx_frames.csv";
        }
        return result;
    }

    private StreamWriter GetSubpageCsv(string channel)
    {
        if (!_subpageCsvWriters.TryGetValue(channel, out var writer)) {
            writer = OpenCsvInSession($"{channel}_mlx_subpages.csv");
            writer.WriteLine(Csv.Row(
                "timestamp_east8",
                "subpage",
                "status_register_hex",
                "control_register_hex",
                "polls",
                "status_after_clear_hex",
                "status_clear_method",
                "subpage_source",
                "frameData_offset_bytes",
                "frameData_words"));
            _subpageCsvWriters[channel] = writer;
        }
        return writer;
    }

    private StreamWriter GetFrameCsv(string channel)
    {
        if (!_frameCsvWriters.TryGetValue(channel, out var writer)) {
            writer = OpenCsvInSession($"{channel}_mlx_frames.csv");
            writer.WriteLine(Csv.Row(
                "timestamp_east8",
                "subpage",
                "to_offset_bytes",
                "robot_thermal_u8_offset_bytes",
                "robot_thermal_u8_bytes",
                "pixel_count",
                "ta_c",
                "min_c",
                "max_c",
                "avg_c",
                "center_c"));
            _frameCsvWriters[channel] = writer;
        }
        return writer;
    }

    private BinaryWriter GetFrameDataWriter(string channel)
    {
        if (!_frameDataWriters.TryGetValue(channel, out var writer)) {
            writer = OpenBinary(Path.Combine(RawDirectory, $"{channel}_frameData.u16le"));
            _frameDataWriters[channel] = writer;
        }
        return writer;
    }

    private BinaryWriter GetTemperatureWriter(string channel)
    {
        if (!_temperatureWriters.TryGetValue(channel, out var writer)) {
            writer = OpenBinary(Path.Combine(TemperatureDirectory, $"{channel}_to.f32le"));
            _temperatureWriters[channel] = writer;
        }
        return writer;
    }

    private BinaryWriter GetRobotThermalWriter(string channel)
    {
        if (!_robotThermalWriters.TryGetValue(channel, out var writer)) {
            writer = OpenBinary(Path.Combine(TemperatureDirectory, $"{channel}_infrared_thermal.bin"));
            _robotThermalWriters[channel] = writer;
        }
        return writer;
    }

    private void WriteJoinedRow(MlxFrameSummary summary)
    {
        var tasi = _latestTasi;
        var ageMs = tasi is null ? null : (double?)(summary.TimestampUtc - tasi.TimestampUtc).TotalMilliseconds;
        _joinedCsv.WriteLine(Csv.Row(
            summary.Channel,
            East8Clock.Format(summary.TimestampUtc),
            summary.SubPage,
            summary.TemperatureOffsetBytes,
            summary.RobotThermalOffsetBytes,
            summary.AmbientTemperature,
            summary.Min,
            summary.Max,
            summary.Average,
            summary.Center,
            tasi is null ? null : East8Clock.Format(tasi.TimestampUtc),
            ageMs,
            ChannelValue(tasi?.ChannelsC, 0),
            ChannelValue(tasi?.ChannelsC, 1),
            ChannelValue(tasi?.ChannelsC, 2),
            ChannelValue(tasi?.ChannelsC, 3),
            tasi?.RawOffsetBytes,
            tasi?.ChecksumOk));
        _joinedCsv.Flush();
    }

    private void WriteMlxLayoutFiles(string channel)
    {
        if (!_layoutWritten.Add(channel)) {
            return;
        }

        var frameLayout = new
        {
            channel,
            format = "u16le-stream",
            recordWords = Mlx90640Constants.FrameDataWords,
            recordBytes = Mlx90640Constants.FrameDataWords * 2,
            endianness = "little",
            reference = new
            {
                archive = "docs/infrared.rar",
                type = "infrared::FrameData",
                definition = "std::array<uint16_t, kFrameWordCount>",
                wordCount = Mlx90640Constants.FrameDataWords
            },
            layout = new object[]
            {
                new { name = "pixelData", startWord = 0, wordCount = Mlx90640Constants.PixelWords, sourceRegisterStartHex = $"0x{Mlx90640Constants.PixelDataStartAddress:X4}" },
                new { name = "auxData", startWord = Mlx90640Constants.PixelWords, wordCount = Mlx90640Constants.AuxWords, sourceRegisterStartHex = $"0x{Mlx90640Constants.AuxDataStartAddress:X4}" },
                new { name = "controlRegister1", wordIndex = 832, sourceRegisterHex = $"0x{Mlx90640Constants.ControlRegister:X4}" },
                new { name = "subpage", wordIndex = 833, source = "statusRegister & 0x0001" }
            },
            index = new { csv = $"{channel}_mlx_subpages.csv", offsetColumn = "frameData_offset_bytes", wordsColumn = "frameData_words" }
        };
        var temperatureLayout = new
        {
            channel,
            format = "f32le-stream",
            recordValues = Mlx90640Constants.PixelWords,
            recordBytes = Mlx90640Constants.PixelWords * 4,
            endianness = "little",
            reference = new
            {
                archive = "docs/infrared.rar",
                type = "infrared::TemperatureArray",
                definition = "std::array<float, kPixelCount>",
                valueCount = Mlx90640Constants.PixelWords
            },
            geometry = new { width = 32, height = 24, order = "row-major" },
            index = new { csv = $"{channel}_mlx_frames.csv", offsetColumn = "to_offset_bytes", valuesColumn = "pixel_count" }
        };
        var robotLayout = new
        {
            channel,
            format = "u8-stream",
            recordValues = Mlx90640Constants.PixelWords,
            recordBytes = Mlx90640Constants.PixelWords,
            geometry = new { width = 32, height = 24, order = "row-major" },
            source = $"temp/{channel}_to.f32le",
            conversion = new
            {
                formula = "uint8(clamp(floor(temp_c + offset_c + 0.5), 0, 255))",
                offsetC = RobotThermalOffsetC,
                inverseEstimate = "temp_c ~= raw_byte - offset_c"
            },
            compatibility = new
            {
                robotFilePattern = "*_infrared_thermal.bin",
                singleFrameBytes = Mlx90640Constants.PixelWords,
                latestFramePath = $"temp/{channel}_infrared_thermal_latest.bin"
            },
            index = new { csv = $"{channel}_mlx_frames.csv", offsetColumn = "robot_thermal_u8_offset_bytes", bytesColumn = "robot_thermal_u8_bytes" }
        };

        File.WriteAllText(Path.Combine(RawDirectory, $"{channel}_frameData.layout.json"), JsonSerializer.Serialize(frameLayout, AppConfig.JsonOptions()));
        File.WriteAllText(Path.Combine(TemperatureDirectory, $"{channel}_to.layout.json"), JsonSerializer.Serialize(temperatureLayout, AppConfig.JsonOptions()));
        File.WriteAllText(Path.Combine(TemperatureDirectory, $"{channel}_infrared_thermal.layout.json"), JsonSerializer.Serialize(robotLayout, AppConfig.JsonOptions()));
    }

    private StreamWriter OpenCsvInSession(string fileName)
    {
        return new StreamWriter(File.Open(Path.Combine(SessionDirectory, fileName), FileMode.CreateNew, FileAccess.Write, FileShare.Read), new UTF8Encoding(false));
    }

    private static BinaryWriter OpenBinary(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        return new BinaryWriter(File.Open(path, FileMode.Create, FileAccess.Write, FileShare.Read));
    }

    private static MlxFrameSummary Summarize(
        DateTimeOffset timestampUtc,
        string channel,
        uint usbIndex,
        string boardUid,
        int subPage,
        long offset,
        float ambientTemperature,
        float[] temperature,
        long robotThermalOffset,
        int robotThermalBytes)
    {
        var valid = temperature.Where(v => !float.IsNaN(v) && !float.IsInfinity(v)).ToArray();
        var min = valid.Length == 0 ? float.NaN : valid.Min();
        var max = valid.Length == 0 ? float.NaN : valid.Max();
        var avg = valid.Length == 0 ? float.NaN : valid.Average();
        var center = temperature[12 * 32 + 16];
        return new MlxFrameSummary(timestampUtc, channel, usbIndex, boardUid, subPage, offset, temperature.Length, ambientTemperature, min, max, avg, center, robotThermalOffset, robotThermalBytes);
    }

    private static byte[] ToRobotThermalBytes(float[] temperature)
    {
        if (temperature.Length != Mlx90640Constants.PixelWords) {
            throw new ArgumentException($"Expected {Mlx90640Constants.PixelWords} temperature pixels, got {temperature.Length}.", nameof(temperature));
        }

        var bytes = new byte[temperature.Length];
        for (var i = 0; i < temperature.Length; i++) {
            var value = temperature[i];
            if (float.IsNaN(value) || float.IsInfinity(value)) {
                bytes[i] = 0;
                continue;
            }
            var raw = (int)MathF.Floor(value + RobotThermalOffsetC + 0.5f);
            bytes[i] = (byte)Math.Clamp(raw, 0, 255);
        }
        return bytes;
    }

    private static object? ChannelValue(float[]? channels, int index)
    {
        return channels is not null && index < channels.Length ? channels[index] : null;
    }

    private static string SafeChannel(string channel)
    {
        var cleaned = new string(channel.Trim().ToLowerInvariant().Select(ch => char.IsLetterOrDigit(ch) || ch is '_' or '-' ? ch : '_').ToArray()).Trim('_', '-');
        if (string.IsNullOrWhiteSpace(cleaned)) {
            throw new ArgumentException("MLX channel name cannot be empty.", nameof(channel));
        }
        return cleaned;
    }
}
