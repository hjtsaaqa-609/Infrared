using System.Runtime.InteropServices;

namespace InfraredCollector.Core.Mlx;

internal static class Mlx90640NativeMethods
{
    private const string DllName = "Mlx90640Native.dll";

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxCreateContext")]
    internal static extern IntPtr CreateContext();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxDestroyContext")]
    internal static extern void DestroyContext(IntPtr context);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxExtractParameters")]
    internal static extern int ExtractParameters(IntPtr context, [In] ushort[] eepromData, int wordCount);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxGetTa")]
    internal static extern float GetTa(IntPtr context, [In] ushort[] frameData, int wordCount);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxCalculateTo")]
    internal static extern int CalculateTo(IntPtr context, [In] ushort[] frameData, int wordCount, float emissivity, float tr, [In, Out] float[] to768);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "MlxBadPixelsCorrection")]
    internal static extern void BadPixelsCorrection(IntPtr context, [In] ushort[] frameData, int wordCount, [In, Out] float[] to768);
}
