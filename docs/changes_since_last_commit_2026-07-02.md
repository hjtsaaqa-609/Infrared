# 上次提交以来改动说明

本文档整理从上次 Git 提交 `2126465 完善红外采集 Web 统计页面` 到 2026-07-02 的本地代码改动。

## 一、总体目标

本轮改动围绕两条采集链路展开：

1. 红外/温度低延迟采集链路：继续完善 Mac 端红外采集、触发采集、位置分段采集、稳定性测试和 Web 报告查看能力。
2. ZS-8K 热电偶温度链路：新增从 DTU MQTT 数据订阅、分 DTU 保存 CSV、局域网 Web 看板、双 DTU 对比、CSV 下载等功能。

## 二、ZS-8K / MQTT 温度采集

新增目录：

```text
mac_mqtt_zs8k_client/
```

主要文件：

```text
mac_mqtt_zs8k_client/mqtt_zs8k_client_mac.py
mac_mqtt_zs8k_client/README_MAC.md
```

主要能力：

- 订阅 DTU 上传到 MQTT 的 `testup/+` 数据。
- 自动解析 ZS-8K JSON 温度 payload。
- 保留总表输出：

```text
zs8k_mqtt.csv
zs8k_mqtt.jsonl
```

- 按 DTU IMEI 分文件保存 10 秒采样 CSV：

```text
csv/863434087141161.csv
csv/863434087141369.csv
```

- CSV 字段包含北京时间、UTC 时间、设备号、topic 和 8 路温度。
- 支持通过参数调整分设备 CSV 的采样间隔：

```bash
python3 mqtt_zs8k_client_mac.py --sample-interval 10
```

## 三、ZS-8K CSV Dashboard

新增文件：

```text
tools/zs8k_csv_dashboard_server.py
```

主要能力：

- 提供本机和局域网可访问的 Web 看板。
- 默认端口可使用 `8765`，也可以另开一份服务在 `8766`。
- 自动读取：

```text
mac_mqtt_zs8k_client/csv/*.csv
mac_mqtt_zs8k_client/zs8k_mqtt.csv
```

- 支持两台 DTU 自动识别与切换。
- 支持顶部 CSV 文件选择。
- 支持折线图展示 8 路通道温度。
- 支持通道名称：

```text
ch1-环境温度
ch2-左内
ch3-上内
ch4-上外
ch5-左外
ch6-右外
ch7-前外
ch8-后外
```

- 支持开始采集、停止采集按钮，由网页控制本机 MQTT 采集脚本。
- 支持读取上限 300000 行，覆盖约 31 天的 10 秒采样数据。
- 支持“最近 31 天”时间范围。
- 支持 CSV 下载：

```text
/download/863434087141161.csv
/download/863434087141369.csv
/download/all.zip
```

- 支持双 DTU 对比界面：
  - 顶部“界面”可在“单DTU”和“双DTU对比”之间切换。
  - 可分别选择“有涂层”和“无涂层”对应的 DTU。
  - 同一坐标系中绘制两台 DTU 的温度趋势。
  - 实线表示有涂层，虚线表示无涂层。
  - 通道统计表增加对比列，例如：

```text
最新(有涂层)
最新(无涂层)
最新差
平均(有涂层)
平均(无涂层)
平均差
```

当前常用访问地址：

```text
http://127.0.0.1:8765/
http://127.0.0.1:8766/
http://10.5.70.229:8765/
http://10.5.70.229:8766/
```

## 四、Windows 端 ZS-8K 调试工具

新增文件：

```text
tools/windows_zs8k_modbus.py
tools/run_zs8k_windows.bat
requirements-windows.txt
docs/zs8k_windows_run_guide.md
```

主要能力：

- 通过 USB-RS485 在 Windows 上读取 ZS-8K。
- 支持列出串口、单次读取、连续读取、保存 CSV。
- 支持 Modbus RTU 功能码 `04`，读取输入寄存器。
- 支持调试输出 TX/RX 原始帧。
- 默认参数覆盖 ZS-8K 常见读取方式，并可通过命令行修改地址、波特率、通道数量、寄存器起始地址。

示例：

```powershell
py windows_zs8k_modbus.py poll --port COM6 --baud 9600 --address 1 --channels 8 --once --debug-wire
```

## 五、Mac 红外低延迟采集工具

新增或补充的工具文件：

```text
tools/macos_infrared_triggered_cl_low_delay.py
tools/macos_infrared_triggered_cli.py
tools/macos_infrared_auto_interval_low_delay.py
tools/macos_infrared_position_segmented_low_delay.py
tools/run_refresh_rate_stability_sweep.py
tools/low_delay_report_server.py
```

主要能力：

- 支持双 MLX90640 低延迟采集。
- 支持触发式采集和自动间隔采集。
- 支持直接寄存器读取与 DLL/native 计算方式。
- 支持温度帧、摘要 CSV、二进制温度帧文件保存。
- 支持按位置分段采集，适配 STM32/编码器位置输出。
- 支持刷新率稳定性 sweep，用于比较不同 MLX90640 刷新率下的采集稳定性。
- 新增低延迟报告 Web 服务，用于浏览采集 session、温度统计、帧数据和异常启动帧过滤。

## 六、STM32 位置采集辅助工具

新增文件：

```text
tools/stm32_position_target_capture.py
tools/stm32_continuous_position_capture.py
```

主要能力：

- 解析 STM32 输出的位置、目标位置、实际位置、状态和帧数据。
- 支持目标点采集和连续流式采集。
- 可与 Mac 红外采集工具结合，用于机器位置分段温度分析。

## 七、Git 忽略规则调整

更新 `.gitignore`，避免上传以下本地运行数据和构建产物：

```text
dist/
packages/
backups/
mac_mqtt_zs8k_client/.venv/
mac_mqtt_zs8k_client/csv/
mac_mqtt_zs8k_client/*.csv
mac_mqtt_zs8k_client/*.jsonl
mac_mqtt_zs8k_client/*.log
mac_mqtt_zs8k_client/.mqtt_zs8k_client.pid
```

原因：

- CSV、JSONL、日志是现场运行数据，会持续增长。
- `.venv` 是本机虚拟环境，不适合提交。
- `dist/`、`packages/`、`backups/` 是构建包、驱动包或备份目录，不属于源码提交范围。

## 八、本次计划提交到 GitHub 的内容

计划提交的核心文件包括：

```text
.gitignore
docs/changes_since_last_commit_2026-07-02.md
docs/zs8k_windows_run_guide.md
mac_mqtt_zs8k_client/README_MAC.md
mac_mqtt_zs8k_client/mqtt_zs8k_client_mac.py
requirements-windows.txt
tools/low_delay_report_server.py
tools/macos_infrared_auto_interval_low_delay.py
tools/macos_infrared_position_segmented_low_delay.py
tools/macos_infrared_triggered_cl_low_delay.py
tools/macos_infrared_triggered_cli.py
tools/run_refresh_rate_stability_sweep.py
tools/run_zs8k_windows.bat
tools/stm32_continuous_position_capture.py
tools/stm32_position_target_capture.py
tools/windows_zs8k_modbus.py
tools/zs8k_csv_dashboard_server.py
```

不计划提交的内容：

```text
mac_mqtt_zs8k_client/.venv/
mac_mqtt_zs8k_client/csv/
mac_mqtt_zs8k_client/zs8k_mqtt.csv
mac_mqtt_zs8k_client/zs8k_mqtt.jsonl
mac_mqtt_zs8k_client/mqtt_zs8k_client.log
dist/
packages/
backups/
```

## 九、验证情况

已做过的主要验证：

- ZS-8K MQTT 客户端可订阅 MQTT 并写入分 DTU CSV。
- Dashboard 可在本机和局域网端口访问。
- 单 DTU 趋势图、双 DTU 对比图、通道对比表可加载数据。
- 单个 CSV 与全部 CSV zip 下载接口可返回文件。
- Dashboard Python 代码通过 `python3 -m py_compile` 语法检查。
- Dashboard 内嵌 JavaScript 通过 `node --check` 语法检查。
