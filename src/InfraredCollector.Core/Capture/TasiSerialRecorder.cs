using System.IO.Ports;

namespace InfraredCollector.Core.Capture;

public sealed class TasiSerialRecorder
{
    private readonly CaptureSessionWriter _writer;
    private readonly string _portName;
    private readonly int _baudRate;
    private readonly TimeSpan _pollInterval;
    private readonly int _readSize;

    public TasiSerialRecorder(
        CaptureSessionWriter writer,
        string portName,
        int baudRate = 9600,
        TimeSpan? pollInterval = null,
        int readSize = 64)
    {
        _writer = writer;
        _portName = portName;
        _baudRate = baudRate;
        _pollInterval = pollInterval ?? TimeSpan.FromSeconds(1);
        _readSize = Math.Clamp(readSize, 8, 4096);
    }

    public event EventHandler<TasiSerialRecord>? FrameCaptured;
    public event EventHandler<string>? Status;
    public event EventHandler<Exception>? Failed;

    public Task RunAsync(CancellationToken cancellationToken)
    {
        return Task.Run(() => Run(cancellationToken), cancellationToken);
    }

    private void Run(CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_portName)) {
            Status?.Invoke(this, "TA612 serial: no COM port selected; capture disabled");
            return;
        }

        using var port = new SerialPort(_portName, _baudRate, Parity.None, 8, StopBits.One)
        {
            ReadTimeout = 200,
            WriteTimeout = 500
        };

        try {
            port.Open();
            port.DiscardInBuffer();
            port.DiscardOutBuffer();
            WriteCommand(port, TasiSerialProtocol.StopCommand);
            Thread.Sleep(100);
            port.DiscardInBuffer();
            WriteCommand(port, TasiSerialProtocol.StartRealtimeCommand);
            var lastCommand = DateTimeOffset.UtcNow;
            Status?.Invoke(this, $"TA612 serial: opened {_portName} @ {_baudRate}");

            var readBuffer = new byte[_readSize];
            var frameBuffer = new List<byte>(_readSize * 2);
            while (!cancellationToken.IsCancellationRequested) {
                if (DateTimeOffset.UtcNow - lastCommand >= _pollInterval) {
                    WriteCommand(port, TasiSerialProtocol.StartRealtimeCommand);
                    lastCommand = DateTimeOffset.UtcNow;
                }

                var read = 0;
                try {
                    read = port.Read(readBuffer, 0, readBuffer.Length);
                }
                catch (TimeoutException) {
                    continue;
                }

                if (read <= 0) {
                    continue;
                }
                for (var i = 0; i < read; i++) {
                    frameBuffer.Add(readBuffer[i]);
                }

                while (TasiSerialProtocol.TryReadFrame(frameBuffer, out var raw)) {
                    var parsed = TasiSerialProtocol.Parse(raw);
                    var record = _writer.WriteTasiSerialFrame(DateTimeOffset.UtcNow, raw, parsed);
                    FrameCaptured?.Invoke(this, record);
                }
            }
        }
        catch (OperationCanceledException) {
            Status?.Invoke(this, "TA612 serial: stopped");
        }
        catch (Exception ex) {
            Failed?.Invoke(this, ex);
        }
        finally {
            if (port.IsOpen) {
                try {
                    WriteCommand(port, TasiSerialProtocol.StopCommand);
                }
                catch {
                    // The port may already be gone.
                }
            }
        }
    }

    private static void WriteCommand(SerialPort port, byte[] command)
    {
        port.Write(command, 0, command.Length);
    }
}
