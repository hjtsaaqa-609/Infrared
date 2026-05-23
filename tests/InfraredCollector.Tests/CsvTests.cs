using InfraredCollector.Core.Util;
using Xunit;

namespace InfraredCollector.Tests;

public sealed class CsvTests
{
    [Fact]
    public void Row_EscapesQuotesAndCommas()
    {
        Assert.Equal("a,\"b,c\",\"d\"\"e\"", Csv.Row("a", "b,c", "d\"e"));
    }
}
