using InfraredCollector.Core.Mlx;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class Mlx90640RegistersTests
{
    [Fact]
    public void WithRefreshRate_SetsOnlyRefreshBits()
    {
        const ushort original = 0b1010_1100_0110_0101;
        var updated = Mlx90640Registers.WithRefreshRate(original, Mlx90640Constants.RefreshRate32Hz);

        Assert.Equal(6, Mlx90640Registers.RefreshRate(updated));
        Assert.Equal(original & 0b1111_1100_0111_1111, updated & 0b1111_1100_0111_1111);
    }

    [Theory]
    [InlineData(0x0008, true)]
    [InlineData(0x0000, false)]
    [InlineData(0x0009, true)]
    public void IsDataReady_ChecksStatusBit3(ushort status, bool expected)
    {
        Assert.Equal(expected, Mlx90640Registers.IsDataReady(status));
    }
}
