namespace InfraredCollector.Core.Mlx;

public static class Mlx90640Constants
{
    public const byte DefaultI2cAddress = 0x33;
    public const int EepromStartAddress = 0x2400;
    public const int EepromWords = 832;
    public const int PixelDataStartAddress = 0x0400;
    public const int PixelWords = 768;
    public const int AuxDataStartAddress = 0x0700;
    public const int AuxWords = 64;
    public const int StatusRegister = 0x8000;
    public const int ControlRegister = 0x800D;
    public const int FrameDataWords = 834;
    public const ushort DataReadyMask = 0x0008;
    public const ushort InitStatusValue = 0x0030;
    public const byte RefreshRate32Hz = 6;
    public const byte Resolution18Bit = 2;
    public const ushort ChessModeMask = 0x1000;
    public const ushort ControlLowBitsMask = 0x000F;
    public const ushort ControlLowBitsMacVerified = 0x0001;
    public const uint IicRate1M = 11;
}
