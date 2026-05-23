using System.Runtime.InteropServices;

namespace InfraredCollector.Core.Devices;

internal static class HidApiNative
{
    private const string DllName = "hidapi.dll";

    [StructLayout(LayoutKind.Sequential)]
    internal struct HidDeviceInfoNative
    {
        public IntPtr path;
        public ushort vendor_id;
        public ushort product_id;
        public IntPtr serial_number;
        public ushort release_number;
        public IntPtr manufacturer_string;
        public IntPtr product_string;
        public ushort usage_page;
        public ushort usage;
        public int interface_number;
        public IntPtr next;
    }

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_init")]
    internal static extern int Init();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_exit")]
    internal static extern int Exit();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_enumerate")]
    internal static extern IntPtr Enumerate(ushort vendorId, ushort productId);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_free_enumeration")]
    internal static extern void FreeEnumeration(IntPtr devs);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_open_path", CharSet = CharSet.Ansi)]
    internal static extern IntPtr OpenPath(string path);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_close")]
    internal static extern void Close(IntPtr device);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "hid_read_timeout")]
    internal static extern int ReadTimeout(IntPtr device, [Out] byte[] data, UIntPtr length, int milliseconds);
}
