namespace InfraredCollector.Core.Util;

public static class East8Clock
{
    public static readonly TimeSpan Offset = TimeSpan.FromHours(8);

    public static DateTimeOffset Now() => DateTimeOffset.UtcNow.ToOffset(Offset);

    public static DateTimeOffset ToEast8(DateTimeOffset timestamp) => timestamp.ToOffset(Offset);

    public static string Format(DateTimeOffset timestamp) => ToEast8(timestamp).ToString("O");
}
