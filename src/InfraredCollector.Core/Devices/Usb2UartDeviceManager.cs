using System.Text;

namespace InfraredCollector.Core.Devices;

public sealed class Usb2UartDeviceManager
{
    public IReadOnlyList<Usb2UartBoardInfo> Scan(uint maxIndex = 99)
    {
        var devices = new List<Usb2UartBoardInfo>();
        for (uint i = 0; i <= maxIndex; i++) {
            var open = Usb2UartNative.OpenUsb(i);
            if (open < 0) {
                continue;
            }

            try {
                var uid = new byte[64];
                var info = Usb2UartNative.GetBoardInformation(uid, out var isHs, out var fw, i);
                var uidText = info >= 0 ? DecodeUid(uid) : $"USB{i:00}";
                devices.Add(new Usb2UartBoardInfo(i, uidText, isHs != 0, fw));
            }
            finally {
                Usb2UartNative.CloseUsb(i);
            }
        }

        return devices;
    }

    private static string DecodeUid(byte[] uid)
    {
        var nul = Array.IndexOf(uid, (byte)0);
        var len = nul >= 0 ? nul : uid.Length;
        var useful = uid.Take(len).ToArray();
        if (useful.Length >= 4 && useful.All(b => b is >= 0x20 and <= 0x7E)) {
            var text = Encoding.ASCII.GetString(useful).Trim();
            if (!string.IsNullOrWhiteSpace(text)) {
                return text;
            }
        }

        var nonZero = useful.Where(b => b != 0).DefaultIfEmpty().ToArray();
        return Convert.ToHexString(nonZero.Take(16).ToArray());
    }
}
