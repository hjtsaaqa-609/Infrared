using InfraredCollector.Core.Configuration;
using InfraredCollector.Core.Mlx;

namespace InfraredCollector.Core.Capture;

public sealed class Mlx90640AcquisitionWorker
{
    private readonly AppConfig _config;
    private readonly CaptureSessionWriter _writer;
    private readonly MlxChannelConfig _channel;
    private float[] _compositeTo = Enumerable.Repeat(float.NaN, Mlx90640Constants.PixelWords).ToArray();
    private int _seenSubpageMask;
    private int _lastSubpage = -1;
    private int _lastRawSubpage = -1;

    public Mlx90640AcquisitionWorker(AppConfig config, CaptureSessionWriter writer, MlxChannelConfig channel)
    {
        _config = config;
        _writer = writer;
        _channel = channel;
    }

    public event EventHandler<MlxSubpageRecord>? SubpageCaptured;
    public event EventHandler<MlxFrameEvent>? FrameComputed;
    public event EventHandler<string>? Status;
    public event EventHandler<Exception>? Failed;

    public Task RunAsync(CancellationToken cancellationToken)
    {
        return Task.Run(() => Run(cancellationToken), cancellationToken);
    }

    private void Run(CancellationToken cancellationToken)
    {
        try {
            using var device = new Usb2UartMlx90640Device(_channel.UsbIndex, _config);
            device.OpenAndConfigure();
            if (!string.IsNullOrWhiteSpace(_channel.ExpectedBoardUid) &&
                !string.Equals(_channel.ExpectedBoardUid, device.BoardUid, StringComparison.OrdinalIgnoreCase)) {
                throw new InvalidOperationException($"{_channel.Name}: expected USB2UART UID {_channel.ExpectedBoardUid}, got {device.BoardUid}.");
            }
            Status?.Invoke(this, $"{_channel.Name}: USB{_channel.UsbIndex} opened, UID={device.BoardUid}");

            var eeprom = device.ReadEeprom();
            _writer.WriteMlxEeprom(_channel.Name, eeprom);
            var mode = device.ConfigureOperatingMode();
            Status?.Invoke(
                this,
                $"{_channel.Name}: EEPROM read and operating mode configured " +
                $"(chess 0x{mode.Chess.Before:X4}->0x{mode.Chess.After:X4}, " +
                $"18bit 0x{mode.Resolution.Before:X4}->0x{mode.Resolution.After:X4}, " +
                $"{mode.RefreshRateHz:g}Hz 0x{mode.Refresh.Before:X4}->0x{mode.Refresh.After:X4} bits={mode.RefreshRateBits}, " +
                $"lowbits 0x{mode.LowBits.Before:X4}->0x{mode.LowBits.After:X4})");

            using var calculator = new Mlx90640Calculator();
            var canCalculate = false;
            if (calculator.IsAvailable) {
                var rc = calculator.ExtractParameters(eeprom);
                if (rc != 0) {
                    Status?.Invoke(this, $"{_channel.Name}: parameter extraction returned {rc}; raw frames will still be saved");
                }
                else {
                    canCalculate = true;
                }
            }
            else {
                Status?.Invoke(this, $"{_channel.Name}: native MLX calculator unavailable: {calculator.UnavailableReason}");
            }

            while (!cancellationToken.IsCancellationRequested) {
                var raw = device.ReadSubpage(cancellationToken);
                var rawSubpage = raw.FrameData[833] & 1;
                var subpage = rawSubpage;
                var subpageSource = "status-bit";
                var frameData = raw.FrameData;
                if (_lastSubpage >= 0 &&
                    rawSubpage == _lastRawSubpage &&
                    !Mlx90640Registers.IsDataReady(raw.StatusAfterClear)) {
                    subpage = 1 - _lastSubpage;
                    subpageSource = "synthetic-toggle";
                    frameData = (ushort[])raw.FrameData.Clone();
                    frameData[833] = (ushort)subpage;
                }
                _lastRawSubpage = rawSubpage;

                var subpageRecord = _writer.WriteMlxSubpage(
                    raw.TimestampUtc,
                    _channel.Name,
                    _channel.UsbIndex,
                    device.BoardUid,
                    subpage,
                    raw.StatusRegister,
                    raw.ControlRegister,
                    raw.DataReadyPolls,
                    raw.StatusAfterClear,
                    raw.StatusClearMethod,
                    subpageSource,
                    frameData);
                SubpageCaptured?.Invoke(this, subpageRecord);

                if (canCalculate) {
                    var calc = calculator.Calculate(frameData, _compositeTo, _config.Emissivity);
                    _seenSubpageMask |= 1 << (subpage & 1);
                    if (_seenSubpageMask == 0b11 && subpage != _lastSubpage) {
                        var snapshot = (float[])_compositeTo.Clone();
                        var summary = _writer.WriteMlxFrame(raw.TimestampUtc, _channel.Name, _channel.UsbIndex, device.BoardUid, calc.SubPage, calc.AmbientTemperature, snapshot);
                        FrameComputed?.Invoke(this, new MlxFrameEvent(summary, snapshot));
                    }
                }

                _lastSubpage = subpage;
            }
        }
        catch (OperationCanceledException) {
            Status?.Invoke(this, $"{_channel.Name}: stopped");
        }
        catch (Exception ex) {
            Failed?.Invoke(this, ex);
        }
    }
}

public sealed record MlxFrameEvent(MlxFrameSummary Summary, float[] Temperature);
