using InfraredCollector.Core.Capture;
using InfraredCollector.Core.Configuration;
using InfraredCollector.Core.Devices;
using System.Drawing;
using System.IO.Ports;
using System.Windows.Forms;

namespace InfraredCollector.Win;

public sealed class MainForm : Form
{
    private readonly AppConfig _config;
    private readonly BindingSource _boardsSource = new();
    private readonly DataGridView _boardsGrid = new();
    private readonly NumericUpDown _leftUsb = new();
    private readonly NumericUpDown _rightUsb = new();
    private readonly ComboBox _tasiPort = new();
    private readonly NumericUpDown _tasiBaud = new();
    private readonly NumericUpDown _tasiPollMs = new();
    private readonly TextBox _captureRoot = new();
    private readonly TextBox _status = new();
    private readonly Label _leftSummary = new();
    private readonly Label _rightSummary = new();
    private readonly Label _sessionLabel = new();
    private readonly PictureBox _leftHeatmap = new();
    private readonly PictureBox _rightHeatmap = new();
    private readonly Button _scanBoardsButton = new();
    private readonly Button _scanSerialButton = new();
    private readonly Button _startButton = new();
    private readonly Button _stopButton = new();

    private CaptureSessionWriter? _writer;
    private CancellationTokenSource? _cts;
    private readonly List<Task> _runningTasks = [];
    private readonly Dictionary<string, int> _subpageLogCounts = new(StringComparer.OrdinalIgnoreCase);

    public MainForm()
    {
        Text = "InfraredCollector - Dual MLX90640";
        Width = 1280;
        Height = 820;
        MinimumSize = new Size(1100, 720);

        _config = AppConfig.LoadOrDefault(Path.Combine(AppContext.BaseDirectory, "appsettings.json"));
        BuildUi();
        ScanSerialPorts();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        StopCapture();
        base.OnFormClosing(e);
    }

    private void BuildUi()
    {
        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 3,
            Padding = new Padding(12),
        };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 420));
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 300));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 160));
        Controls.Add(root);

        var devicePanel = BuildDevicePanel();
        var heatmapPanel = BuildHeatmapPanel();
        var statusPanel = BuildStatusPanel();
        root.Controls.Add(devicePanel, 0, 0);
        root.SetRowSpan(devicePanel, 2);
        root.Controls.Add(heatmapPanel, 1, 0);
        root.Controls.Add(statusPanel, 1, 2);
    }

    private Control BuildDevicePanel()
    {
        var panel = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 12, ColumnCount = 2 };
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));

        _scanBoardsButton.Text = "扫描 USB2UART";
        _scanBoardsButton.Dock = DockStyle.Fill;
        _scanBoardsButton.MinimumSize = new Size(160, 28);
        _scanBoardsButton.Click += (_, _) => ScanBoards();
        _scanSerialButton.Text = "扫描 TA612 串口";
        _scanSerialButton.Dock = DockStyle.Fill;
        _scanSerialButton.MinimumSize = new Size(160, 28);
        _scanSerialButton.Click += (_, _) => ScanSerialPorts();
        panel.Controls.Add(_scanBoardsButton, 0, 0);
        panel.Controls.Add(_scanSerialButton, 1, 0);

        _boardsGrid.Dock = DockStyle.Fill;
        _boardsGrid.ReadOnly = true;
        _boardsGrid.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
        _boardsGrid.AutoGenerateColumns = true;
        _boardsGrid.DataSource = _boardsSource;
        panel.Controls.Add(_boardsGrid, 0, 1);
        panel.SetColumnSpan(_boardsGrid, 2);
        panel.SetRowSpan(_boardsGrid, 3);

        panel.Controls.Add(new Label { Text = "Left USB Index", AutoSize = true }, 0, 4);
        panel.Controls.Add(new Label { Text = "Right USB Index", AutoSize = true }, 1, 4);
        ConfigureNumeric(_leftUsb, 0);
        ConfigureNumeric(_rightUsb, 1);
        panel.Controls.Add(_leftUsb, 0, 5);
        panel.Controls.Add(_rightUsb, 1, 5);

        panel.Controls.Add(new Label { Text = "TA612 COM Port", AutoSize = true }, 0, 6);
        _tasiPort.Dock = DockStyle.Fill;
        _tasiPort.DropDownStyle = ComboBoxStyle.DropDown;
        panel.Controls.Add(_tasiPort, 1, 6);

        panel.Controls.Add(new Label { Text = "TA612 Baud", AutoSize = true }, 0, 7);
        ConfigureNumeric(_tasiBaud, _config.TasiBaudRate, 1200, 921600);
        panel.Controls.Add(_tasiBaud, 1, 7);

        panel.Controls.Add(new Label { Text = "TA612 Poll ms", AutoSize = true }, 0, 8);
        ConfigureNumeric(_tasiPollMs, Math.Max(100, (int)Math.Round(_config.TasiPollIntervalSeconds * 1000)), 100, 60000);
        panel.Controls.Add(_tasiPollMs, 1, 8);

        _captureRoot.Text = Path.GetFullPath(_config.CaptureRoot);
        _captureRoot.Dock = DockStyle.Fill;
        panel.Controls.Add(new Label { Text = "Capture Root", AutoSize = true }, 0, 10);
        panel.Controls.Add(_captureRoot, 1, 10);

        _startButton.Text = "Start";
        _startButton.Height = 36;
        _startButton.Click += (_, _) => StartCapture();
        _stopButton.Text = "Stop";
        _stopButton.Height = 36;
        _stopButton.Enabled = false;
        _stopButton.Click += (_, _) => StopCapture();
        panel.Controls.Add(_startButton, 0, 11);
        panel.Controls.Add(_stopButton, 1, 11);

        for (var i = 0; i < panel.RowCount; i++) {
            panel.RowStyles.Add(i is 1 or 6 ? new RowStyle(SizeType.Percent, 50) : new RowStyle(SizeType.AutoSize));
        }

        return panel;
    }

    private Control BuildHeatmapPanel()
    {
        var panel = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 3 };
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        panel.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        panel.RowStyles.Add(new RowStyle(SizeType.Absolute, 56));

        panel.Controls.Add(new Label { Text = "Left MLX90640", Dock = DockStyle.Fill, Font = new Font(Font, FontStyle.Bold) }, 0, 0);
        panel.Controls.Add(new Label { Text = "Right MLX90640", Dock = DockStyle.Fill, Font = new Font(Font, FontStyle.Bold) }, 1, 0);
        ConfigurePicture(_leftHeatmap);
        ConfigurePicture(_rightHeatmap);
        panel.Controls.Add(_leftHeatmap, 0, 1);
        panel.Controls.Add(_rightHeatmap, 1, 1);
        panel.Controls.Add(_leftSummary, 0, 2);
        panel.Controls.Add(_rightSummary, 1, 2);
        _leftSummary.Dock = DockStyle.Fill;
        _rightSummary.Dock = DockStyle.Fill;
        _leftSummary.Text = "Waiting";
        _rightSummary.Text = "Waiting";
        return panel;
    }

    private Control BuildStatusPanel()
    {
        var panel = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2 };
        panel.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        _sessionLabel.Text = "No active session";
        _sessionLabel.Dock = DockStyle.Fill;
        _status.Multiline = true;
        _status.Dock = DockStyle.Fill;
        _status.ScrollBars = ScrollBars.Vertical;
        _status.ReadOnly = true;
        panel.Controls.Add(_sessionLabel, 0, 0);
        panel.Controls.Add(_status, 0, 1);
        return panel;
    }

    private void ScanBoards()
    {
        Task.Run(() => {
            try {
                var devices = new Usb2UartDeviceManager().Scan();
                Ui(() => {
                    _boardsSource.DataSource = devices;
                    AppendStatus($"Found {devices.Count} USB2UARTPSIIIC board(s).");
                });
            }
            catch (Exception ex) {
                Ui(() => AppendStatus("Scan Boards failed: " + ex.Message));
            }
        });
    }

    private void ScanSerialPorts()
    {
        var current = _tasiPort.Text;
        var ports = SerialPort.GetPortNames().OrderBy(p => p, StringComparer.OrdinalIgnoreCase).ToArray();
        _tasiPort.Items.Clear();
        _tasiPort.Items.AddRange(ports);
        if (!string.IsNullOrWhiteSpace(_config.TasiSerialPort) && ports.Contains(_config.TasiSerialPort, StringComparer.OrdinalIgnoreCase)) {
            _tasiPort.SelectedItem = _config.TasiSerialPort;
        }
        else if (!string.IsNullOrWhiteSpace(current) && ports.Contains(current, StringComparer.OrdinalIgnoreCase)) {
            _tasiPort.SelectedItem = current;
        }
        else if (ports.Length > 0) {
            _tasiPort.SelectedIndex = 0;
        }
        AppendStatus($"Found {ports.Length} serial port(s).");
    }

    private void StartCapture()
    {
        if (_cts is not null) {
            return;
        }

        try {
            _config.CaptureRoot = _captureRoot.Text;
            _config.TasiSerialPort = _tasiPort.Text.Trim();
            _config.TasiBaudRate = (int)_tasiBaud.Value;
            _config.TasiPollIntervalSeconds = (double)_tasiPollMs.Value / 1000.0;
            Directory.CreateDirectory(_config.CaptureRoot);

            _cts = new CancellationTokenSource();
            _writer = new CaptureSessionWriter(_config.CaptureRoot, _config, new
            {
                leftUsbIndex = (uint)_leftUsb.Value,
                rightUsbIndex = (uint)_rightUsb.Value,
                tasiSerialPort = _config.TasiSerialPort,
                tasiBaudRate = _config.TasiBaudRate,
                tasiPollIntervalSeconds = _config.TasiPollIntervalSeconds
            });

            _sessionLabel.Text = _writer.SessionDirectory;
            _subpageLogCounts.Clear();
            _startButton.Enabled = false;
            _stopButton.Enabled = true;
            AppendStatus("Capture started.");

            StartMlxWorker(new MlxChannelConfig("left", (uint)_leftUsb.Value));
            StartMlxWorker(new MlxChannelConfig("right", (uint)_rightUsb.Value));

            if (!string.IsNullOrWhiteSpace(_config.TasiSerialPort)) {
                var recorder = new TasiSerialRecorder(
                    _writer,
                    _config.TasiSerialPort,
                    _config.TasiBaudRate,
                    TimeSpan.FromSeconds(_config.TasiPollIntervalSeconds),
                    _config.TasiSerialReadSize);
                recorder.Status += (_, msg) => Ui(() => AppendStatus(msg));
                recorder.Failed += (_, ex) => Ui(() => AppendStatus("TA612 serial failed: " + ex.Message));
                recorder.FrameCaptured += (_, record) => Ui(() => {
                    var channels = record.ChannelsC is null ? "no temperature payload" : string.Join(" ", record.ChannelsC.Select(v => $"{v:F1}C"));
                    AppendStatus($"TA612 frame {channels} @ {record.RawOffsetBytes}");
                });
                _runningTasks.Add(recorder.RunAsync(_cts.Token));
            }
            else {
                AppendStatus("No TA612 COM port selected; TA612 capture disabled.");
            }
        }
        catch (Exception ex) {
            AppendStatus("Start failed: " + ex.Message);
            StopCapture();
        }
    }

    private void StopCapture()
    {
        if (_cts is null) {
            return;
        }

        _cts.Cancel();
        try {
            Task.WaitAll(_runningTasks.ToArray(), TimeSpan.FromSeconds(2));
        }
        catch {
            // Workers report failures through the UI log.
        }

        _runningTasks.Clear();
        _cts.Dispose();
        _cts = null;
        _writer?.Dispose();
        _writer = null;
        _startButton.Enabled = true;
        _stopButton.Enabled = false;
        AppendStatus("Capture stopped.");
    }

    private void StartMlxWorker(MlxChannelConfig channel)
    {
        if (_writer is null || _cts is null) {
            return;
        }

        var worker = new Mlx90640AcquisitionWorker(_config, _writer, channel);
        worker.Status += (_, msg) => Ui(() => AppendStatus(msg));
        worker.Failed += (_, ex) => Ui(() => AppendStatus($"{channel.Name} failed: {ex.Message}"));
        worker.SubpageCaptured += (_, record) => Ui(() => AppendSubpageStatus(record));
        worker.FrameComputed += (_, frame) => Ui(() => UpdateFrame(frame));
        _runningTasks.Add(worker.RunAsync(_cts.Token));
    }

    private void AppendSubpageStatus(MlxSubpageRecord record)
    {
        _subpageLogCounts.TryGetValue(record.Channel, out var count);
        count++;
        _subpageLogCounts[record.Channel] = count;
        if (count <= 4 || count % 32 == 0) {
            AppendStatus(
                $"{record.Channel}: subpage {record.SubPage} status=0x{record.StatusRegister:X4} " +
                $"clear=0x{record.StatusAfterClear:X4}/{record.StatusClearMethod} " +
                $"source={record.SubPageSource} ctrl=0x{record.ControlRegister:X4} polls={record.DataReadyPolls} raw offset {record.FrameDataOffsetBytes}");
        }
    }

    private void UpdateFrame(MlxFrameEvent frame)
    {
        var summary = frame.Summary;
        var target = summary.Channel.Equals("left", StringComparison.OrdinalIgnoreCase) ? _leftHeatmap : _rightHeatmap;
        var label = summary.Channel.Equals("left", StringComparison.OrdinalIgnoreCase) ? _leftSummary : _rightSummary;
        var old = target.Image;
        target.Image = ThermalBitmapRenderer.Render(frame.Temperature);
        old?.Dispose();
        label.Text = $"Ta {summary.AmbientTemperature:F2} C   min {summary.Min:F2}   avg {summary.Average:F2}   max {summary.Max:F2}   center {summary.Center:F2}";
    }

    private void AppendStatus(string message)
    {
        _status.AppendText($"[{DateTime.Now:HH:mm:ss}] {message}{Environment.NewLine}");
    }

    private void Ui(Action action)
    {
        if (IsDisposed) {
            return;
        }
        if (InvokeRequired) {
            BeginInvoke(action);
        }
        else {
            action();
        }
    }

    private static void ConfigureNumeric(NumericUpDown numeric, int value, int min = 0, int max = 99)
    {
        numeric.Minimum = min;
        numeric.Maximum = max;
        numeric.Value = value;
        numeric.Dock = DockStyle.Fill;
    }

    private static void ConfigurePicture(PictureBox picture)
    {
        picture.Dock = DockStyle.Fill;
        picture.BackColor = Color.FromArgb(22, 24, 28);
        picture.SizeMode = PictureBoxSizeMode.Zoom;
        picture.BorderStyle = BorderStyle.FixedSingle;
    }
}
