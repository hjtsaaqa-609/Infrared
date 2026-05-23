namespace InfraredCollector.Core.Capture;

public sealed record MlxChannelConfig(string Name, uint UsbIndex, string? ExpectedBoardUid = null)
{
    public static MlxChannelConfig Left(uint usbIndex = 0) => new("left", usbIndex);
    public static MlxChannelConfig Right(uint usbIndex = 1) => new("right", usbIndex);
}
