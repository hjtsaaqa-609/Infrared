#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from datetime import datetime, timezone
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIENT_DIR = PROJECT_ROOT / "mac_mqtt_zs8k_client"
DEFAULT_CSV_DIR = DEFAULT_CLIENT_DIR / "csv"
DEFAULT_RAW_CSV = DEFAULT_CLIENT_DIR / "zs8k_mqtt.csv"
DEFAULT_COLLECTOR_SCRIPT = DEFAULT_CLIENT_DIR / "mqtt_zs8k_client_mac.py"
DEFAULT_COLLECTOR_PYTHON = DEFAULT_CLIENT_DIR / ".venv" / "bin" / "python"
DEFAULT_COLLECTOR_PID_FILE = DEFAULT_CLIENT_DIR / ".mqtt_zs8k_client.pid"
DEFAULT_COLLECTOR_LOG_FILE = DEFAULT_CLIENT_DIR / "mqtt_zs8k_client.log"
DEFAULT_GATEWAY = "10.5.70.1"
CHANNELS = [f"ch{i}_c" for i in range(1, 9)]
CHANNEL_NAMES = ["环境温度", "左内", "上内", "上外", "左外", "右外", "前外", "后外"]
CHANNEL_LABELS = [f"ch{index + 1}-{name}" for index, name in enumerate(CHANNEL_NAMES)]
EAST8 = timezone(timedelta(hours=8))
CACHE_LOCK = threading.Lock()
CACHE: dict[str, tuple[tuple[int, int], list[dict[str, str]]]] = {}
COLLECTOR_LOCK = threading.Lock()


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def device_id_from_topic(topic: str | None) -> str:
    if not topic:
        return ""
    parts = [part for part in topic.strip("/").split("/") if part]
    return parts[-1] if parts else ""


def read_csv_cached(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(path.resolve())
    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1]
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    with CACHE_LOCK:
        CACHE[key] = (signature, rows)
    return rows


def normalize_row(row: dict[str, str], fallback_device_id: str = "") -> dict[str, Any] | None:
    received_utc = row.get("received_utc") or row.get("timestamp_utc") or ""
    received_east8 = row.get("received_east8") or ""
    timestamp = parse_timestamp(received_east8) or parse_timestamp(received_utc)
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    topic = row.get("topic", "")
    device_id = row.get("device_id") or fallback_device_id or device_id_from_topic(topic)
    values = [parse_float(row.get(channel)) for channel in CHANNELS]
    if all(value is None for value in values):
        return None
    if not received_east8:
        received_east8 = timestamp.astimezone(EAST8).isoformat()
    if not received_utc:
        received_utc = timestamp.astimezone(timezone.utc).isoformat()
    return {
        "received_east8": received_east8,
        "received_utc": received_utc,
        "timestamp_ms": int(timestamp.timestamp() * 1000),
        "device_id": device_id,
        "topic": topic,
        "channels": values,
    }


def read_device_rows(csv_dir: Path, raw_csv: Path, device_id: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    device_path = csv_dir / f"{device_id}.csv"
    per_device_rows: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    if device_path.exists():
        per_device_rows = [
            row
            for row in (normalize_row(row, device_id) for row in read_csv_cached(device_path))
            if row is not None
        ]
        latest_per_device_ms = max((row["timestamp_ms"] for row in per_device_rows), default=0)
        raw_rows = [
            row
            for row in (
                normalize_row(row, device_id)
                for row in read_csv_cached(raw_csv)
                if (row.get("device_id") or device_id_from_topic(row.get("topic"))) == device_id
            )
            if row is not None and row["timestamp_ms"] > latest_per_device_ms
        ]
        source = str(device_path)
        if raw_rows:
            source = f"{device_path} + {raw_csv}"
    else:
        raw_rows = [
            row
            for row in (
                normalize_row(row, device_id)
                for row in read_csv_cached(raw_csv)
                if (row.get("device_id") or device_id_from_topic(row.get("topic"))) == device_id
            )
            if row is not None
        ]
        source = str(raw_csv)
    normalized = per_device_rows + raw_rows
    normalized.sort(key=lambda row: row["timestamp_ms"])
    deduped = []
    seen = set()
    for row in normalized:
        key = (row["timestamp_ms"], tuple(row["channels"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    normalized = deduped
    if limit > 0:
        normalized = normalized[-limit:]
    return normalized, source


def list_devices(csv_dir: Path, raw_csv: Path) -> list[dict[str, Any]]:
    devices: dict[str, dict[str, Any]] = {}
    if csv_dir.exists():
        for path in sorted(csv_dir.glob("*.csv")):
            device_id = path.stem
            rows = [normalize_row(row, device_id) for row in read_csv_cached(path)]
            valid_rows = [row for row in rows if row is not None]
            latest = max((row["timestamp_ms"] for row in valid_rows), default=None)
            devices[device_id] = {
                "device_id": device_id,
                "source": str(path),
                "source_type": "per_device",
                "rows": len(valid_rows),
                "latest_ms": latest,
            }
    if raw_csv.exists():
        raw_counts: dict[str, int] = {}
        raw_latest: dict[str, int] = {}
        for row in read_csv_cached(raw_csv):
            device_id = row.get("device_id") or device_id_from_topic(row.get("topic"))
            if not device_id:
                continue
            normalized = normalize_row(row, device_id)
            if normalized is None:
                continue
            raw_counts[device_id] = raw_counts.get(device_id, 0) + 1
            raw_latest[device_id] = max(raw_latest.get(device_id, 0), int(normalized["timestamp_ms"]))
        for device_id, count in raw_counts.items():
            if device_id in devices:
                previous_latest = devices[device_id].get("latest_ms") or 0
                latest = raw_latest.get(device_id) or previous_latest
                devices[device_id]["latest_ms"] = max(previous_latest, latest)
                if latest > previous_latest:
                    devices[device_id]["source_type"] = "per_device+raw"
                continue
            devices[device_id] = {
                "device_id": device_id,
                "source": str(raw_csv),
                "source_type": "raw_fallback",
                "rows": count,
                "latest_ms": raw_latest.get(device_id),
            }
    return sorted(devices.values(), key=lambda item: item["device_id"])


def channel_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats = []
    for index, channel in enumerate(CHANNELS):
        series = [
            (row["timestamp_ms"], row["channels"][index])
            for row in rows
            if row["channels"][index] is not None
        ]
        values = [float(value) for _, value in series]
        latest = values[-1] if values else None
        first = values[0] if values else None
        stats.append(
            {
                "channel": channel,
                "label": CHANNEL_LABELS[index],
                "count": len(values),
                "latest": latest,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "avg": sum(values) / len(values) if values else None,
                "delta": (latest - first) if latest is not None and first is not None else None,
            }
        )
    return stats


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_collector_pid(pid_file: Path = DEFAULT_COLLECTOR_PID_FILE) -> int | None:
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def process_state(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def pid_matches_collector(pid: int, script: Path = DEFAULT_COLLECTOR_SCRIPT) -> bool:
    command = process_command(pid)
    return bool(command and script.name in command)


def reap_child(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return reaped == pid


def collector_status() -> dict[str, Any]:
    pid = read_collector_pid()
    running = bool(
        pid
        and process_is_running(pid)
        and not process_state(pid).startswith("Z")
        and pid_matches_collector(pid)
    )
    if pid and not running:
        with contextlib_suppress_oserror():
            DEFAULT_COLLECTOR_PID_FILE.unlink()
        pid = None
    log_tail = ""
    if DEFAULT_COLLECTOR_LOG_FILE.exists():
        try:
            log_tail = "\n".join(DEFAULT_COLLECTOR_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        except OSError:
            log_tail = ""
    return {
        "running": running,
        "pid": pid if running else None,
        "script": str(DEFAULT_COLLECTOR_SCRIPT),
        "python": str(DEFAULT_COLLECTOR_PYTHON if DEFAULT_COLLECTOR_PYTHON.exists() else Path(sys.executable)),
        "log": str(DEFAULT_COLLECTOR_LOG_FILE),
        "log_tail": log_tail,
    }


class contextlib_suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def start_collector() -> dict[str, Any]:
    with COLLECTOR_LOCK:
        status = collector_status()
        if status["running"]:
            return {**status, "message": "采集已经在运行"}
        if not DEFAULT_COLLECTOR_SCRIPT.exists():
            raise FileNotFoundError(f"Collector script not found: {DEFAULT_COLLECTOR_SCRIPT}")
        python = DEFAULT_COLLECTOR_PYTHON if DEFAULT_COLLECTOR_PYTHON.exists() else Path(sys.executable)
        DEFAULT_CLIENT_DIR.mkdir(parents=True, exist_ok=True)
        log = DEFAULT_COLLECTOR_LOG_FILE.open("ab", buffering=0)
        log.write(f"\n--- start {east8_now_text()} ---\n".encode("utf-8"))
        process = subprocess.Popen(
            [str(python), "-u", str(DEFAULT_COLLECTOR_SCRIPT)],
            cwd=str(DEFAULT_CLIENT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
        DEFAULT_COLLECTOR_PID_FILE.write_text(str(process.pid), encoding="utf-8")
        time.sleep(0.2)
        status = collector_status()
        return {**status, "message": "采集已启动"}


def stop_collector() -> dict[str, Any]:
    with COLLECTOR_LOCK:
        pid = read_collector_pid()
        if not pid or not process_is_running(pid) or not pid_matches_collector(pid):
            with contextlib_suppress_oserror():
                DEFAULT_COLLECTOR_PID_FILE.unlink()
            return {**collector_status(), "message": "采集没有在运行"}
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process_is_running(pid):
            if reap_child(pid) or process_state(pid).startswith("Z"):
                break
            time.sleep(0.1)
        reap_child(pid)
        if process_is_running(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                with contextlib_suppress_oserror():
                    os.kill(pid, signal.SIGKILL)
        with contextlib_suppress_oserror():
            DEFAULT_COLLECTOR_PID_FILE.unlink()
        return {**collector_status(), "message": "采集已停止"}


def east8_now_text() -> str:
    return datetime.now(EAST8).isoformat()


def local_hostname() -> str:
    try:
        name = socket.gethostname().strip()
    except OSError:
        return ""
    return name


def detect_lan_ip(gateway: str = DEFAULT_GATEWAY) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((gateway, 80))
            return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""


def dashboard_urls(host: str, port: int, lan_ip: str) -> list[tuple[str, str]]:
    urls = [("本机", f"http://127.0.0.1:{port}/")]
    if host not in ("127.0.0.1", "localhost") and lan_ip:
        urls.append(("局域网 IP", f"http://{lan_ip}:{port}/"))
    name = local_hostname()
    if name:
        domain = name if name.endswith(".local") else f"{name}.local"
        urls.append(("局域网域名", f"http://{domain}:{port}/"))
    return urls


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZS-8K 温度趋势</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --line-strong: #b6bfcc;
      --text: #1c2430;
      --muted: #687385;
      --accent: #1264a3;
      --accent-soft: #e7f2fb;
      --danger: #b42318;
      --ok: #157f3b;
      --shadow: 0 10px 24px rgba(28, 36, 48, 0.08);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    header {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 14px 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 4;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      white-space: nowrap;
    }
    .toolbar {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .field {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    button, select, input[type="number"], .download-button, .download-link {
      height: 34px;
      border: 1px solid var(--line-strong);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
      font-size: 13px;
    }
    .download-button, .download-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      font-weight: 700;
      white-space: nowrap;
    }
    .download-button[aria-disabled="true"], .download-link[aria-disabled="true"] {
      pointer-events: none;
      opacity: .55;
    }
    .file-select {
      width: 230px;
      max-width: 42vw;
    }
    button {
      cursor: pointer;
      font-weight: 600;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.success {
      background: var(--ok);
      border-color: var(--ok);
      color: #fff;
    }
    button.danger {
      background: var(--danger);
      border-color: var(--danger);
      color: #fff;
    }
    button:disabled {
      cursor: default;
      opacity: .55;
    }
    .collector {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding-left: 2px;
      white-space: nowrap;
    }
    .collector-state {
      min-width: 82px;
      height: 26px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .collector-state.running {
      color: var(--ok);
      border-color: rgba(21,127,59,.35);
      background: #edf8f1;
    }
    .collector-state.stopped {
      color: var(--danger);
      border-color: rgba(180,35,24,.28);
      background: #fff1f0;
    }
    .main {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      min-height: 0;
    }
    aside {
      border-right: 1px solid var(--line);
      background: #fbfcfd;
      padding: 16px 12px;
      overflow: auto;
    }
    .section-title {
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      margin: 0 0 8px;
    }
    .download-title {
      margin-top: 16px;
    }
    .device-list {
      display: grid;
      gap: 8px;
    }
    .device {
      width: 100%;
      height: auto;
      min-height: 58px;
      text-align: left;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 8px;
      padding: 9px 10px;
      display: grid;
      gap: 4px;
      box-shadow: none;
    }
    .device.active {
      border-color: var(--accent);
      background: var(--accent-soft);
    }
    .device .id {
      font-size: 13px;
      font-weight: 700;
    }
    .device .meta {
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .download-list {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .download-link {
      width: 100%;
      height: auto;
      min-height: 36px;
      justify-content: space-between;
      padding: 8px 10px;
      text-align: left;
      background: #fff;
      overflow-wrap: anywhere;
    }
    .download-link .name {
      font-size: 12px;
      font-weight: 750;
    }
    .download-link .hint {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      margin-left: 8px;
    }
    .content {
      min-width: 0;
      padding: 16px;
      display: grid;
      grid-template-rows: auto minmax(280px, 48vh) auto minmax(160px, 1fr);
      gap: 14px;
    }
    .status {
      min-height: 22px;
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .status strong { color: var(--text); }
    .error { color: var(--danger); font-weight: 700; }
    .chart-panel, .stats-panel, .table-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .chart-panel {
      position: relative;
      overflow: hidden;
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
    }
    .tooltip {
      position: absolute;
      min-width: 210px;
      max-width: min(340px, calc(100% - 24px));
      pointer-events: none;
      background: rgba(255,255,255,.96);
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      padding: 8px 10px;
      box-shadow: 0 10px 22px rgba(28,36,48,.14);
      font-size: 12px;
      color: var(--text);
      display: none;
    }
    .tooltip .time {
      font-weight: 700;
      margin-bottom: 5px;
    }
    .legend {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .legend label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 30px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      font-size: 12px;
      white-space: nowrap;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 3px;
      display: inline-block;
    }
    .stats-panel {
      overflow: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: right;
      white-space: nowrap;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2) {
      text-align: left;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      background: #fbfcfd;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    .table-panel {
      overflow: auto;
    }
    .empty {
      padding: 20px;
      color: var(--muted);
      font-size: 14px;
    }
    @media (max-width: 880px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      .toolbar {
        width: 100%;
        margin-left: 0;
        justify-content: flex-start;
      }
      .main {
        grid-template-columns: 1fr;
      }
      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
        max-height: 210px;
      }
      .content {
        grid-template-rows: auto 360px auto 260px;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <h1>ZS-8K 温度趋势</h1>
      <div class="collector">
        <span id="collectorState" class="collector-state">采集未知</span>
        <button id="collectorStart" class="success">开始采集</button>
        <button id="collectorStop" class="danger">停止采集</button>
      </div>
      <div class="legend" id="legend"></div>
      <div class="toolbar">
        <label class="field">
          <span>界面</span>
          <select id="viewMode">
            <option value="single" selected>单DTU</option>
            <option value="compare">双DTU对比</option>
          </select>
        </label>
        <label class="field">
          <span>CSV文件</span>
          <select id="csvFile" class="file-select">
            <option value="">等待CSV文件</option>
          </select>
        </label>
        <label class="field">
          <span>有涂层</span>
          <select id="coatedDevice" class="file-select">
            <option value="">等待CSV文件</option>
          </select>
        </label>
        <label class="field">
          <span>无涂层</span>
          <select id="uncoatedDevice" class="file-select">
            <option value="">等待CSV文件</option>
          </select>
        </label>
        <label class="field">
          <span>模式</span>
          <select id="range">
            <option value="rolling" selected>滚动窗口</option>
            <option value="3600000">最近 1 小时</option>
            <option value="21600000">最近 6 小时</option>
            <option value="86400000">最近 24 小时</option>
            <option value="2678400000">最近 31 天</option>
            <option value="0">全部数据</option>
          </select>
        </label>
        <label class="field">
          <span>显示点数</span>
          <input id="points" type="number" min="10" max="5000" step="10" value="120" title="滚动窗口显示点数">
        </label>
        <label class="field">
          <span>X轴坐标</span>
          <input id="ticks" type="number" min="4" max="24" step="1" value="10" title="X轴坐标数量">
        </label>
        <label class="field">
          <span>读取上限</span>
          <input id="limit" type="number" min="100" max="300000" step="1000" value="300000" title="31天按10秒一次约267840行，建议300000">
        </label>
        <button id="refresh" class="primary">刷新</button>
        <button id="auto">自动刷新</button>
        <a id="downloadCurrent" class="download-button" href="#" aria-disabled="true" download>下载当前CSV</a>
      </div>
    </header>
    <div class="main">
      <aside>
        <p class="section-title">DTU</p>
        <div id="devices" class="device-list"></div>
        <p class="section-title download-title">CSV下载</p>
        <div id="downloads" class="download-list"></div>
      </aside>
      <main class="content">
        <div id="status" class="status"></div>
        <section class="chart-panel">
          <canvas id="chart"></canvas>
          <div id="tooltip" class="tooltip"></div>
        </section>
        <section class="stats-panel">
          <table>
            <thead id="statsHead">
              <tr>
                <th>通道</th>
                <th>数量</th>
                <th>最新</th>
                <th>最小</th>
                <th>最大</th>
                <th>平均</th>
                <th>变化</th>
              </tr>
            </thead>
            <tbody id="stats"></tbody>
          </table>
        </section>
        <section class="table-panel">
          <table>
            <thead id="rowsHead">
              <tr>
                <th>时间</th>
                <th>设备</th>
                <th>ch1-环境温度</th><th>ch2-左内</th><th>ch3-上内</th><th>ch4-上外</th>
                <th>ch5-左外</th><th>ch6-右外</th><th>ch7-前外</th><th>ch8-后外</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
        </section>
      </main>
    </div>
  </div>
  <script>
    const colors = ["#1264a3", "#c2410c", "#148f4b", "#7c3aed", "#d97706", "#0891b2", "#be123c", "#4d7c0f"];
    const channelLabels = ["ch1-环境温度", "ch2-左内", "ch3-上内", "ch4-上外", "ch5-左外", "ch6-右外", "ch7-前外", "ch8-后外"];
    const state = {
      devices: [],
      activeDevice: "",
      viewMode: "single",
      rows: [],
      stats: [],
      compare: {
        coatedDevice: "",
        uncoatedDevice: "",
        coatedRows: [],
        uncoatedRows: [],
        coatedSource: "",
        uncoatedSource: ""
      },
      visible: new Set([0,1,2,3,4,5,6,7]),
      collector: null,
      autoTimer: null,
      hover: null
    };

    const el = {
      collectorState: document.getElementById("collectorState"),
      collectorStart: document.getElementById("collectorStart"),
      collectorStop: document.getElementById("collectorStop"),
      devices: document.getElementById("devices"),
      downloads: document.getElementById("downloads"),
      downloadCurrent: document.getElementById("downloadCurrent"),
      status: document.getElementById("status"),
      legend: document.getElementById("legend"),
      statsHead: document.getElementById("statsHead"),
      stats: document.getElementById("stats"),
      rowsHead: document.getElementById("rowsHead"),
      rows: document.getElementById("rows"),
      chart: document.getElementById("chart"),
      tooltip: document.getElementById("tooltip"),
      viewMode: document.getElementById("viewMode"),
      csvFile: document.getElementById("csvFile"),
      coatedDevice: document.getElementById("coatedDevice"),
      uncoatedDevice: document.getElementById("uncoatedDevice"),
      range: document.getElementById("range"),
      points: document.getElementById("points"),
      ticks: document.getElementById("ticks"),
      limit: document.getElementById("limit"),
      refresh: document.getElementById("refresh"),
      auto: document.getElementById("auto")
    };

    function fmtTemp(value) {
      return Number.isFinite(value) ? `${value.toFixed(1)} C` : "";
    }

    function fmtSignedTemp(value) {
      return Number.isFinite(value) ? `${value >= 0 ? "+" : ""}${value.toFixed(1)} C` : "";
    }

    function fmtTime(ms) {
      if (!ms) return "";
      return new Date(ms).toLocaleString("zh-CN", { hour12: false });
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[char]));
    }

    async function loadCollectorStatus() {
      const response = await fetch("/api/collector/status", { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json();
      state.collector = payload;
      renderCollector(payload);
      return payload;
    }

    function renderCollector(payload) {
      const running = Boolean(payload && payload.running);
      el.collectorState.textContent = running ? `采集中 PID ${payload.pid}` : "采集停止";
      el.collectorState.classList.toggle("running", running);
      el.collectorState.classList.toggle("stopped", !running);
      el.collectorStart.disabled = running;
      el.collectorStop.disabled = !running;
      el.collectorState.title = payload && payload.log ? `日志：${payload.log}` : "";
    }

    async function postCollector(action) {
      el.collectorStart.disabled = true;
      el.collectorStop.disabled = true;
      try {
        const response = await fetch(`/api/collector/${action}`, { method: "POST", cache: "no-store" });
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        renderCollector(payload);
        await refresh();
      } catch (error) {
        el.status.innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
        await loadCollectorStatus().catch(() => {});
      }
    }

    async function loadDevices() {
      const response = await fetch("/api/devices", { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json();
      state.devices = payload.devices || [];
      const deviceIds = new Set(state.devices.map(device => device.device_id));
      if (!state.devices.length) {
        state.activeDevice = "";
      } else if (!state.activeDevice || !deviceIds.has(state.activeDevice)) {
        state.activeDevice = state.devices[0].device_id;
      }
      ensureCompareDevices();
      renderDevices(payload);
      renderCsvFileOptions();
      renderCompareDeviceOptions();
      renderDownloads();
    }

    async function loadData() {
      if (state.viewMode === "compare") {
        await loadCompareData();
        return;
      }
      if (!state.activeDevice) {
        state.rows = [];
        state.stats = [];
        renderAll();
        return;
      }
      const pointCount = Math.max(10, Number(el.points.value) || 120);
      const limit = Math.max(100, pointCount, Number(el.limit.value) || 300000);
      const url = `/api/data?device=${encodeURIComponent(state.activeDevice)}&limit=${encodeURIComponent(limit)}`;
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json();
      state.rows = payload.rows || [];
      state.stats = payload.stats || [];
      const latest = state.rows.length ? fmtTime(state.rows[state.rows.length - 1].timestamp_ms) : "暂无";
      el.status.innerHTML = `<strong>${escapeHtml(state.activeDevice)}</strong> ${state.rows.length} 行，最新 ${escapeHtml(latest)}，来源 ${escapeHtml(payload.source || "")}`;
      renderAll();
    }

    async function fetchDeviceData(deviceId) {
      const pointCount = Math.max(10, Number(el.points.value) || 120);
      const limit = Math.max(100, pointCount, Number(el.limit.value) || 300000);
      const url = `/api/data?device=${encodeURIComponent(deviceId)}&limit=${encodeURIComponent(limit)}`;
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    async function loadCompareData() {
      ensureCompareDevices();
      const coated = state.compare.coatedDevice;
      const uncoated = state.compare.uncoatedDevice;
      if (!coated || !uncoated || coated === uncoated) {
        state.compare.coatedRows = [];
        state.compare.uncoatedRows = [];
        el.status.textContent = "对比需要选择两台不同的 DTU";
        renderAll();
        return;
      }
      const [coatedPayload, uncoatedPayload] = await Promise.all([
        fetchDeviceData(coated),
        fetchDeviceData(uncoated)
      ]);
      state.compare.coatedRows = coatedPayload.rows || [];
      state.compare.uncoatedRows = uncoatedPayload.rows || [];
      state.compare.coatedSource = coatedPayload.source || "";
      state.compare.uncoatedSource = uncoatedPayload.source || "";
      const coatedLatest = state.compare.coatedRows.length ? fmtTime(state.compare.coatedRows[state.compare.coatedRows.length - 1].timestamp_ms) : "暂无";
      const uncoatedLatest = state.compare.uncoatedRows.length ? fmtTime(state.compare.uncoatedRows[state.compare.uncoatedRows.length - 1].timestamp_ms) : "暂无";
      el.status.innerHTML = `有涂层 <strong>${escapeHtml(coated)}</strong> ${state.compare.coatedRows.length} 行，最新 ${escapeHtml(coatedLatest)}；无涂层 <strong>${escapeHtml(uncoated)}</strong> ${state.compare.uncoatedRows.length} 行，最新 ${escapeHtml(uncoatedLatest)}`;
      renderAll();
    }

    async function refresh() {
      el.refresh.disabled = true;
      try {
        await loadCollectorStatus();
        await loadDevices();
        await loadData();
      } catch (error) {
        el.status.innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
      } finally {
        el.refresh.disabled = false;
      }
    }

    function renderDevices(payload) {
      if (!state.devices.length) {
        el.devices.innerHTML = `<div class="empty">等待 csv/*.csv 或 zs8k_mqtt.csv</div>`;
        el.status.textContent = `CSV 目录：${payload.csv_dir}`;
        return;
      }
      el.devices.innerHTML = state.devices.map(device => `
        <button class="device ${device.device_id === state.activeDevice ? "active" : ""}" data-device="${escapeHtml(device.device_id)}">
          <span class="id">${escapeHtml(device.device_id)}</span>
          <span class="meta">${device.rows || 0} 行 · ${escapeHtml(sourceTypeText(device.source_type))}</span>
          <span class="meta">${escapeHtml(device.latest_ms ? fmtTime(device.latest_ms) : "暂无时间")}</span>
        </button>
      `).join("");
      el.devices.querySelectorAll("button").forEach(button => {
        button.addEventListener("click", async () => {
          state.activeDevice = button.dataset.device;
          renderDevices(payload);
          renderCsvFileOptions();
          renderDownloads();
          await loadData();
        });
      });
    }

    function sourceTypeText(sourceType) {
      if (sourceType === "per_device") return "分文件";
      if (sourceType === "per_device+raw") return "分文件+总表";
      return "总表";
    }

    function csvLabel(device) {
      const source = String(device.source || "");
      const filename = source.split(/[\\/]/).pop() || `${device.device_id}.csv`;
      const sourceType = sourceTypeText(device.source_type);
      return `${filename} / ${device.device_id} (${sourceType})`;
    }

    function renderCsvFileOptions() {
      if (!state.devices.length) {
        el.csvFile.disabled = true;
        el.csvFile.innerHTML = `<option value="">等待CSV文件</option>`;
        return;
      }
      el.csvFile.disabled = false;
      el.csvFile.innerHTML = state.devices.map(device => `
        <option value="${escapeHtml(device.device_id)}" ${device.device_id === state.activeDevice ? "selected" : ""}>
          ${escapeHtml(csvLabel(device))}
        </option>
      `).join("");
    }

    function ensureCompareDevices() {
      const ids = state.devices.map(device => device.device_id);
      if (!ids.length) {
        state.compare.coatedDevice = "";
        state.compare.uncoatedDevice = "";
        return;
      }
      if (!ids.includes(state.compare.coatedDevice)) {
        state.compare.coatedDevice = ids[0] || "";
      }
      if (!ids.includes(state.compare.uncoatedDevice) || state.compare.uncoatedDevice === state.compare.coatedDevice) {
        state.compare.uncoatedDevice = ids.find(id => id !== state.compare.coatedDevice) || "";
      }
      if (!state.compare.uncoatedDevice && ids.length > 1) {
        state.compare.uncoatedDevice = ids[1];
      }
    }

    function renderCompareDeviceOptions() {
      if (!state.devices.length) {
        el.coatedDevice.disabled = true;
        el.uncoatedDevice.disabled = true;
        el.coatedDevice.innerHTML = `<option value="">等待CSV文件</option>`;
        el.uncoatedDevice.innerHTML = `<option value="">等待CSV文件</option>`;
        return;
      }
      el.coatedDevice.disabled = false;
      el.uncoatedDevice.disabled = false;
      el.coatedDevice.innerHTML = state.devices.map(device => `
        <option value="${escapeHtml(device.device_id)}" ${device.device_id === state.compare.coatedDevice ? "selected" : ""}>
          ${escapeHtml(device.device_id)}
        </option>
      `).join("");
      el.uncoatedDevice.innerHTML = state.devices.map(device => `
        <option value="${escapeHtml(device.device_id)}" ${device.device_id === state.compare.uncoatedDevice ? "selected" : ""}>
          ${escapeHtml(device.device_id)}
        </option>
      `).join("");
    }

    function csvDownloadUrl(deviceId) {
      return `/download/${encodeURIComponent(deviceId)}.csv`;
    }

    function renderDownloads() {
      if (!state.devices.length) {
        el.downloads.innerHTML = `<div class="empty">暂无CSV文件</div>`;
        el.downloadCurrent.href = "#";
        el.downloadCurrent.setAttribute("aria-disabled", "true");
        return;
      }
      el.downloads.innerHTML = state.devices.map(device => `
        <a class="download-link" href="${csvDownloadUrl(device.device_id)}" download="${escapeHtml(device.device_id)}.csv">
          <span class="name">${escapeHtml(device.device_id)}.csv</span>
          <span class="hint">下载</span>
        </a>
      `).join("") + `
        <a class="download-link" href="/download/all.zip" download="zs8k_csv.zip">
          <span class="name">全部CSV.zip</span>
          <span class="hint">下载</span>
        </a>
      `;
      if (state.activeDevice) {
        el.downloadCurrent.href = csvDownloadUrl(state.activeDevice);
        el.downloadCurrent.download = `${state.activeDevice}.csv`;
        el.downloadCurrent.removeAttribute("aria-disabled");
      } else {
        el.downloadCurrent.href = "#";
        el.downloadCurrent.setAttribute("aria-disabled", "true");
      }
    }

    function renderLegend() {
      const styleNote = state.viewMode === "compare"
        ? `<label><span class="swatch" style="background:#1c2430"></span> 实线=有涂层 · 虚线=无涂层</label>`
        : "";
      el.legend.innerHTML = colors.map((color, index) => `
        <label>
          <input type="checkbox" data-channel="${index}" ${state.visible.has(index) ? "checked" : ""}>
          <span class="swatch" style="background:${color}"></span>
          ${escapeHtml(channelLabels[index])}
        </label>
      `).join("") + styleNote;
      el.legend.querySelectorAll("input").forEach(input => {
        input.addEventListener("change", () => {
          const index = Number(input.dataset.channel);
          if (input.checked) state.visible.add(index);
          else state.visible.delete(index);
          renderAll();
        });
      });
    }

    function filteredRows() {
      return filterRows(state.rows);
    }

    function filterRows(rows) {
      if (!rows.length) return rows;
      const pointCount = Math.max(1, Number(el.points.value) || 120);
      if (el.range.value === "rolling") {
        return rows.slice(-pointCount);
      }
      const rangeMs = Number(el.range.value) || 0;
      if (!rangeMs) return rows;
      const end = rows[rows.length - 1].timestamp_ms;
      return rows.filter(row => row.timestamp_ms >= end - rangeMs).slice(-pointCount);
    }

    function summarizeValues(values) {
      if (!values.length) {
        return { latest: null, min: null, max: null, avg: null, delta: null };
      }
      let min = values[0];
      let max = values[0];
      let sum = 0;
      for (const value of values) {
        if (value < min) min = value;
        if (value > max) max = value;
        sum += value;
      }
      const latest = values[values.length - 1];
      return { latest, min, max, avg: sum / values.length, delta: latest - values[0] };
    }

    function computeStats(rows) {
      return colors.map((_, index) => {
        const values = rows.map(row => row.channels[index]).filter(Number.isFinite);
        const summary = summarizeValues(values);
        return {
          label: channelLabels[index],
          count: values.length,
          ...summary
        };
      });
    }

    function renderStats(rows) {
      el.statsHead.innerHTML = `
        <tr>
          <th>通道</th>
          <th>数量</th>
          <th>最新</th>
          <th>最小</th>
          <th>最大</th>
          <th>平均</th>
          <th>变化</th>
        </tr>
      `;
      const stats = computeStats(rows);
      el.stats.innerHTML = stats.map((item, index) => `
        <tr>
          <td><span class="swatch" style="background:${colors[index]}"></span> ${item.label}</td>
          <td>${item.count}</td>
          <td>${fmtTemp(item.latest)}</td>
          <td>${fmtTemp(item.min)}</td>
          <td>${fmtTemp(item.max)}</td>
          <td>${fmtTemp(item.avg)}</td>
          <td>${Number.isFinite(item.delta) ? `${item.delta >= 0 ? "+" : ""}${item.delta.toFixed(1)} C` : ""}</td>
        </tr>
      `).join("");
    }

    function renderCompareStats(coatedRows, uncoatedRows) {
      el.statsHead.innerHTML = `
        <tr>
          <th>通道</th>
          <th>最新(有涂层)</th>
          <th>最新(无涂层)</th>
          <th>最新差</th>
          <th>平均(有涂层)</th>
          <th>平均(无涂层)</th>
          <th>平均差</th>
          <th>数量(有/无)</th>
        </tr>
      `;
      const coatedStats = computeStats(coatedRows);
      const uncoatedStats = computeStats(uncoatedRows);
      el.stats.innerHTML = coatedStats.map((coated, index) => {
        const uncoated = uncoatedStats[index];
        const latestDiff = Number.isFinite(coated.latest) && Number.isFinite(uncoated.latest) ? coated.latest - uncoated.latest : null;
        const avgDiff = Number.isFinite(coated.avg) && Number.isFinite(uncoated.avg) ? coated.avg - uncoated.avg : null;
        return `
          <tr>
            <td><span class="swatch" style="background:${colors[index]}"></span> ${coated.label}</td>
            <td>${fmtTemp(coated.latest)}</td>
            <td>${fmtTemp(uncoated.latest)}</td>
            <td>${fmtSignedTemp(latestDiff)}</td>
            <td>${fmtTemp(coated.avg)}</td>
            <td>${fmtTemp(uncoated.avg)}</td>
            <td>${fmtSignedTemp(avgDiff)}</td>
            <td>${coated.count} / ${uncoated.count}</td>
          </tr>
        `;
      }).join("");
    }

    function renderRows(rows) {
      el.rowsHead.innerHTML = `
        <tr>
          <th>时间</th>
          <th>设备</th>
          <th>ch1-环境温度</th><th>ch2-左内</th><th>ch3-上内</th><th>ch4-上外</th>
          <th>ch5-左外</th><th>ch6-右外</th><th>ch7-前外</th><th>ch8-后外</th>
        </tr>
      `;
      const recent = rows.slice(-80).reverse();
      el.rows.innerHTML = recent.map(row => `
        <tr>
          <td>${escapeHtml(fmtTime(row.timestamp_ms))}</td>
          <td>${escapeHtml(row.device_id)}</td>
          ${row.channels.map(value => `<td>${fmtTemp(value)}</td>`).join("")}
        </tr>
      `).join("");
    }

    function renderCompareRows(coatedRows, uncoatedRows) {
      el.rowsHead.innerHTML = `
        <tr>
          <th>时间</th>
          <th>类型</th>
          <th>设备</th>
          <th>ch1-环境温度</th><th>ch2-左内</th><th>ch3-上内</th><th>ch4-上外</th>
          <th>ch5-左外</th><th>ch6-右外</th><th>ch7-前外</th><th>ch8-后外</th>
        </tr>
      `;
      const combined = [
        ...coatedRows.map(row => ({ ...row, role: "有涂层" })),
        ...uncoatedRows.map(row => ({ ...row, role: "无涂层" }))
      ].sort((a, b) => b.timestamp_ms - a.timestamp_ms).slice(0, 80);
      el.rows.innerHTML = combined.map(row => `
        <tr>
          <td>${escapeHtml(fmtTime(row.timestamp_ms))}</td>
          <td>${escapeHtml(row.role)}</td>
          <td>${escapeHtml(row.device_id)}</td>
          ${row.channels.map(value => `<td>${fmtTemp(value)}</td>`).join("")}
        </tr>
      `).join("");
    }

    function renderAll() {
      renderLegend();
      if (state.viewMode === "compare") {
        const coatedRows = filterRows(state.compare.coatedRows);
        const uncoatedRows = filterRows(state.compare.uncoatedRows);
        renderCompareStats(coatedRows, uncoatedRows);
        renderCompareRows(coatedRows, uncoatedRows);
        drawCompareChart(coatedRows, uncoatedRows);
      } else {
        const rows = filteredRows();
        renderStats(rows);
        renderRows(rows);
        drawChart(rows);
      }
    }

    function chartBounds(rows) {
      const values = [];
      rows.forEach(row => {
        row.channels.forEach((value, index) => {
          if (state.visible.has(index) && Number.isFinite(value)) values.push(value);
        });
      });
      if (!rows.length || !values.length) return null;
      const minTime = rows[0].timestamp_ms;
      const maxTime = rows[rows.length - 1].timestamp_ms;
      let minValue = values[0];
      let maxValue = values[0];
      for (const value of values) {
        if (value < minValue) minValue = value;
        if (value > maxValue) maxValue = value;
      }
      if (Math.abs(maxValue - minValue) < 0.5) {
        minValue -= 0.5;
        maxValue += 0.5;
      }
      const pad = (maxValue - minValue) * 0.12;
      return { minTime, maxTime, minValue: minValue - pad, maxValue: maxValue + pad };
    }

    function chartBoundsForGroups(rowGroups) {
      const values = [];
      let minTime = null;
      let maxTime = null;
      rowGroups.forEach(rows => {
        rows.forEach(row => {
          minTime = minTime === null ? row.timestamp_ms : Math.min(minTime, row.timestamp_ms);
          maxTime = maxTime === null ? row.timestamp_ms : Math.max(maxTime, row.timestamp_ms);
          row.channels.forEach((value, index) => {
            if (state.visible.has(index) && Number.isFinite(value)) values.push(value);
          });
        });
      });
      if (minTime === null || maxTime === null || !values.length) return null;
      let minValue = values[0];
      let maxValue = values[0];
      for (const value of values) {
        if (value < minValue) minValue = value;
        if (value > maxValue) maxValue = value;
      }
      if (Math.abs(maxTime - minTime) < 1) {
        minTime -= 1;
        maxTime += 1;
      }
      if (Math.abs(maxValue - minValue) < 0.5) {
        minValue -= 0.5;
        maxValue += 0.5;
      }
      const pad = (maxValue - minValue) * 0.12;
      return { minTime, maxTime, minValue: minValue - pad, maxValue: maxValue + pad };
    }

    function drawCompareChart(coatedRows, uncoatedRows) {
      const canvas = el.chart;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, rect.width, rect.height);

      const pad = { left: 56, right: 18, top: 20, bottom: 38 };
      const plotW = Math.max(10, rect.width - pad.left - pad.right);
      const plotH = Math.max(10, rect.height - pad.top - pad.bottom);
      const bounds = chartBoundsForGroups([coatedRows, uncoatedRows]);

      ctx.strokeStyle = "#d9dee7";
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(pad.left, pad.top, plotW, plotH);

      if (!bounds) {
        ctx.fillStyle = "#687385";
        ctx.font = "14px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText("暂无可绘制对比数据", pad.left + 16, pad.top + 32);
        return;
      }

      const xFor = (time) => pad.left + ((time - bounds.minTime) / Math.max(1, bounds.maxTime - bounds.minTime)) * plotW;
      const yFor = (value) => pad.top + (1 - (value - bounds.minValue) / Math.max(0.001, bounds.maxValue - bounds.minValue)) * plotH;

      ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillStyle = "#687385";
      ctx.strokeStyle = "#edf0f4";
      ctx.setLineDash([]);
      for (let i = 0; i <= 5; i++) {
        const y = pad.top + (plotH * i / 5);
        const value = bounds.maxValue - ((bounds.maxValue - bounds.minValue) * i / 5);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
        ctx.fillText(value.toFixed(1), 12, y + 4);
      }
      const tickCount = Math.max(4, Math.min(24, Number(el.ticks.value) || 10));
      for (let i = 0; i < tickCount; i++) {
        const ratio = tickCount === 1 ? 0 : i / (tickCount - 1);
        const x = pad.left + (plotW * ratio);
        const time = bounds.minTime + ((bounds.maxTime - bounds.minTime) * ratio);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        const label = new Date(time).toLocaleTimeString("zh-CN", { hour12: false });
        const textWidth = ctx.measureText(label).width;
        const labelX = Math.max(pad.left, Math.min(pad.left + plotW - textWidth, x - textWidth / 2));
        ctx.fillText(label, labelX, pad.top + plotH + 25);
      }

      function drawRows(rows, dash) {
        colors.forEach((color, index) => {
          if (!state.visible.has(index)) return;
          ctx.beginPath();
          ctx.strokeStyle = color;
          ctx.lineWidth = dash.length ? 1.8 : 2.3;
          ctx.setLineDash(dash);
          let started = false;
          rows.forEach(row => {
            const value = row.channels[index];
            if (!Number.isFinite(value)) {
              started = false;
              return;
            }
            const x = xFor(row.timestamp_ms);
            const y = yFor(value);
            if (!started) {
              ctx.moveTo(x, y);
              started = true;
            } else {
              ctx.lineTo(x, y);
            }
          });
          ctx.stroke();
        });
      }

      drawRows(coatedRows, []);
      drawRows(uncoatedRows, [6, 4]);
      ctx.setLineDash([]);

      if (state.hover && (coatedRows.length || uncoatedRows.length)) {
        const hoverX = Math.max(pad.left, Math.min(pad.left + plotW, state.hover.x));
        const targetTime = bounds.minTime + ((hoverX - pad.left) / plotW) * (bounds.maxTime - bounds.minTime);
        const nearest = (rows) => {
          if (!rows.length) return null;
          let item = rows[0];
          for (const row of rows) {
            if (Math.abs(row.timestamp_ms - targetTime) < Math.abs(item.timestamp_ms - targetTime)) item = row;
          }
          return item;
        };
        const coated = nearest(coatedRows);
        const uncoated = nearest(uncoatedRows);
        const guideTime = coated && uncoated
          ? (Math.abs(coated.timestamp_ms - targetTime) <= Math.abs(uncoated.timestamp_ms - targetTime) ? coated.timestamp_ms : uncoated.timestamp_ms)
          : (coated ? coated.timestamp_ms : uncoated.timestamp_ms);
        const x = xFor(guideTime);
        ctx.strokeStyle = "#1c2430";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        const roleLines = (role, row) => {
          if (!row) return `<div class="time">${escapeHtml(role)}：暂无</div>`;
          const values = [...state.visible].sort((a, b) => a - b).map(index => {
            const value = row.channels[index];
            return `<div><span class="swatch" style="background:${colors[index]}"></span> ${escapeHtml(channelLabels[index])}: ${escapeHtml(fmtTemp(value))}</div>`;
          }).join("");
          return `<div class="time">${escapeHtml(role)} ${escapeHtml(fmtTime(row.timestamp_ms))}</div>${values}`;
        };
        el.tooltip.innerHTML = `${roleLines("有涂层", coated)}<hr>${roleLines("无涂层", uncoated)}`;
        const tooltipX = Math.min(rect.width - 260, Math.max(10, x + 12));
        el.tooltip.style.left = `${tooltipX}px`;
        el.tooltip.style.top = `${Math.max(10, state.hover.y - 20)}px`;
        el.tooltip.style.display = "block";
      } else {
        el.tooltip.style.display = "none";
      }
    }

    function drawChart(rows) {
      const canvas = el.chart;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, rect.width, rect.height);

      const pad = { left: 56, right: 18, top: 20, bottom: 38 };
      const plotW = Math.max(10, rect.width - pad.left - pad.right);
      const plotH = Math.max(10, rect.height - pad.top - pad.bottom);
      const bounds = chartBounds(rows);

      ctx.strokeStyle = "#d9dee7";
      ctx.lineWidth = 1;
      ctx.strokeRect(pad.left, pad.top, plotW, plotH);

      if (!bounds) {
        ctx.fillStyle = "#687385";
        ctx.font = "14px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText("暂无可绘制数据", pad.left + 16, pad.top + 32);
        return;
      }

      const xFor = (time) => pad.left + ((time - bounds.minTime) / Math.max(1, bounds.maxTime - bounds.minTime)) * plotW;
      const yFor = (value) => pad.top + (1 - (value - bounds.minValue) / Math.max(0.001, bounds.maxValue - bounds.minValue)) * plotH;

      ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillStyle = "#687385";
      ctx.strokeStyle = "#edf0f4";
      for (let i = 0; i <= 5; i++) {
        const y = pad.top + (plotH * i / 5);
        const value = bounds.maxValue - ((bounds.maxValue - bounds.minValue) * i / 5);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
        ctx.fillText(value.toFixed(1), 12, y + 4);
      }
      const tickCount = Math.max(4, Math.min(24, Number(el.ticks.value) || 10));
      for (let i = 0; i < tickCount; i++) {
        const ratio = tickCount === 1 ? 0 : i / (tickCount - 1);
        const x = pad.left + (plotW * ratio);
        const time = bounds.minTime + ((bounds.maxTime - bounds.minTime) * ratio);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        const label = new Date(time).toLocaleTimeString("zh-CN", { hour12: false });
        const textWidth = ctx.measureText(label).width;
        const labelX = Math.max(pad.left, Math.min(pad.left + plotW - textWidth, x - textWidth / 2));
        ctx.fillText(label, labelX, pad.top + plotH + 25);
      }

      colors.forEach((color, index) => {
        if (!state.visible.has(index)) return;
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        let started = false;
        rows.forEach(row => {
          const value = row.channels[index];
          if (!Number.isFinite(value)) {
            started = false;
            return;
          }
          const x = xFor(row.timestamp_ms);
          const y = yFor(value);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        });
        ctx.stroke();
      });

      if (state.hover && rows.length) {
        const hoverX = Math.max(pad.left, Math.min(pad.left + plotW, state.hover.x));
        const targetTime = bounds.minTime + ((hoverX - pad.left) / plotW) * (bounds.maxTime - bounds.minTime);
        let nearest = rows[0];
        for (const row of rows) {
          if (Math.abs(row.timestamp_ms - targetTime) < Math.abs(nearest.timestamp_ms - targetTime)) nearest = row;
        }
        const x = xFor(nearest.timestamp_ms);
        ctx.strokeStyle = "#1c2430";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        const lines = [...state.visible].sort((a, b) => a - b).map(index => {
          const value = nearest.channels[index];
          return `<div><span class="swatch" style="background:${colors[index]}"></span> ${escapeHtml(channelLabels[index])}: ${escapeHtml(fmtTemp(value))}</div>`;
        }).join("");
        el.tooltip.innerHTML = `<div class="time">${escapeHtml(fmtTime(nearest.timestamp_ms))}</div>${lines}`;
        const tooltipX = Math.min(rect.width - 230, Math.max(10, x + 12));
        el.tooltip.style.left = `${tooltipX}px`;
        el.tooltip.style.top = `${Math.max(10, state.hover.y - 20)}px`;
        el.tooltip.style.display = "block";
      } else {
        el.tooltip.style.display = "none";
      }
    }

    function redrawChartOnly() {
      if (state.viewMode === "compare") {
        drawCompareChart(filterRows(state.compare.coatedRows), filterRows(state.compare.uncoatedRows));
      } else {
        drawChart(filteredRows());
      }
    }

    el.chart.addEventListener("mousemove", event => {
      const rect = el.chart.getBoundingClientRect();
      state.hover = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      redrawChartOnly();
    });
    el.chart.addEventListener("mouseleave", () => {
      state.hover = null;
      redrawChartOnly();
    });
    el.refresh.addEventListener("click", refresh);
    el.viewMode.addEventListener("change", async () => {
      state.viewMode = el.viewMode.value;
      await loadData();
    });
    el.csvFile.addEventListener("change", async () => {
      state.activeDevice = el.csvFile.value;
      renderDevices({ csv_dir: "", devices: state.devices });
      renderDownloads();
      await loadData();
    });
    el.coatedDevice.addEventListener("change", async () => {
      state.compare.coatedDevice = el.coatedDevice.value;
      if (state.compare.coatedDevice === state.compare.uncoatedDevice) {
        state.compare.uncoatedDevice = state.devices.map(device => device.device_id).find(id => id !== state.compare.coatedDevice) || "";
      }
      renderCompareDeviceOptions();
      await loadData();
    });
    el.uncoatedDevice.addEventListener("change", async () => {
      state.compare.uncoatedDevice = el.uncoatedDevice.value;
      if (state.compare.coatedDevice === state.compare.uncoatedDevice) {
        state.compare.coatedDevice = state.devices.map(device => device.device_id).find(id => id !== state.compare.uncoatedDevice) || "";
      }
      renderCompareDeviceOptions();
      await loadData();
    });
    el.range.addEventListener("change", renderAll);
    el.points.addEventListener("change", renderAll);
    el.ticks.addEventListener("change", renderAll);
    el.limit.addEventListener("change", loadData);
    el.collectorStart.addEventListener("click", () => postCollector("start"));
    el.collectorStop.addEventListener("click", () => postCollector("stop"));
    el.auto.addEventListener("click", () => {
      if (state.autoTimer) {
        clearInterval(state.autoTimer);
        state.autoTimer = null;
        el.auto.textContent = "自动刷新";
        el.auto.classList.remove("primary");
      } else {
        state.autoTimer = setInterval(refresh, 10000);
        el.auto.textContent = "停止自动";
        el.auto.classList.add("primary");
      }
    });
    window.addEventListener("resize", redrawChartOnly);
    refresh();
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    csv_dir: Path = DEFAULT_CSV_DIR
    raw_csv: Path = DEFAULT_RAW_CSV

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html(HTML)
                return
            if parsed.path == "/api/devices":
                self.send_json(
                    {
                        "csv_dir": str(self.csv_dir),
                        "raw_csv": str(self.raw_csv),
                        "devices": list_devices(self.csv_dir, self.raw_csv),
                    }
                )
                return
            if parsed.path == "/api/collector/status":
                self.send_json(collector_status())
                return
            if parsed.path == "/api/data":
                query = parse_qs(parsed.query)
                device = (query.get("device") or [""])[0]
                limit = int((query.get("limit") or ["300000"])[0])
                if not device:
                    self.send_json({"rows": [], "stats": [], "source": ""})
                    return
                rows, source = read_device_rows(self.csv_dir, self.raw_csv, device, limit)
                self.send_json(
                    {
                        "device_id": device,
                        "source": source,
                        "rows": rows,
                        "stats": channel_stats(rows),
                    }
                )
                return
            if parsed.path == "/download/all.zip":
                self.send_zip_download()
                return
            if parsed.path.startswith("/download/"):
                device = unquote(parsed.path.removeprefix("/download/")).removesuffix(".csv")
                self.send_csv_download(device)
                return
            self.send_error(404, "Not found")
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/collector/start":
                self.send_json(start_collector())
                return
            if parsed.path == "/api/collector/stop":
                self.send_json(stop_collector())
                return
            self.send_error(404, "Not found")
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_csv_download(self, device: str) -> None:
        if not device or "/" in device or "\\" in device or device in {".", ".."}:
            self.send_error(400, "Invalid CSV name")
            return
        csv_dir = self.csv_dir.resolve()
        path = (csv_dir / f"{device}.csv").resolve()
        if csv_dir not in path.parents:
            self.send_error(400, "Invalid CSV path")
            return
        if not path.exists():
            self.send_error(404, "CSV not found")
            return
        data = path.read_bytes()
        filename = quote(path.name)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_zip_download(self) -> None:
        paths = [path for path in sorted(self.csv_dir.glob("*.csv")) if path.is_file()]
        if not paths:
            self.send_error(404, "CSV not found")
            return
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, arcname=path.name)
        data = buffer.getvalue()
        filename = quote("zs8k_csv.zip")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local ZS-8K CSV trend dashboard.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address; 0.0.0.0 allows LAN access")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY, help="Gateway used to auto-detect the LAN IPv4 address")
    parser.add_argument("--lan-ip", default="auto", help="LAN IPv4 address printed for other computers; default: auto")
    parser.add_argument("--csv-dir", default=str(DEFAULT_CSV_DIR), help="Directory containing per-DTU CSV files")
    parser.add_argument("--raw-csv", default=str(DEFAULT_RAW_CSV), help="Fallback raw MQTT CSV")
    parser.add_argument("--open", action=argparse.BooleanOptionalAction, default=True, help="Open browser after starting")
    args = parser.parse_args()

    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {})
    handler.csv_dir = Path(args.csv_dir).expanduser().resolve()
    handler.raw_csv = Path(args.raw_csv).expanduser().resolve()

    server = ThreadingHTTPServer((args.host, args.port), handler)
    lan_ip = detect_lan_ip(args.gateway) if args.lan_ip == "auto" else args.lan_ip
    urls = dashboard_urls(args.host, args.port, lan_ip)
    print("ZS-8K CSV dashboard:")
    for label, url in urls:
        print(f"  {label}: {url}")
    print(f"CSV directory: {handler.csv_dir}")
    print(f"Raw fallback CSV: {handler.raw_csv}")
    if args.open:
        webbrowser.open(urls[0][1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
