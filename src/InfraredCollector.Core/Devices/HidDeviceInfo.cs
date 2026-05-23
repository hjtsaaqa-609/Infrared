namespace InfraredCollector.Core.Devices;

public sealed record HidDeviceInfo(
    string Path,
    ushort VendorId,
    ushort ProductId,
    string SerialNumber,
    string Manufacturer,
    string Product,
    ushort UsagePage,
    ushort Usage,
    int InterfaceNumber)
{
    public string DisplayName => $"{VendorId:X4}:{ProductId:X4} {Product} {SerialNumber}".Trim();
}
