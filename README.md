# Infrared 采集工具说明

本仓库用于调试和采集红外温度数据。当前推荐流程是在 macOS 上使用 Python 命令行工具完成：

- 两路 `USB2UARTPSIIIC / USB2IIC` 分别连接一个 `MLX90640`
- 一个 `TA612C` 通过 USB 串口读取四路温度
- 三路数据写入同一个 session，并按时间戳生成合并摘要

Windows GUI 工程仍保留在仓库中，但当前硬件调试优先使用 macOS CLI。

## macOS 环境准备

创建 Python 虚拟环境并安装依赖：

```bash
python3 -m venv .venv-macos
source .venv-macos/bin/activate
python -m pip install -r requirements-macos.txt
```

编译 MLX90640 官方算法封装库：

```bash
./build-macos-native.sh
```

确认串口设备：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py list-ports
```

常见端口：

- `USB2UARTPSIIIC / USB2IIC`：通常是 `/dev/cu.usbmodem*`，VID/PID 为 `0483:5740`
- `TA612C`：通常是 `/dev/cu.usbserial*`，CH340 VID/PID 为 `1A86:7523`

## 单路 MLX90640 检查

先确认单个 MLX90640 能读到 EEPROM、控制寄存器和 data-ready 状态：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py check-mlx \
  --port /dev/cu.usbmodem212301
```

正常结果应包含：

- `EEPROM words: 832`
- `Control final` 中默认 `refresh=8Hz bits=4`
- `Status` 中 `data_ready=True`

短时间采集单路 MLX90640：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py capture-mlx \
  --port /dev/cu.usbmodem212301 \
  --refresh-rate-hz 8 \
  --duration 10 \
  --print-every 16
```

## TA612C 串口采集

TA612C 协议来自 `docs/TA系列通讯协议.docx`：

- 串口参数：`9600 8N1`
- 开始实时采集命令：`AA 55 01 03 03`
- 停止命令：`AA 55 00 03 02`
- 实时返回四路温度，单位解析为 `int16 / 10`

单独采集 TA612C：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py capture-tasi-serial \
  --port /dev/cu.usbserial-21240 \
  --duration 60 \
  --poll-interval 1
```

## 双路 MLX90640 + TA612C 同步采集

推荐使用新命令：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py capture-dual-combined \
  --left-mlx-port /dev/cu.usbmodem212301 \
  --right-mlx-port /dev/cu.usbmodemXXXXXX \
  --tasi-port /dev/cu.usbserial-21240 \
  --refresh-rate-hz 8 \
  --duration 60 \
  --tasi-poll-interval 1 \
  --print-every 32
```

如果不指定 `--left-mlx-port` 和 `--right-mlx-port`，程序会自动选择检测到的前两个 `USB2UARTPSIIIC` 设备。

默认配置：

- 两个 MLX90640 的 I2C 地址都是 `0x33`
- 两个 MLX90640 分别在独立 USB-I2C 总线上，所以地址可以相同
- I2C 速度为 `1M`
- MLX90640 刷新率默认 `8Hz`，可用 `--refresh-rate-hz 0.5|1|2|4|8|16|32|64` 修改
- ADC 分辨率为 `18-bit`
- 模式为 `chess`
- MLX 原始读取方式为 `--read-mode register`

如果只需要旧的单路 MLX + TA612C 合并采集，仍可使用：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py capture-combined \
  --mlx-port /dev/cu.usbmodem212301 \
  --tasi-port /dev/cu.usbserial-21240 \
  --duration 60
```

## 输出目录

每次采集都会在 `captures/` 下生成一个 session 目录，例如：

```text
captures/mac_dual_mlx_tasi_YYYYMMDD_HHMMSS/
```

所有 CSV 和 `session.json` 中记录的采集时间戳统一使用东八区时间，字段名使用 `timestamp_east8`、`mlx_timestamp_east8`、`tasi_timestamp_east8` 或 `createdEast8`。

MLX90640 配置的刷新率是半帧 subpage 的 data-ready 速率。采集程序会把每个原始 subpage 全部写入 `*_mlx_subpages.csv` 和 `raw/*_frameData.u16le`，并把每路 I2C 读请求/返回/失败计数写入 `raw/*_i2c_events.json`；`*_mlx_frames.csv`、`temp/*_to.f32le`、机器人对比用 `temp/*_infrared_thermal.bin`、`joined_summary.csv` 只在 subpage 0/1 都更新后写入严格完整帧。因此默认 `8Hz` 对应约 8 个 raw subpage/s、约 4 个完整温度矩阵/s。

如果用 `--duration 60 --refresh-rate-hz 8` 测试，理论上是接近 480 个 raw subpage，但实际数量应按 CSV 时间戳判断。程序结束时会打印 `subpage_active`、`subpage_hz`、`max_gap`、`long_gaps`；只要 `long_gaps=0`，通常表示没有出现明显丢帧。之前 60 秒数据中 left 为 473 个、right 为 472 个，按首尾时间戳计算实际 subpage 速率约 7.84 到 7.86Hz，最大间隔约 132ms，没有超过 1.5 倍周期的长间隔，因此更像是传感器/USB 实际节拍与命令行计时边界叠加造成的数量差异，而不是硬件错包。

双路同步采集的主要文件：

- `session.json`：本次采集的设备、端口、UID、采集参数和文件索引
- `joined_summary.csv`：按时间戳合并后的摘要，每行是一帧严格完整 MLX 温度矩阵关联最近一次 TA612 数据
- `tasi_serial_frames.csv`：TA612C 解析后的四路温度
- `raw/tasi_serial_frames.bin`：TA612C 原始串口帧，长度前缀格式

左路 MLX90640 文件：

- `left_mlx_subpages.csv`
- `left_mlx_frames.csv`
- `raw/left_eeprom.u16le`
- `raw/left_eeprom.csv`
- `raw/left_frameData.u16le`
- `raw/left_frameData.layout.json`
- `raw/left_i2c_events.json`
- `temp/left_to.f32le`
- `temp/left_to.layout.json`
- `temp/left_infrared_thermal.bin`
- `temp/left_infrared_thermal_latest.bin`
- `temp/left_infrared_thermal.layout.json`

右路 MLX90640 文件同理，文件名前缀为 `right_`。

## MLX90640 原始数据格式

完整原始数据保存在：

```text
raw/left_frameData.u16le
raw/right_frameData.u16le
```

每条记录为 `834` 个 little-endian `uint16`：

- `0..767`：像素区，来自寄存器 `0x0400`
- `768..831`：辅助区，来自寄存器 `0x0700`
- `832`：控制寄存器 `0x800D`
- `833`：subpage

每条原始记录大小：

```text
834 * 2 = 1668 bytes
```

CSV 中的 `frameData_offset_bytes` 可用于定位每条原始记录。

## 温度矩阵与机器人兼容 bin

完整温度矩阵保存在：

```text
temp/left_to.f32le
temp/right_to.f32le
```

每条记录为 `768` 个 little-endian `float32`，对应 `32x24`，row-major 顺序。

为了方便和机器人上的数据对比，额外生成 768 字节的兼容 bin：

```text
temp/left_infrared_thermal.bin
temp/right_infrared_thermal.bin
```

每条记录为 `768` 个 `uint8`，转换规则：

```text
raw_byte = clamp(floor(temp_C + 44 + 0.5), 0..255)
temp_C ~= raw_byte - 44
```

最新一帧也会单独保存：

```text
temp/left_infrared_thermal_latest.bin
temp/right_infrared_thermal_latest.bin
```

这两个 latest 文件都是固定 `768 bytes`，可以直接拿来和机器人导出的 `left_infrared_thermal.bin`、`right_infrared_thermal.bin` 做对比。

## Web 实时统计页面

采集和分析可以分成两个独立进程：前台继续运行采集命令写入 `captures/...` session 目录，另开一个终端启动 Web 统计页面按固定间隔读取当前 CSV 快照并重新计算。Web 服务不会启动采集，也不会写入采集目录。

启动 Web 页面：

```bash
python3 tools/capture_report_server.py \
  --session captures/mac_dual_mlx_tasi_20260526_113720 \
  --host 127.0.0.1 \
  --port 8765
```

如果需要让局域网内 `10.5.70.229` 访问，同时保留本机 `localhost` 访问，建议绑定所有网卡：

```bash
python3 tools/capture_report_server.py \
  --session captures/mac_dual_mlx_tasi_20260526_113720 \
  --host 0.0.0.0 \
  --port 8765
```

如果只想绑定指定网卡，服务端主机必须拥有这个 IP：

```bash
python3 tools/capture_report_server.py \
  --session captures/mac_dual_mlx_tasi_20260526_113720 \
  --host 10.5.70.229 \
  --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

页面顶部可以直接填写要分析的 session 路径，例如：

```text
captures/mac_dual_mlx_tasi_20260526_113720
```

页面支持：

- 指定采集数据路径，不需要重启 Web 服务
- 点击 `浏览` 在 `captures/` 下选择本地采集 session 文件夹；可用 `--browse-root` 修改可浏览根目录
- 选择 `采集质量`、`温区阈值`、`raw P95`、`raw P99` 或不过滤；连续温变趋势建议用 `采集质量`
- 选择 MLX/TA612 时间对齐窗口
- 设置 `2s`、`5s`、`10s` 自动刷新，用于观察正在采集中的实时统计
- 查看 TA612 1/2/3/4 路与左右 MLX90640 全帧 `min / avg / max` 连续趋势、过滤状态和全程统计；右侧图例勾选框可切换曲线显示

`采集质量` 不依赖温度数值，而是按 `*_mlx_subpages.csv` 判断 subpage 是否重复、时间间隔是否超过配置刷新周期的 1.5 倍，以及 status/control 是否异常；`温区阈值` 才按 `>200°C`、`<0°C` 或 NaN 摘要做过滤。

如果采集刚开始，某些 CSV 还未创建或还没有足够数据，页面会先显示错误；等待采集程序写入 `left_mlx_frames.csv`、`right_mlx_frames.csv`、`tasi_serial_frames.csv` 后刷新即可。

## 常用排查命令

持续发 I2C 时钟，方便用示波器看 SCL/SDA：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py clock-scl \
  --port /dev/cu.usbmodem212301 \
  --i2c-rate 100k \
  --clock-mode register
```

扫描 I2C 地址：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py scan-i2c \
  --port /dev/cu.usbmodem212301 \
  --i2c-rate 400k \
  --read-mode register
```

对比不同读取模式：

```bash
.venv-macos/bin/python tools/macos_infrared_cli.py probe-mlx \
  --port /dev/cu.usbmodem212301 \
  --i2c-rate 400k \
  --read-mode register \
  --debug-wire
```

## Windows GUI 说明

Windows x64 GUI 已按 macOS 验证过的流程调整为：

- 双路 `USB2UARTPSIIIC` + 双 `MLX90640`
- `TA612C` 通过串口 `COMx` 读取四路温度，不再把 HID raw 作为主路径
- `MLX Refresh Hz` 下拉框可选择 `0.5/1/2/4/8/16/32/64Hz`，默认 `8Hz`
- 输出文件名和二进制格式与 macOS 双路同步采集保持一致

构建要求：

- .NET 8 SDK x64，必须安装 SDK，不能只安装 Runtime
- Visual Studio Build Tools 2019 或 2022，安装 `Desktop development with C++`
- 已安装 USB2UARTPSIIIC 驱动

.NET SDK 安装和检查：

```powershell
winget install Microsoft.DotNet.SDK.8
dotnet --list-sdks
dotnet --info
```

如果机器上没有 `winget`，从 Microsoft 官网下载 `.NET 8 SDK` 的 Windows x64 安装包：

```text
https://dotnet.microsoft.com/download/dotnet/8.0
```

安装完成后需要重新打开 PowerShell，再执行构建命令。

构建命令：

```powershell
.\build-windows.ps1 -Configuration Release
```

`build-windows.ps1` 会先用 MSBuild 编译 Windows x64 的 `Mlx90640Native.dll`，再发布 WinForms 程序。
最终输出目录：

```text
src/InfraredCollector.Win/bin/Release/net8.0-windows/win-x64/publish/
```

Windows 输出目录需要包含：

- `USB2UARTSPIIICDLL.dll`
- `Mlx90640Native.dll`
- `System.IO.Ports.dll`
- `InfraredCollector.Win.exe`

GUI 使用步骤：

1. 点击 `Scan Boards`，确认能看到两个 USB2UARTPSIIIC。
2. 设置 Left USB Index 和 Right USB Index，通常是 `0` 和 `1`。
3. 点击 `Scan Serial`，选择 TA612C 对应的 `COMx`。
4. TA612C 默认 `9600` baud，轮询间隔默认 `1000 ms`。
5. 点击 `Start` 开始采集，点击 `Stop` 结束。

Windows session 输出目录形如：

```text
captures/win_dual_mlx_tasi_YYYYMMDD_HHMMSS/
```

主要输出文件：

- `left_mlx_subpages.csv`、`right_mlx_subpages.csv`：每个 MLX raw subpage，一条记录对应一个 834-word `frameData`
- `left_mlx_frames.csv`、`right_mlx_frames.csv`：严格完整帧，只有 subpage 0/1 都刷新后才写入
- `raw/left_frameData.u16le`、`raw/right_frameData.u16le`
- `temp/left_to.f32le`、`temp/right_to.f32le`
- `temp/left_infrared_thermal.bin`、`temp/right_infrared_thermal.bin`
- `temp/left_infrared_thermal_latest.bin`、`temp/right_infrared_thermal_latest.bin`
- `tasi_serial_frames.csv`
- `raw/tasi_serial_frames.bin`
- `joined_summary.csv`

USB2UART 驱动安装程序位于：

```text
vendor/usb2uart/usb2uart_driver_installer.exe
```
