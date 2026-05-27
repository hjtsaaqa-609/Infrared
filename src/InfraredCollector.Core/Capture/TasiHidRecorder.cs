using InfraredCollector.Core.Devices;
using InfraredCollector.Core.Util;

namespace InfraredCollector.Core.Capture;

public sealed class TasiHidRecorder
{
    private readonly CaptureSessionWriter _writer;
    private readonly string _path;
    private readonly int _reportLength;

    public TasiHidRecorder(CaptureSessionWriter writer, string path, int reportLength)
    {
        _writer = writer;
        _path = path;
        _reportLength = Math.Clamp(reportLength, 8, 1024);
    }

    public event EventHandler<TasiRawRecord>? ReportCaptured;
    public event EventHandler<string>? Status;
    public event EventHandler<Exception>? Failed;

    public Task RunAsync(CancellationToken cancellationToken)
    {
        return Task.Run(() => Run(cancellationToken), cancellationToken);
    }

    private void Run(CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_path)) {
            Status?.Invoke(this, "TASI HID: no device selected; raw HID capture disabled");
            return;
        }

        IntPtr handle = IntPtr.Zero;
        try {
            HidApiNative.Init();
            handle = HidApiNative.OpenPath(_path);
            if (handle == IntPtr.Zero) {
                throw new InvalidOperationException("hid_open_path returned null.");
            }

            Status?.Invoke(this, "TASI HID: opened for raw report capture");
            var buffer = new byte[_reportLength];
            while (!cancellationToken.IsCancellationRequested) {
                Array.Clear(buffer);
                var read = HidApiNative.ReadTimeout(handle, buffer, (UIntPtr)buffer.Length, 250);
                if (read > 0) {
                    var record = _writer.WriteTasiRaw(East8Clock.Now(), buffer, read);
                    ReportCaptured?.Invoke(this, record);
                }
                else if (read < 0) {
                    throw new IOException("hid_read_timeout returned an error.");
                }
            }
        }
        catch (OperationCanceledException) {
            Status?.Invoke(this, "TASI HID: stopped");
        }
        catch (Exception ex) {
            Failed?.Invoke(this, ex);
        }
        finally {
            if (handle != IntPtr.Zero) {
                HidApiNative.Close(handle);
            }
        }
    }
}
