using InfraredCollector.Core.Capture;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class TasiSerialProtocolTests
{
    [Fact]
    public void Parse_DecodesTa612RealtimeChannels()
    {
        var withoutChecksum = new byte[] { 0x55, 0xAA, 0x01, 0x0B, 0xF6, 0x00, 0x00, 0x01, 0x0A, 0x01, 0x14, 0x01 };
        var raw = withoutChecksum.Concat(new[] { (byte)(withoutChecksum.Sum(b => b) & 0xFF) }).ToArray();

        var parsed = TasiSerialProtocol.Parse(raw);

        Assert.True(parsed.ChecksumOk);
        Assert.Equal(0x01, parsed.Command);
        Assert.Equal(new[] { 24.6f, 25.6f, 26.6f, 27.6f }, parsed.ChannelsC);
    }

    [Fact]
    public void TryReadFrame_SkipsNoiseAndReturnsOneFrame()
    {
        var withoutChecksum = new byte[] { 0x55, 0xAA, 0x00, 0x06, 0x64, 0x02, 0x64, 0x00 };
        var raw = withoutChecksum.Concat(new[] { (byte)(withoutChecksum.Sum(b => b) & 0xFF) }).ToArray();
        var buffer = new List<byte>(new byte[] { 0x00, 0xFF, 0x10 }.Concat(raw));

        Assert.True(TasiSerialProtocol.TryReadFrame(buffer, out var frame));
        Assert.Equal(raw, frame);
        Assert.Empty(buffer);
    }
}
