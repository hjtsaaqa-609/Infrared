namespace InfraredCollector.Core.Capture;

public sealed record MlxSubpageRecord(
    DateTimeOffset TimestampUtc,
    string Channel,
    uint UsbIndex,
    string BoardUid,
    int SubPage,
    ushort StatusRegister,
    ushort ControlRegister,
    int DataReadyPolls,
    ushort StatusAfterClear,
    string StatusClearMethod,
    string SubPageSource,
    long FrameDataOffsetBytes,
    int FrameDataWords);

public sealed record MlxFrameSummary(
    DateTimeOffset TimestampUtc,
    string Channel,
    uint UsbIndex,
    string BoardUid,
    int SubPage,
    long TemperatureOffsetBytes,
    int PixelCount,
    float AmbientTemperature,
    float Min,
    float Max,
    float Average,
    float Center,
    long RobotThermalOffsetBytes = 0,
    int RobotThermalBytes = 0);

public sealed record TasiRawRecord(
    DateTimeOffset TimestampUtc,
    long RawOffsetBytes,
    int ReportLength,
    string RawHex,
    string ParseStatus);

public sealed record TasiSerialRecord(
    DateTimeOffset TimestampUtc,
    long RawOffsetBytes,
    int FrameLength,
    byte Command,
    bool ChecksumOk,
    float[]? ChannelsC,
    int? Model,
    float? Version,
    string RawHex);
