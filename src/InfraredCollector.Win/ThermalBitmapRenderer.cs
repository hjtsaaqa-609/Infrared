using System.Drawing.Imaging;
using System.Drawing;

namespace InfraredCollector.Win;

internal static class ThermalBitmapRenderer
{
    public static Bitmap Render(float[] values, int width = 32, int height = 24, int scale = 12)
    {
        var bmp = new Bitmap(width * scale, height * scale, PixelFormat.Format24bppRgb);
        var valid = values.Where(v => !float.IsNaN(v) && !float.IsInfinity(v)).ToArray();
        var min = valid.Length == 0 ? 0 : valid.Min();
        var max = valid.Length == 0 ? 1 : valid.Max();
        var span = Math.Max(0.001f, max - min);

        using var g = Graphics.FromImage(bmp);
        g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
        for (var y = 0; y < height; y++) {
            for (var x = 0; x < width; x++) {
                var value = values[y * width + x];
                var color = float.IsNaN(value) ? Color.FromArgb(24, 24, 24) : Palette((value - min) / span);
                using var brush = new SolidBrush(color);
                g.FillRectangle(brush, x * scale, y * scale, scale, scale);
            }
        }

        return bmp;
    }

    private static Color Palette(float x)
    {
        x = Math.Clamp(x, 0, 1);
        var r = Math.Clamp(1.5f - Math.Abs(4f * x - 3f), 0f, 1f);
        var g = Math.Clamp(1.5f - Math.Abs(4f * x - 2f), 0f, 1f);
        var b = Math.Clamp(1.5f - Math.Abs(4f * x - 1f), 0f, 1f);
        return Color.FromArgb((int)(r * 255), (int)(g * 255), (int)(b * 255));
    }
}
