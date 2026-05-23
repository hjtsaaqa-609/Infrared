using System.Runtime.InteropServices;

namespace InfraredCollector.Core.Devices;

internal static class Usb2UartNative
{
    private const string DllName = "USB2UARTSPIIICDLL.dll";

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "OpenUsb")]
    internal static extern int OpenUsb(uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "CloseUsb")]
    internal static extern int CloseUsb(uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "ConfigIICParam")]
    internal static extern int ConfigIICParam(uint rate, uint clkSLevel, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "IICCheckSlaveAddr")]
    internal static extern int IICCheckSlaveAddr(byte addrMod, uint addr, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "IICSendData")]
    internal static extern int IICSendData(byte startBit, byte stopBit, [In] byte[] sendBuf, uint len, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "IICRcvData")]
    internal static extern int IICRcvData(byte stopBit, [Out] byte[] rcvBuf, uint len, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "GetBoardInformation")]
    internal static extern int GetBoardInformation([Out] byte[] uidBuf, out uint isHsUsb, out uint fwVersion, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "IICRegisterSend")]
    internal static extern int IICRegisterSend(byte addrMod, uint addr, [In] byte[] regBuf, [In] byte[] sendBuf, byte regLen, uint sendLen, uint usbIndex);

    [DllImport(DllName, CallingConvention = CallingConvention.StdCall, EntryPoint = "IICRegisterRead")]
    internal static extern int IICRegisterRead(byte addrMod, uint addr, [In] byte[] regBuf, [Out] byte[] rcvBuf, byte regLen, uint rcvLen, uint usbIndex);
}
