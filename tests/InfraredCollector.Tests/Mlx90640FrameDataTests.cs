using InfraredCollector.Core.Mlx;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class Mlx90640FrameDataTests
{
    [Fact]
    public void Compose_BuildsMelexisFrameDataLayout()
    {
        var pixels = Enumerable.Range(0, Mlx90640Constants.PixelWords).Select(i => (ushort)i).ToArray();
        var aux = Enumerable.Range(0, Mlx90640Constants.AuxWords).Select(i => (ushort)(1000 + i)).ToArray();
        var frame = Mlx90640FrameData.Compose(pixels, aux, 0x1234, 0x0009);

        Assert.Equal(Mlx90640Constants.FrameDataWords, frame.Length);
        Assert.Equal((ushort)0, frame[0]);
        Assert.Equal((ushort)767, frame[767]);
        Assert.Equal((ushort)1000, frame[768]);
        Assert.Equal((ushort)1063, frame[831]);
        Assert.Equal((ushort)0x1234, frame[832]);
        Assert.Equal((ushort)1, frame[833]);
    }
}
