namespace InfraredCollector.Core.Devices;

public sealed record Usb2UartBoardInfo(
    uint UsbIndex,
    string Uid,
    bool IsHighSpeedUsb,
    uint FirmwareVersion);
