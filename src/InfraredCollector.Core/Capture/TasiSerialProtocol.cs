using System.Buffers.Binary;

namespace InfraredCollector.Core.Capture;

public static class TasiSerialProtocol
{
    public static readonly byte[] StartRealtimeCommand = [0xAA, 0x55, 0x01, 0x03, 0x03];
    public static readonly byte[] StopCommand = [0xAA, 0x55, 0x00, 0x03, 0x02];

    private static readonly byte[] DeviceHeader = [0x55, 0xAA];
    private static readonly byte[] HostHeader = [0xAA, 0x55];

    public static bool ChecksumOk(ReadOnlySpan<byte> raw)
    {
        if (raw.Length < 5) {
            return false;
        }
        var sum = 0;
        for (var i = 0; i < raw.Length - 1; i++) {
            sum += raw[i];
        }
        return (sum & 0xFF) == raw[^1];
    }

    public static TasiSerialFrame Parse(ReadOnlySpan<byte> raw)
    {
        if (raw.Length < 5) {
            throw new ArgumentException("TA612 frame is too short.", nameof(raw));
        }

        var length = raw[3];
        var lengthIncludesChecksum = length == raw.Length - 2;
        var lengthExcludesChecksum = length == raw.Length - 3;
        if (!lengthIncludesChecksum && !lengthExcludesChecksum) {
            throw new ArgumentException($"TA612 frame length mismatch: field={length}, actual={raw.Length}.");
        }

        var command = raw[2];
        var data = raw[4..^1];
        float[]? channels = null;
        int? model = null;
        float? version = null;
        if ((command == 0x01 || command == 0x02) && data.Length >= 8) {
            channels =
            [
                BinaryPrimitives.ReadInt16LittleEndian(data[0..2]) / 10.0f,
                BinaryPrimitives.ReadInt16LittleEndian(data[2..4]) / 10.0f,
                BinaryPrimitives.ReadInt16LittleEndian(data[4..6]) / 10.0f,
                BinaryPrimitives.ReadInt16LittleEndian(data[6..8]) / 10.0f
            ];
        }
        if (command == 0x00 && data.Length >= 4) {
            model = BinaryPrimitives.ReadUInt16LittleEndian(data[0..2]);
            version = BinaryPrimitives.ReadUInt16LittleEndian(data[2..4]) / 100.0f;
        }

        return new TasiSerialFrame(command, length, lengthIncludesChecksum, ChecksumOk(raw), channels, model, version);
    }

    public static bool TryReadFrame(List<byte> buffer, out byte[] frame, bool acceptHostHeader = false)
    {
        frame = [];
        while (true) {
            var start = FindHeader(buffer, acceptHostHeader);
            if (start < 0) {
                var keep = Math.Max(DeviceHeader.Length - 1, 0);
                if (buffer.Count > keep) {
                    buffer.RemoveRange(0, buffer.Count - keep);
                }
                return false;
            }
            if (start > 0) {
                buffer.RemoveRange(0, start);
            }
            if (buffer.Count < 5) {
                return false;
            }

            var length = buffer[3];
            if (length < 3 || length > 62) {
                buffer.RemoveAt(0);
                continue;
            }

            var totals = new[] { 2 + length, 3 + length };
            foreach (var total in totals) {
                if (buffer.Count >= total) {
                    var candidate = buffer.Take(total).ToArray();
                    if (ChecksumOk(candidate)) {
                        buffer.RemoveRange(0, total);
                        frame = candidate;
                        return true;
                    }
                }
            }
            if (buffer.Count < totals[^1]) {
                return false;
            }
            buffer.RemoveAt(0);
        }
    }

    private static int FindHeader(List<byte> buffer, bool acceptHostHeader)
    {
        var best = FindSequence(buffer, DeviceHeader);
        if (acceptHostHeader) {
            var host = FindSequence(buffer, HostHeader);
            if (host >= 0 && (best < 0 || host < best)) {
                best = host;
            }
        }
        return best;
    }

    private static int FindSequence(List<byte> buffer, byte[] sequence)
    {
        for (var i = 0; i <= buffer.Count - sequence.Length; i++) {
            var matched = true;
            for (var j = 0; j < sequence.Length; j++) {
                if (buffer[i + j] != sequence[j]) {
                    matched = false;
                    break;
                }
            }
            if (matched) {
                return i;
            }
        }
        return -1;
    }
}

public sealed record TasiSerialFrame(
    byte Command,
    int Length,
    bool LengthIncludesChecksum,
    bool ChecksumOk,
    float[]? ChannelsC,
    int? Model,
    float? Version);
