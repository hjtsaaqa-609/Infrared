using System.Globalization;

namespace InfraredCollector.Core.Util;

public static class Csv
{
    public static string Row(params object?[] values)
    {
        return string.Join(",", values.Select(Cell));
    }

    public static string Cell(object? value)
    {
        if (value is null) {
            return "";
        }

        var text = value switch
        {
            IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
            _ => value.ToString() ?? ""
        };

        if (text.Contains('"') || text.Contains(',') || text.Contains('\n') || text.Contains('\r')) {
            return "\"" + text.Replace("\"", "\"\"") + "\"";
        }

        return text;
    }
}
