# ZS-8K Windows 读取说明

这个包用于在 Windows 电脑上通过 USB-RS485 转换器读取中盛 ZS-8K K 型热电偶采集模块。

## 1. 接线

先用电脑直接验证 ZS-8K：

```text
USB-RS485 A    -> ZS-8K A
USB-RS485 B    -> ZS-8K B
USB-RS485 GND  -> ZS-8K -
12V 电源 +     -> ZS-8K +
12V 电源 -     -> ZS-8K -
```

如果没有返回数据，优先尝试交换 A/B。

## 2. 安装依赖

在解压后的目录打开 PowerShell：

```powershell
py -m pip install -r requirements-windows.txt
```

如果 Windows 没有 `py` 命令，先安装 Python 3，并勾选 Add Python to PATH。

## 3. 查看串口

```powershell
py windows_zs8k_modbus.py list-ports
```

找到 USB-RS485 对应的 COM 口，例如 `COM5`。

## 4. 读取一次

```powershell
py windows_zs8k_modbus.py poll --port COM5 --address 1 --channels 8 --once --debug-wire
```

正常输出会包含：

```text
TX 01 04 00 00 00 08 f1 cc
RX ...
ch1=25.0C ch2=25.1C ...
```

看到 `RX` 和温度值，就说明读通了。

## 5. 连续读取并保存 CSV

```powershell
py windows_zs8k_modbus.py poll --port COM5 --address 1 --channels 8 --interval 0.2 --csv zs8k.csv
```

## 6. 常见排查

- 没有 `RX`：检查供电、COM 口、A/B 是否接反。
- 温度显示 `FAULT`：该通道热电偶未接入或线路开路。
- 默认参数不通：试 `--baud 9600`、`--baud 38400`、`--baud 115200`。
- 默认地址不通：试 `--address 2`、`--address 3`。
