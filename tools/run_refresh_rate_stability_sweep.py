#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_REFRESH_RATES = (1, 2, 4, 8, 16, 32)
OUTPUT_RE = re.compile(r"output=([^\s,]+)")


def parse_rates(text: str) -> list[int]:
    if text == "supported":
        return list(SUPPORTED_REFRESH_RATES)
    if "-" in text and "," not in text:
        start_text, end_text = text.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        step = 1 if end >= start else -1
        return list(range(start, end + step, step))
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def validate_rates(rates: list[int], allow_unsupported: bool) -> list[int]:
    unsupported = [rate for rate in rates if rate not in SUPPORTED_REFRESH_RATES]
    if unsupported and not allow_unsupported:
        supported = ", ".join(str(rate) for rate in SUPPORTED_REFRESH_RATES)
        requested = ", ".join(str(rate) for rate in unsupported)
        raise SystemExit(
            "MLX90640 不能设置任意整数 Hz。"
            f"不支持的频率: {requested}。"
            f"当前 1-32Hz 内真实支持: {supported}。\n"
            "如果你想先强行尝试，请加 --allow-unsupported-rates；通常会被采集程序拒绝。"
        )
    return rates


def label_session(session_dir: Path, label: str, note: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "report_label.json").write_text(
        json.dumps({"label": label, "note": note}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def effective_warmup_seconds(args: argparse.Namespace, rate: int) -> float:
    # Low MLX90640 refresh rates can produce one or two startup subpages with
    # stale/invalid calibration results right after changing the refresh rate.
    # Waiting for several refresh periods avoids putting that startup artifact
    # inside Trigger #1.
    return max(float(args.warmup_seconds), float(args.stabilize_frames) / float(rate))


def command_for_rate(args: argparse.Namespace, rate: int) -> list[str]:
    script = (
        PROJECT_ROOT / "tools" / "macos_infrared_triggered_cl_low_delay.py"
        if args.mode == "io"
        else PROJECT_ROOT / "tools" / "macos_infrared_auto_interval_low_delay.py"
    )
    command = [
        str(PROJECT_ROOT / ".venv-macos" / "bin" / "python"),
        str(script),
    ]
    if args.mode == "io":
        command += [
            "capture-triggered-dual-combined-low-delay",
            "--trigger-io",
            str(args.trigger_io),
            "--gpio-poll-interval",
            str(args.gpio_poll_interval),
        ]
    else:
        command += [
            "capture-auto-interval-dual-combined-low-delay",
            "--trigger-interval",
            str(args.trigger_interval),
        ]
    command += [
        "--left-mlx-port",
        args.left_mlx_port,
        "--right-mlx-port",
        args.right_mlx_port,
        "--tasi-port",
        args.tasi_port,
        "--tasi-poll-interval",
        str(args.tasi_poll_interval),
        "--trigger-count",
        str(args.trigger_count),
        "--capture-seconds",
        str(args.capture_seconds),
        "--warmup-seconds",
        str(effective_warmup_seconds(args, rate)),
        "--refresh-rate-hz",
        str(rate),
        "--capture-root",
        args.capture_root,
        "--quiet-live",
    ]
    return command


def run_one_rate(args: argparse.Namespace, rate: int, index: int, total: int) -> Path | None:
    print("\n" + "=" * 72, flush=True)
    print(f"[{index}/{total}] 开始采集 {rate}Hz，每组 {args.trigger_count} 次测量", flush=True)
    if args.mode == "io":
        print("等待移动滑台 IO 触发；这一档频率完成 30 次后会自动进入下一档。", flush=True)
    else:
        print(f"自动间隔触发，每 {args.trigger_interval}s 一次。", flush=True)
    warmup_seconds = effective_warmup_seconds(args, rate)
    print(f"预热时间: {warmup_seconds:.3f}s（至少等待 {args.stabilize_frames:g} 个刷新周期）", flush=True)
    command = command_for_rate(args, rate)
    print("命令:", " ".join(command), flush=True)
    if args.dry_run:
        return None

    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_dir: Path | None = None
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        match = OUTPUT_RE.search(line)
        if match:
            output_dir = (PROJECT_ROOT / match.group(1)).resolve()
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"{rate}Hz 采集失败，退出码 {return_code}")
    if output_dir is None:
        print(f"警告: 未能从输出中解析 {rate}Hz 的目录。", flush=True)
        return None

    label = f"{args.label_prefix}-{rate:02d}Hz"
    note = (
        f"采集频率稳定性实验；mode={args.mode}; refresh_rate_hz={rate}; "
        f"trigger_count={args.trigger_count}; capture_seconds={args.capture_seconds}; "
        f"tasi_poll_interval={args.tasi_poll_interval}; "
        f"warmup_seconds={warmup_seconds:.3f}; stabilize_frames={args.stabilize_frames:g}; "
        f"created={datetime.now().isoformat(timespec='seconds')}"
    )
    label_session(output_dir, label, note)
    print(f"已标注: {output_dir} -> {label}", flush=True)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch-run low-delay infrared captures across MLX90640 refresh rates."
    )
    parser.add_argument("--mode", choices=("io", "auto"), default="io", help="io=移动滑台 IO 触发；auto=静态自动间隔触发")
    parser.add_argument("--rates", default="supported", help="频率列表，例如 supported 或 1,2,4,8,16,32；MLX 不支持 1-32 全部整数")
    parser.add_argument("--allow-unsupported-rates", action="store_true", help="允许传入硬件不支持的频率；通常采集程序会报错")
    parser.add_argument("--left-mlx-port", default="/dev/cu.usbmodem212201")
    parser.add_argument("--right-mlx-port", default="/dev/cu.usbmodem2123101")
    parser.add_argument("--tasi-port", default="/dev/cu.usbserial-21240")
    parser.add_argument("--tasi-poll-interval", type=float, default=0.25, help="TA612 轮询间隔秒数；0.25 约等于 4Hz")
    parser.add_argument("--trigger-io", type=int, default=1)
    parser.add_argument("--trigger-count", type=int, default=30)
    parser.add_argument("--trigger-interval", type=float, default=5.866)
    parser.add_argument("--capture-seconds", type=float, default=1.0)
    parser.add_argument("--warmup-seconds", type=float, default=1.0)
    parser.add_argument("--stabilize-frames", type=float, default=4.0, help="每个频率开始前至少等待多少个刷新周期；低频可避免 Trigger #1 启动伪帧")
    parser.add_argument("--gpio-poll-interval", type=float, default=0.01)
    parser.add_argument("--capture-root", default="captures")
    parser.add_argument("--label-prefix", default="频率稳定性")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令，不实际采集")
    args = parser.parse_args()

    rates = validate_rates(parse_rates(args.rates), args.allow_unsupported_rates)
    print(f"将采集 {len(rates)} 个频率: {', '.join(str(rate) + 'Hz' for rate in rates)}", flush=True)
    print("提示: 采集过程中按 Ctrl+C 会停止当前批量任务。", flush=True)

    output_dirs: list[Path] = []
    try:
      for index, rate in enumerate(rates, start=1):
          output_dir = run_one_rate(args, rate, index, len(rates))
          if output_dir is not None:
              output_dirs.append(output_dir)
    except KeyboardInterrupt:
        print("\n已手动停止批量采集。", flush=True)
        return 130

    print("\n全部完成。生成目录:", flush=True)
    for output_dir in output_dirs:
        print(f"  {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
