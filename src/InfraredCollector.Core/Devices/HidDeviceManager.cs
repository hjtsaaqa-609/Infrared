using System.Runtime.InteropServices;

namespace InfraredCollector.Core.Devices;

public sealed class HidDeviceManager
{
    public IReadOnlyList<HidDeviceInfo> Scan()
    {
        HidApiNative.Init();
        var result = new List<HidDeviceInfo>();
        var root = HidApiNative.Enumerate(0, 0);
        if (root == IntPtr.Zero) {
            return result;
        }

        try {
            var current = root;
            while (current != IntPtr.Zero) {
                var native = Marshal.PtrToStructure<HidApiNative.HidDeviceInfoNative>(current);
                result.Add(new HidDeviceInfo(
                    PtrAnsi(native.path),
                    native.vendor_id,
                    native.product_id,
                    PtrUni(native.serial_number),
                    PtrUni(native.manufacturer_string),
                    PtrUni(native.product_string),
                    native.usage_page,
                    native.usage,
                    native.interface_number));
                current = native.next;
            }
        }
        finally {
            HidApiNative.FreeEnumeration(root);
        }

        return result;
    }

    private static string PtrAnsi(IntPtr ptr) => ptr == IntPtr.Zero ? "" : Marshal.PtrToStringAnsi(ptr) ?? "";

    private static string PtrUni(IntPtr ptr) => ptr == IntPtr.Zero ? "" : Marshal.PtrToStringUni(ptr) ?? "";
}
