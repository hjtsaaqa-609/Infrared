namespace InfraredCollector.Core.Mlx;

public static class Mlx90640Registers
{
    private const ushort RefreshMask = 0b0000_0011_1000_0000;
    private const ushort ResolutionMask = 0b0000_1100_0000_0000;

    public static ushort WithRefreshRate(ushort controlRegister, byte refreshRate)
    {
        return (ushort)((controlRegister & ~RefreshMask) | ((refreshRate & 0x07) << 7));
    }

    public static int RefreshRate(ushort controlRegister)
    {
        return (controlRegister >> 7) & 0x07;
    }

    public static ushort WithResolution(ushort controlRegister, byte resolution)
    {
        return (ushort)((controlRegister & ~ResolutionMask) | ((resolution & 0x03) << 10));
    }

    public static int Resolution(ushort controlRegister)
    {
        return (controlRegister >> 10) & 0x03;
    }

    public static ushort WithChessMode(ushort controlRegister)
    {
        return (ushort)(controlRegister | Mlx90640Constants.ChessModeMask);
    }

    public static bool IsChessMode(ushort controlRegister)
    {
        return (controlRegister & Mlx90640Constants.ChessModeMask) != 0;
    }

    public static ushort WithMacVerifiedLowBits(ushort controlRegister)
    {
        return (ushort)((controlRegister & ~Mlx90640Constants.ControlLowBitsMask) | Mlx90640Constants.ControlLowBitsMacVerified);
    }

    public static int SubPageFromStatus(ushort statusRegister)
    {
        return statusRegister & 0x01;
    }

    public static bool IsDataReady(ushort statusRegister)
    {
        return (statusRegister & Mlx90640Constants.DataReadyMask) != 0;
    }
}
