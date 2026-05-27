#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


DEFAULT_SESSION = "captures/mac_dual_mlx_tasi_20260526_113720"
MLX_PIXELS = 32 * 24
PHYSICAL_MIN_C = 0.0
PHYSICAL_MAX_C = 200.0
SERIES_KEYS = (
    "left_min",
    "left_avg",
    "left_max",
    "right_min",
    "right_avg",
    "right_max",
)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_float(value: str) -> float:
    if value.lower() == "nan":
        return float("nan")
    return float(value)


def finite(values: list[float]) -> list[float]:
    return [value for value in values if math.isfinite(value)]


def mean(values: list[float]) -> float:
    valid = finite(values)
    if not valid:
        return float("nan")
    return sum(valid) / len(valid)


def percentile(values: list[float], p: float) -> float:
    valid = sorted(finite(values))
    if not valid:
        return float("nan")
    k = (len(valid) - 1) * p / 100
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return valid[lo]
    return valid[lo] + (valid[hi] - valid[lo]) * (k - lo)


def stddev(values: list[float]) -> float:
    valid = finite(values)
    if not valid:
        return float("nan")
    avg = sum(valid) / len(valid)
    return math.sqrt(sum((value - avg) ** 2 for value in valid) / len(valid))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass(frozen=True)
class MlxFrame:
    index: int
    timestamp: datetime
    min_c: float
    avg_c: float
    max_c: float
    ta_c: float


@dataclass(frozen=True)
class TasiFrame:
    timestamp: datetime
    channel1_c: float
    channel2_c: float
    channel3_c: float
    channel4_c: float


def load_mlx_frames(path: Path) -> list[MlxFrame]:
    frames: list[MlxFrame] = []
    for index, row in enumerate(read_csv(path)):
        try:
            frames.append(
                MlxFrame(
                    index=index,
                    timestamp=parse_timestamp(row["timestamp_east8"]),
                    min_c=parse_float(row["min_c"]),
                    avg_c=parse_float(row["avg_c"]),
                    max_c=parse_float(row["max_c"]),
                    ta_c=parse_float(row["ta_c"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return frames


def load_tasi_frames(path: Path) -> list[TasiFrame]:
    frames: list[TasiFrame] = []
    for row in read_csv(path):
        try:
            if row.get("checksum_ok") not in (None, "", "True", "true", "1"):
                continue
            frames.append(
                TasiFrame(
                    timestamp=parse_timestamp(row["timestamp_east8"]),
                    channel1_c=parse_float(row["channel1_c"]),
                    channel2_c=parse_float(row["channel2_c"]),
                    channel3_c=parse_float(row["channel3_c"]),
                    channel4_c=parse_float(row["channel4_c"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return frames


def nearest_index(timestamps: list[datetime], target: datetime) -> int | None:
    index = bisect.bisect_left(timestamps, target)
    candidates: list[int] = []
    if index < len(timestamps):
        candidates.append(index)
    if index > 0:
        candidates.append(index - 1)
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs((timestamps[item] - target).total_seconds()))


def filter_limit(values: list[float], mode: str) -> float:
    mode = mode.lower()
    if mode in ("none", "physical"):
        return float("inf")
    if mode == "p99":
        return percentile(values, 99)
    return percentile(values, 95)


def summarize_values(values: list[float]) -> dict[str, Any]:
    valid = finite(values)
    if not valid:
        return {
            "n": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
            "std": None,
        }
    return {
        "n": len(valid),
        "min": min(valid),
        "mean": mean(valid),
        "p50": percentile(valid, 50),
        "p95": percentile(valid, 95),
        "max": max(valid),
        "std": stddev(valid),
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"n": len(rows), "series": {}}
    for key in SERIES_KEYS:
        raw_values = [row[key] for row in rows if math.isfinite(row[key])]
        filtered_values = [row[f"{key}_filtered"] for row in rows if math.isfinite(row[f"{key}_filtered"])]
        result["series"][key] = {
            "raw": summarize_values(raw_values),
            "filtered": summarize_values(filtered_values),
            "removed": len(raw_values) - len(filtered_values),
        }
    result["tasi4"] = summarize_values([row["tasi4"] for row in rows])
    return result


def sample_rows(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if max_points <= 0 or len(rows) <= max_points:
        return rows
    sampled: list[dict[str, Any]] = []
    step = len(rows) / max_points
    for index in range(max_points):
        sampled.append(rows[min(int(index * step), len(rows) - 1)])
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def physical_anomaly_stats(session_dir: Path, side: str, frames: list[MlxFrame]) -> dict[int, dict[str, Any]]:
    stats: dict[int, dict[str, Any]] = {}
    if not frames:
        return stats
    path = session_dir / "temp" / f"{side}_to.f32le"
    if not path.exists():
        for frame in frames:
            stats[frame.index] = {
                "bad": (
                    not math.isfinite(frame.min_c)
                    or not math.isfinite(frame.avg_c)
                    or not math.isfinite(frame.max_c)
                    or frame.min_c < PHYSICAL_MIN_C
                    or frame.max_c > PHYSICAL_MAX_C
                ),
                "lt0": int(math.isfinite(frame.min_c) and frame.min_c < PHYSICAL_MIN_C),
                "gt200": int(math.isfinite(frame.max_c) and frame.max_c > PHYSICAL_MAX_C),
                "nan": int(not all(math.isfinite(value) for value in (frame.min_c, frame.avg_c, frame.max_c))),
            }
        return stats

    frame_size = MLX_PIXELS * 4
    with path.open("rb") as file:
        for frame in frames:
            file.seek(frame.index * frame_size)
            data = file.read(frame_size)
            if len(data) != frame_size:
                stats[frame.index] = {"bad": True, "lt0": 0, "gt200": 0, "nan": 1}
                continue
            values = struct.unpack("<768f", data)
            nan_count = 0
            lt_count = 0
            gt_count = 0
            for value in values:
                if not math.isfinite(value):
                    nan_count += 1
                elif value < PHYSICAL_MIN_C:
                    lt_count += 1
                elif value > PHYSICAL_MAX_C:
                    gt_count += 1
            stats[frame.index] = {
                "bad": bool(nan_count or lt_count or gt_count),
                "lt0": lt_count,
                "gt200": gt_count,
                "nan": nan_count,
            }
    return stats


def analyze_session(session_dir: Path, filter_mode: str, align_ms: float, max_points: int) -> dict[str, Any]:
    filter_mode = filter_mode.lower()
    if filter_mode not in {"physical", "p95", "p99", "none"}:
        filter_mode = "physical"
    required = {
        "left": session_dir / "left_mlx_frames.csv",
        "right": session_dir / "right_mlx_frames.csv",
        "tasi": session_dir / "tasi_serial_frames.csv",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required files: " + ", ".join(missing))

    left = load_mlx_frames(required["left"])
    right = load_mlx_frames(required["right"])
    tasi = load_tasi_frames(required["tasi"])
    if not left or not right or not tasi:
        raise ValueError("Session has no usable left/right MLX or TA612 rows")

    left_times = [frame.timestamp for frame in left]
    right_times = [frame.timestamp for frame in right]
    start = tasi[0].timestamp
    aligned: list[dict[str, Any]] = []

    for tasi_frame in tasi:
        left_index = nearest_index(left_times, tasi_frame.timestamp)
        right_index = nearest_index(right_times, tasi_frame.timestamp)
        if left_index is None or right_index is None:
            continue
        left_dt = (left[left_index].timestamp - tasi_frame.timestamp).total_seconds() * 1000
        right_dt = (right[right_index].timestamp - tasi_frame.timestamp).total_seconds() * 1000
        if abs(left_dt) > align_ms or abs(right_dt) > align_ms:
            continue
        left_frame = left[left_index]
        right_frame = right[right_index]
        row = {
            "timestamp": tasi_frame.timestamp,
            "minutes": (tasi_frame.timestamp - start).total_seconds() / 60,
            "tasi4": tasi_frame.channel4_c,
            "left_min": left_frame.min_c,
            "left_avg": left_frame.avg_c,
            "left_max": left_frame.max_c,
            "right_min": right_frame.min_c,
            "right_avg": right_frame.avg_c,
            "right_max": right_frame.max_c,
            "left_dt_ms": left_dt,
            "right_dt_ms": right_dt,
            "left_index": left_frame.index,
            "right_index": right_frame.index,
        }
        aligned.append(row)

    if not aligned:
        raise ValueError("No rows matched the requested timestamp alignment window")

    left_anomalies: dict[int, dict[str, Any]] = {}
    right_anomalies: dict[int, dict[str, Any]] = {}
    if filter_mode == "physical":
        left_indices = {row["left_index"] for row in aligned}
        right_indices = {row["right_index"] for row in aligned}
        left_anomalies = physical_anomaly_stats(session_dir, "left", [left[index] for index in sorted(left_indices)])
        right_anomalies = physical_anomaly_stats(session_dir, "right", [right[index] for index in sorted(right_indices)])
        for row in aligned:
            row["left_physical_bad"] = bool(left_anomalies.get(row["left_index"], {}).get("bad", False))
            row["right_physical_bad"] = bool(right_anomalies.get(row["right_index"], {}).get("bad", False))

    thresholds: dict[str, dict[str, Any]] = {}
    for key in SERIES_KEYS:
        raw_values = [row[key] for row in aligned if math.isfinite(row[key])]
        limit = filter_limit(raw_values, filter_mode)
        thresholds[key] = {
            "mode": filter_mode,
            "limit_c": safe_float(limit),
            "raw_min_c": safe_float(min(raw_values)) if raw_values else None,
            "raw_max_c": safe_float(max(raw_values)) if raw_values else None,
            "raw_n": len(raw_values),
            "removed": 0,
        }
        for row in aligned:
            value = row[key]
            if filter_mode == "physical":
                channel_bad = row["left_physical_bad"] if key.startswith("left_") else row["right_physical_bad"]
                keep = math.isfinite(value) and not channel_bad
            elif filter_mode == "none":
                keep = math.isfinite(value)
            else:
                keep = math.isfinite(value) and value > 0 and value <= limit
            row[f"{key}_filtered"] = value if keep else float("nan")
        thresholds[key]["removed"] = sum(1 for row in aligned if math.isfinite(row[key]) and not math.isfinite(row[f"{key}_filtered"]))

    metadata_path = session_dir / "session.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}

    sampled = sample_rows(aligned, max_points)
    series = {
        "timestamps": [row["timestamp"].isoformat() for row in sampled],
        "minutes": [row["minutes"] for row in sampled],
        "tasi4": [safe_float(row["tasi4"]) for row in sampled],
    }
    for key in SERIES_KEYS:
        series[key] = [safe_float(row[f"{key}_filtered"]) for row in sampled]

    left_duration = (left[-1].timestamp - left[0].timestamp).total_seconds()
    right_duration = (right[-1].timestamp - right[0].timestamp).total_seconds()
    tasi_duration = (tasi[-1].timestamp - tasi[0].timestamp).total_seconds()

    return {
        "session": {
            "path": str(session_dir),
            "kind": metadata.get("kind"),
            "createdEast8": metadata.get("createdEast8"),
            "filterMode": filter_mode,
            "alignMs": align_ms,
        },
        "counts": {
            "leftRows": len(left),
            "rightRows": len(right),
            "tasiRows": len(tasi),
            "alignedRows": len(aligned),
            "sampledRows": len(sampled),
            "leftPhysicalAnomalyRows": sum(1 for row in aligned if row.get("left_physical_bad")),
            "rightPhysicalAnomalyRows": sum(1 for row in aligned if row.get("right_physical_bad")),
        },
        "rates": {
            "leftFps": (len(left) - 1) / left_duration if left_duration > 0 else None,
            "rightFps": (len(right) - 1) / right_duration if right_duration > 0 else None,
            "tasiHz": (len(tasi) - 1) / tasi_duration if tasi_duration > 0 else None,
        },
        "thresholds": thresholds,
        "stats": {
            "all": summarize_rows(aligned),
        },
        "series": series,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Infrared Capture Report</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #111827;
      --muted: #5b6472;
      --line: #d9dee7;
      --left-min: #93c5fd;
      --left-avg: #2563eb;
      --left-max: #1e3a8a;
      --right-min: #fca5a5;
      --right-avg: #dc2626;
      --right-max: #7f1d1d;
      --tasi: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 22px;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 25px;
      letter-spacing: 0;
    }
    .subtitle {
      color: var(--muted);
      margin: 0 0 18px;
      font-size: 14px;
    }
    .toolbar, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(360px, 1fr) 150px 150px 150px auto;
      gap: 12px;
      align-items: end;
      padding: 14px;
      margin-bottom: 14px;
    }
    label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 6px;
    }
    input, select, button {
      width: 100%;
      height: 36px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font: inherit;
      font-size: 14px;
      background: #fff;
      color: var(--ink);
    }
    input { padding: 0 10px; font-family: "SFMono-Regular", Consolas, monospace; }
    select { padding: 0 8px; }
    button {
      padding: 0 16px;
      background: #111827;
      border-color: #111827;
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled { opacity: .55; cursor: default; }
    .grid {
      display: grid;
      grid-template-columns: 1.35fr .85fr;
      gap: 14px;
      align-items: start;
    }
    .panel { padding: 14px; margin-bottom: 14px; }
    .panel-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 10px;
    }
    .panel-title h2 {
      margin: 0;
      font-size: 18px;
    }
    .hint { color: var(--muted); font-size: 12px; }
    .cards {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 78px;
    }
    .card .label { color: var(--muted); font-size: 12px; margin-bottom: 7px; }
    .card .value { font-size: 22px; font-weight: 760; }
    .card .detail { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .chart-wrap {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 180px;
      gap: 12px;
      align-items: start;
    }
    svg {
      display: block;
      width: 100%;
      height: 560px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .legend {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }
    .legend h3 { margin: 0 0 8px; font-size: 13px; }
    .legend label {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 7px 0;
      color: var(--ink);
      font-size: 13px;
      font-weight: 600;
    }
    .swatch { width: 26px; height: 3px; border-radius: 2px; display: inline-block; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid #e5e7eb;
      padding: 7px 6px;
      text-align: right;
      white-space: nowrap;
    }
    th:first-child, td:first-child { text-align: left; }
    th {
      color: var(--muted);
      font-weight: 700;
      background: #f8fafc;
    }
    .error {
      display: none;
      background: #fff1f2;
      border: 1px solid #fecdd3;
      color: #991b1b;
      padding: 12px;
      border-radius: 8px;
      margin-bottom: 14px;
      font-size: 13px;
      white-space: pre-wrap;
    }
    .footer-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    @media (max-width: 980px) {
      .toolbar, .grid, .chart-wrap, .cards { grid-template-columns: 1fr; }
      svg { height: 460px; }
    }
  </style>
</head>
<body>
<main>
  <h1>Infrared Capture Web Report</h1>
  <p class="subtitle">读取指定采集目录，实时统计 TA612-4 与左右 MLX90640 全帧 min / avg / max。</p>

  <section class="toolbar">
    <div>
      <label for="pathInput">采集数据路径</label>
      <input id="pathInput" />
    </div>
    <div>
      <label for="filterMode">异常过滤</label>
      <select id="filterMode">
        <option value="physical">物理异常</option>
        <option value="p95">raw P95（会裁峰）</option>
        <option value="p99">raw P99（会裁峰）</option>
        <option value="none">不过滤</option>
      </select>
    </div>
    <div>
      <label for="alignMs">对齐窗口</label>
      <select id="alignMs">
        <option value="25">25 ms</option>
        <option value="50">50 ms</option>
        <option value="100">100 ms</option>
      </select>
    </div>
    <div>
      <label for="refreshSec">自动刷新</label>
      <select id="refreshSec">
        <option value="0">关闭</option>
        <option value="2">2 秒</option>
        <option value="5">5 秒</option>
        <option value="10">10 秒</option>
      </select>
    </div>
    <button id="loadBtn">分析</button>
  </section>

  <div id="errorBox" class="error"></div>

  <section class="cards" id="cards"></section>

  <section class="panel">
    <div class="panel-title">
      <h2>趋势</h2>
      <span class="hint" id="updatedAt"></span>
    </div>
    <div class="chart-wrap">
      <svg id="trendChart" role="img" aria-label="temperature trend"></svg>
      <aside class="legend" id="legend"></aside>
    </div>
  </section>

  <div class="grid">
    <section class="panel">
      <div class="panel-title">
        <h2>全程统计</h2>
        <span class="hint">过滤后数值，单位 °C</span>
      </div>
      <div id="statsTable"></div>
    </section>
    <section class="panel">
      <div class="panel-title">
        <h2>过滤阈值</h2>
        <span class="hint">P95/P99 为分位阈值；物理异常按帧剔除</span>
      </div>
      <div id="thresholdTable"></div>
    </section>
  </div>

  <section class="panel footer-note">
    页面不会生成图片文件；趋势图是浏览器内的 SVG。物理异常过滤只剔除包含 &gt;200°C、&lt;0°C 或 NaN 像素的 MLX 帧；P95/P99 会裁掉正常高温峰值，适合离线看异常上界，不适合连续温变趋势。
  </section>
</main>

<script>
const DEFAULT_PATH = "__DEFAULT_SESSION__";
const COLORS = {
  tasi4: getCss("--tasi"),
  left_min: getCss("--left-min"),
  left_avg: getCss("--left-avg"),
  left_max: getCss("--left-max"),
  right_min: getCss("--right-min"),
  right_avg: getCss("--right-avg"),
  right_max: getCss("--right-max")
};
const LABELS = {
  tasi4: "TA612-4",
  left_min: "左 min",
  left_avg: "左 avg",
  left_max: "左 max",
  right_min: "右 min",
  right_avg: "右 avg",
  right_max: "右 max"
};
let currentData = null;
let refreshTimer = null;
let visible = {
  tasi4: true, left_min: true, left_avg: true, left_max: true,
  right_min: true, right_avg: true, right_max: true
};

function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Number(value).toFixed(digits);
}
function byId(id) { return document.getElementById(id); }
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function init() {
  const params = new URLSearchParams(location.search);
  byId("pathInput").value = params.get("path") || DEFAULT_PATH;
  byId("filterMode").value = params.get("filter") || "physical";
  byId("alignMs").value = params.get("alignMs") || "25";
  byId("refreshSec").value = params.get("refresh") || "0";
  byId("loadBtn").addEventListener("click", () => loadReport());
  byId("refreshSec").addEventListener("change", scheduleRefresh);
  renderLegend();
  scheduleRefresh();
  loadReport();
}

function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  const seconds = Number(byId("refreshSec").value);
  if (seconds > 0) refreshTimer = setInterval(loadReport, seconds * 1000);
}

async function loadReport() {
  const button = byId("loadBtn");
  button.disabled = true;
  button.textContent = "分析中";
  byId("errorBox").style.display = "none";
  const path = byId("pathInput").value.trim();
  const query = new URLSearchParams({
    path,
    filter: byId("filterMode").value,
    alignMs: byId("alignMs").value,
    maxPoints: "6000"
  });
  history.replaceState(null, "", `?path=${encodeURIComponent(path)}&filter=${encodeURIComponent(byId("filterMode").value)}&alignMs=${encodeURIComponent(byId("alignMs").value)}&refresh=${encodeURIComponent(byId("refreshSec").value)}`);
  try {
    const response = await fetch(`/api/analyze?${query.toString()}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    currentData = payload;
    renderAll();
  } catch (error) {
    byId("errorBox").textContent = error.message;
    byId("errorBox").style.display = "block";
  } finally {
    button.disabled = false;
    button.textContent = "分析";
  }
}

function renderAll() {
  renderCards();
  renderChart();
  renderStats();
  renderThresholds();
  byId("updatedAt").textContent = `更新 ${currentData.updatedAt}`;
}

function renderCards() {
  const cards = [
    ["对齐样本", currentData.counts.alignedRows, `采样点 ${currentData.counts.sampledRows}`],
    ["左路帧率", fmt(currentData.rates.leftFps, 2), "fps"],
    ["右路帧率", fmt(currentData.rates.rightFps, 2), "fps"],
    ["TA612", fmt(currentData.rates.tasiHz, 3), "Hz"]
  ];
  byId("cards").innerHTML = cards.map(([label, value, detail]) => `
    <div class="card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="detail">${escapeHtml(detail)}</div>
    </div>
  `).join("");
}

function renderLegend() {
  const keys = ["tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"];
  byId("legend").innerHTML = `<h3>图例</h3>` + keys.map(key => `
    <label>
      <input type="checkbox" data-key="${key}" ${visible[key] ? "checked" : ""} />
      <span class="swatch" style="background:${COLORS[key]}"></span>
      ${LABELS[key]}
    </label>
  `).join("");
  byId("legend").querySelectorAll("input").forEach(input => {
    input.addEventListener("change", () => {
      visible[input.dataset.key] = input.checked;
      renderChart();
    });
  });
}

function renderChart() {
  const svg = byId("trendChart");
  const width = 1060, height = 560;
  const margin = { left: 58, right: 18, top: 24, bottom: 48 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const s = currentData.series;
  const keys = ["tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"].filter(key => visible[key]);
  const values = [];
  keys.forEach(key => (s[key] || []).forEach(value => { if (Number.isFinite(value)) values.push(value); }));
  if (!values.length) {
    svg.innerHTML = `<text x="30" y="40">没有可绘制数据</text>`;
    return;
  }
  const xMin = 0;
  const xMax = Math.max(1, Math.ceil(Math.max(...s.minutes)));
  const yMin = Math.floor((Math.min(...values) - 1) / 2) * 2;
  const yMax = Math.ceil((Math.max(...values) + 1) / 2) * 2;
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const x = value => margin.left + (value - xMin) / (xMax - xMin) * plotW;
  const y = value => margin.top + plotH - (value - yMin) / (yMax - yMin) * plotH;
  const lines = [];
  lines.push(`<rect x="${margin.left}" y="${margin.top}" width="${plotW}" height="${plotH}" fill="#ffffff" />`);
  for (let minute = 0; minute <= xMax; minute += 10) {
    lines.push(`<line x1="${x(minute).toFixed(1)}" y1="${margin.top}" x2="${x(minute).toFixed(1)}" y2="${margin.top + plotH}" stroke="#e5e7eb" />`);
    lines.push(`<text x="${x(minute).toFixed(1)}" y="${margin.top + plotH + 24}" text-anchor="middle" font-size="12">${minute}</text>`);
  }
  const yStep = (yMax - yMin) > 20 ? 4 : 2;
  for (let temp = yMin; temp <= yMax; temp += yStep) {
    lines.push(`<line x1="${margin.left}" y1="${y(temp).toFixed(1)}" x2="${margin.left + plotW}" y2="${y(temp).toFixed(1)}" stroke="#e5e7eb" />`);
    lines.push(`<text x="${margin.left - 10}" y="${y(temp) + 4}" text-anchor="end" font-size="12">${temp}</text>`);
  }
  lines.push(`<line x1="${margin.left}" y1="${margin.top + plotH}" x2="${margin.left + plotW}" y2="${margin.top + plotH}" stroke="#374151" />`);
  lines.push(`<line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${margin.top + plotH}" stroke="#374151" />`);
  lines.push(`<text x="${margin.left + plotW / 2}" y="${height - 12}" text-anchor="middle" font-size="12">时间 / min</text>`);
  lines.push(`<text x="18" y="${margin.top + plotH / 2}" transform="rotate(-90 18 ${margin.top + plotH / 2})" text-anchor="middle" font-size="12">温度 / °C</text>`);
  keys.forEach(key => {
    const path = buildPath(s.minutes, s[key], x, y);
    const width = key === "tasi4" || key.endsWith("_max") ? 2.4 : key.endsWith("_avg") ? 2.0 : 1.35;
    lines.push(`<path d="${path}" fill="none" stroke="${COLORS[key]}" stroke-width="${width}" opacity="0.9" />`);
  });
  svg.innerHTML = lines.join("");
}

function buildPath(xs, ys, xScale, yScale) {
  let path = "";
  let drawing = false;
  for (let i = 0; i < xs.length; i++) {
    const yValue = ys[i];
    if (Number.isFinite(yValue)) {
      path += `${drawing ? "L" : "M"}${xScale(xs[i]).toFixed(1)},${yScale(yValue).toFixed(1)} `;
      drawing = true;
    } else {
      drawing = false;
    }
  }
  return path.trim();
}

function renderStats() {
  const stats = currentData.stats.all;
  const rows = ["left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"].map(key => {
    const item = stats.series[key].filtered;
    const removed = stats.series[key].removed;
    return `<tr>
      <td>${LABELS[key]}</td>
      <td>${item.n}</td>
      <td>${removed}</td>
      <td>${fmt(item.min)}</td>
      <td>${fmt(item.mean)}</td>
      <td>${fmt(item.p50)}</td>
      <td>${fmt(item.p95)}</td>
      <td>${fmt(item.max)}</td>
    </tr>`;
  }).join("");
  byId("statsTable").innerHTML = `<table>
    <thead><tr><th>曲线</th><th>n</th><th>过滤</th><th>min</th><th>mean</th><th>P50</th><th>P95</th><th>max</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderThresholds() {
  const rows = ["left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"].map(key => {
    const item = currentData.thresholds[key];
    return `<tr>
      <td>${LABELS[key]}</td>
      <td>${fmt(item.raw_min_c)}</td>
      <td>${fmt(item.limit_c)}</td>
      <td>${fmt(item.raw_max_c)}</td>
      <td>${item.removed}</td>
      <td>${item.raw_n}</td>
    </tr>`;
  }).join("");
  byId("thresholdTable").innerHTML = `<table>
    <thead><tr><th>曲线</th><th>raw min</th><th>阈值</th><th>raw max</th><th>过滤</th><th>n</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

init();
</script>
</body>
</html>
"""


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "InfraredReport/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_html()
            return
        if parsed.path == "/api/analyze":
            self.send_analysis(parsed.query)
            return
        self.send_error(404, "Not found")

    def send_html(self) -> None:
        default_path = quote(str(self.server.default_session))  # type: ignore[attr-defined]
        html = HTML.replace("__DEFAULT_SESSION__", unquote(default_path))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def send_analysis(self, query: str) -> None:
        params = parse_qs(query)
        raw_path = params.get("path", [str(self.server.default_session)])[0]  # type: ignore[attr-defined]
        filter_mode = params.get("filter", ["physical"])[0].lower()
        align_ms = float(params.get("alignMs", ["25"])[0])
        max_points = int(params.get("maxPoints", ["1800"])[0])
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.server.root / path).resolve()  # type: ignore[attr-defined]
        try:
            payload = analyze_session(path, filter_mode, align_ms, max_points)
            self.send_json(payload, 200)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def send_json(self, payload: dict[str, Any], status: int) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve an interactive HTML report for infrared capture sessions.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", default=DEFAULT_SESSION, help="Default capture session path shown in the web UI")
    args = parser.parse_args()

    root = Path.cwd().resolve()
    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    server.root = root  # type: ignore[attr-defined]
    server.default_session = args.session  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/?path={quote(args.session)}"
    print(f"Serving infrared report at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
