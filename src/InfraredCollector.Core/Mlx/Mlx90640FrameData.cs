namespace InfraredCollector.Core.Mlx;

public static class Mlx90640FrameData
{
    public static ushort[] Compose(ushort[] pixels, ushort[] aux, ushort controlRegister, ushort statusRegister)
    {
        if (pixels.Length != Mlx90640Constants.PixelWords) {
            throw new ArgumentException("Expected 768 pixel words.", nameof(pixels));
        }
        if (aux.Length != Mlx90640Constants.AuxWords) {
            throw new ArgumentException("Expected 64 auxiliary words.", nameof(aux));
        }

        var frameData = new ushort[Mlx90640Constants.FrameDataWords];
        Array.Copy(pixels, 0, frameData, 0, pixels.Length);
        Array.Copy(aux, 0, frameData, Mlx90640Constants.PixelWords, aux.Length);
        frameData[832] = controlRegister;
        frameData[833] = (ushort)Mlx90640Registers.SubPageFromStatus(statusRegister);
        return frameData;
    }
}
