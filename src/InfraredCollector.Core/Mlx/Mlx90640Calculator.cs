namespace InfraredCollector.Core.Mlx;

public sealed class Mlx90640Calculator : IDisposable
{
    private IntPtr _context;
    private bool _initialized;

    public bool IsAvailable { get; private set; }
    public string? UnavailableReason { get; private set; }

    public Mlx90640Calculator()
    {
        try {
            _context = Mlx90640NativeMethods.CreateContext();
            IsAvailable = _context != IntPtr.Zero;
            if (!IsAvailable) {
                UnavailableReason = "Mlx90640Native returned null context.";
            }
        }
        catch (Exception ex) when (ex is DllNotFoundException or EntryPointNotFoundException or BadImageFormatException) {
            IsAvailable = false;
            UnavailableReason = ex.Message;
        }
    }

    public int ExtractParameters(ushort[] eepromData)
    {
        EnsureAvailable();
        var rc = Mlx90640NativeMethods.ExtractParameters(_context, eepromData, eepromData.Length);
        _initialized = rc == 0;
        return rc;
    }

    public MlxCalculationResult Calculate(ushort[] frameData, float[] compositeTo, double emissivity)
    {
        EnsureAvailable();
        if (!_initialized) {
            throw new InvalidOperationException("MLX90640 parameters have not been extracted.");
        }

        var ta = Mlx90640NativeMethods.GetTa(_context, frameData, frameData.Length);
        var tr = ta - 8.0f;
        var subpage = Mlx90640NativeMethods.CalculateTo(_context, frameData, frameData.Length, (float)emissivity, tr, compositeTo);
        Mlx90640NativeMethods.BadPixelsCorrection(_context, frameData, frameData.Length, compositeTo);
        return new MlxCalculationResult(subpage, ta);
    }

    public void Dispose()
    {
        if (_context != IntPtr.Zero) {
            Mlx90640NativeMethods.DestroyContext(_context);
            _context = IntPtr.Zero;
        }
    }

    private void EnsureAvailable()
    {
        if (!IsAvailable || _context == IntPtr.Zero) {
            throw new InvalidOperationException($"Mlx90640Native.dll is unavailable: {UnavailableReason}");
        }
    }
}

public sealed record MlxCalculationResult(int SubPage, float AmbientTemperature);
