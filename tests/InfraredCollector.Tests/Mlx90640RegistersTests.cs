using InfraredCollector.Core.Mlx;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class Mlx90640RegistersTests
{
    [Fact]
    public void WithRefreshRate_SetsOnlyRefreshBits()
    {
        const ushort original = 0b1010_1100_0110_0101;
        var updated = Mlx90640Registers.WithRefreshRate(original, Mlx90640Constants.RefreshRate8Hz);

        Assert.Equal(4, Mlx90640Registers.RefreshRate(updated));
        Assert.Equal(original & 0b1111_1100_0111_1111, updated & 0b1111_1100_0111_1111);
    }

    [Theory]
    [InlineData(0.5, 0)]
    [InlineData(1, 1)]
    [InlineData(2, 2)]
    [InlineData(4, 3)]
    [InlineData(8, 4)]
    [InlineData(16, 5)]
    [InlineData(32, 6)]
    [InlineData(64, 7)]
    public void RefreshRateBitsFromHz_MapsSupportedRates(double refreshRateHz, byte expectedBits)
    {
        Assert.Equal(expectedBits, Mlx90640Constants.RefreshRateBitsFromHz(refreshRateHz));
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
