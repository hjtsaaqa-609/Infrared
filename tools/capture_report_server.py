#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


DEFAULT_SESSION = "captures/mac_dual_mlx_tasi_20260526_113720"
PHYSICAL_MIN_C = 0.0
PHYSICAL_MAX_C = 200.0
DATA_READY_MASK = 0x0008
SERIES_KEYS = (
    "left_min",
    "left_avg",
    "left_max",
    "right_min",
    "right_avg",
    "right_max",
)
TASI_KEYS = (
    "tasi1",
    "tasi2",
    "tasi3",
    "tasi4",
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


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_session_dir(path: Path) -> bool:
    return (
        (path / "left_mlx_frames.csv").exists()
        and (path / "right_mlx_frames.csv").exists()
        and (path / "tasi_serial_frames.csv").exists()
    )


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
class MlxSubpage:
    index: int
    timestamp: datetime
    subpage: int
    status: int
    control: int
    polls: int


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


def load_mlx_subpages(path: Path) -> list[MlxSubpage]:
    subpages: list[MlxSubpage] = []
    if not path.exists():
        return subpages
    for index, row in enumerate(read_csv(path)):
        try:
            subpages.append(
                MlxSubpage(
                    index=index,
                    timestamp=parse_timestamp(row["timestamp_east8"]),
                    subpage=int(row["subpage"]) & 1,
                    status=int(row["status_register_hex"], 16),
                    control=int(row["control_register_hex"], 16),
                    polls=int(row["polls"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return subpages


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
    if mode in ("none", "physical", "range"):
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
    for key in TASI_KEYS:
        result["series"][key] = {
            "raw": summarize_values([row[key] for row in rows if math.isfinite(row[key])]),
            "filtered": summarize_values([row[key] for row in rows if math.isfinite(row[key])]),
            "removed": 0,
        }
    for key in SERIES_KEYS:
        raw_values = [row[key] for row in rows if math.isfinite(row[key])]
        filtered_values = [row[f"{key}_filtered"] for row in rows if math.isfinite(row[f"{key}_filtered"])]
        result["series"][key] = {
            "raw": summarize_values(raw_values),
            "filtered": summarize_values(filtered_values),
            "removed": len(raw_values) - len(filtered_values),
        }
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


def most_common_control(subpages: list[MlxSubpage]) -> int | None:
    counts: dict[int, int] = {}
    for subpage in subpages:
        counts[subpage.control] = counts.get(subpage.control, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def emitted_raw_indices(subpages: list[MlxSubpage]) -> list[int]:
    mask = 0
    last_subpage = -1
    indices: list[int] = []
    for item in subpages:
        subpage = item.subpage & 1
        mask |= 1 << subpage
        emit = mask == 0b11 and subpage != last_subpage
        last_subpage = subpage
        if emit:
            indices.append(item.index)
    return indices


def stream_quality_stats(
    subpages: list[MlxSubpage],
    frames: list[MlxFrame],
    refresh_rate_hz: float | None,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    if not subpages:
        return {}, {
            "subpages": 0,
            "repeatedSubpages": 0,
            "longGaps": 0,
            "statusBad": 0,
            "controlBad": 0,
            "qualityBadFrames": 0,
        }

    expected_period_s = 1.0 / refresh_rate_hz if refresh_rate_hz and refresh_rate_hz > 0 else None
    control_ref = most_common_control(subpages)
    bad_raw: dict[int, set[str]] = {item.index: set() for item in subpages}

    repeated_count = 0
    long_gap_count = 0
    for index, item in enumerate(subpages):
        if (item.status & DATA_READY_MASK) == 0:
            bad_raw[item.index].add("status")
        if control_ref is not None and item.control != control_ref:
            bad_raw[item.index].add("control")
        if index > 0:
            previous = subpages[index - 1]
            if item.subpage == previous.subpage:
                repeated_count += 1
                bad_raw[item.index].add("repeat")
                bad_raw[previous.index].add("repeat")
            if expected_period_s is not None:
                gap_s = (item.timestamp - previous.timestamp).total_seconds()
                if gap_s > expected_period_s * 1.5:
                    long_gap_count += 1
                    bad_raw[item.index].add("gap")
                    bad_raw[previous.index].add("gap")

    raw_indices = emitted_raw_indices(subpages)
    stats: dict[int, dict[str, Any]] = {}
    for frame in frames:
        raw_index = raw_indices[frame.index] if frame.index < len(raw_indices) else None
        reasons = bad_raw.get(raw_index, {"missing"}) if raw_index is not None else {"missing"}
        stats[frame.index] = {
            "bad": bool(reasons),
            "rawIndex": raw_index,
            "reasons": sorted(reasons),
        }

    return stats, {
        "subpages": len(subpages),
        "repeatedSubpages": repeated_count,
        "longGaps": long_gap_count,
        "statusBad": sum(1 for item in subpages if "status" in bad_raw[item.index]),
        "controlBad": sum(1 for item in subpages if "control" in bad_raw[item.index]),
        "qualityBadFrames": sum(1 for item in frames if stats.get(item.index, {}).get("bad")),
    }


def range_anomaly_stats(session_dir: Path, side: str, frames: list[MlxFrame]) -> dict[int, dict[str, Any]]:
    stats: dict[int, dict[str, Any]] = {}
    if not frames:
        return stats
    for frame in frames:
        has_nan = not all(math.isfinite(value) for value in (frame.min_c, frame.avg_c, frame.max_c))
        has_low = math.isfinite(frame.min_c) and frame.min_c < PHYSICAL_MIN_C
        has_high = math.isfinite(frame.max_c) and frame.max_c > PHYSICAL_MAX_C
        stats[frame.index] = {
            "bad": has_nan or has_low or has_high,
            "lt0": int(has_low),
            "gt200": int(has_high),
            "nan": int(has_nan),
        }
    return stats


def analyze_session(session_dir: Path, filter_mode: str, align_ms: float, max_points: int) -> dict[str, Any]:
    filter_mode = filter_mode.lower()
    if filter_mode not in {"physical", "range", "p95", "p99", "none"}:
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
            "tasi1": tasi_frame.channel1_c,
            "tasi2": tasi_frame.channel2_c,
            "tasi3": tasi_frame.channel3_c,
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

    metadata_path = session_dir / "session.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}

    refresh_rate_hz = metadata.get("refreshRateHz")
    if not isinstance(refresh_rate_hz, (int, float)):
        refresh_rate_hz = None

    left_quality, left_quality_summary = stream_quality_stats(
        load_mlx_subpages(session_dir / "left_mlx_subpages.csv"),
        left,
        refresh_rate_hz,
    )
    right_quality, right_quality_summary = stream_quality_stats(
        load_mlx_subpages(session_dir / "right_mlx_subpages.csv"),
        right,
        refresh_rate_hz,
    )

    left_range: dict[int, dict[str, Any]] = {}
    right_range: dict[int, dict[str, Any]] = {}
    if filter_mode == "range":
        left_indices = {row["left_index"] for row in aligned}
        right_indices = {row["right_index"] for row in aligned}
        left_range = range_anomaly_stats(session_dir, "left", [left[index] for index in sorted(left_indices)])
        right_range = range_anomaly_stats(session_dir, "right", [right[index] for index in sorted(right_indices)])

    for row in aligned:
        row["left_quality_bad"] = bool(left_quality.get(row["left_index"], {}).get("bad", False))
        row["right_quality_bad"] = bool(right_quality.get(row["right_index"], {}).get("bad", False))
        row["left_range_bad"] = bool(left_range.get(row["left_index"], {}).get("bad", False))
        row["right_range_bad"] = bool(right_range.get(row["right_index"], {}).get("bad", False))

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
                channel_bad = row["left_quality_bad"] if key.startswith("left_") else row["right_quality_bad"]
                keep = math.isfinite(value) and not channel_bad
            elif filter_mode == "range":
                channel_bad = row["left_range_bad"] if key.startswith("left_") else row["right_range_bad"]
                keep = math.isfinite(value) and not channel_bad
            elif filter_mode == "none":
                keep = math.isfinite(value)
            else:
                keep = math.isfinite(value) and value <= limit
            row[f"{key}_filtered"] = value if keep else float("nan")
        thresholds[key]["removed"] = sum(1 for row in aligned if math.isfinite(row[key]) and not math.isfinite(row[f"{key}_filtered"]))

    sampled = sample_rows(aligned, max_points)
    series = {
        "timestamps": [row["timestamp"].isoformat() for row in sampled],
        "minutes": [row["minutes"] for row in sampled],
        "tasi1": [safe_float(row["tasi1"]) for row in sampled],
        "tasi2": [safe_float(row["tasi2"]) for row in sampled],
        "tasi3": [safe_float(row["tasi3"]) for row in sampled],
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
            "leftQualityBadRows": sum(1 for row in aligned if row.get("left_quality_bad")),
            "rightQualityBadRows": sum(1 for row in aligned if row.get("right_quality_bad")),
            "leftRangeBadRows": sum(1 for row in aligned if row.get("left_range_bad")),
            "rightRangeBadRows": sum(1 for row in aligned if row.get("right_range_bad")),
        },
        "rates": {
            "leftFps": (len(left) - 1) / left_duration if left_duration > 0 else None,
            "rightFps": (len(right) - 1) / right_duration if right_duration > 0 else None,
            "tasiHz": (len(tasi) - 1) / tasi_duration if tasi_duration > 0 else None,
        },
        "quality": {
            "left": left_quality_summary,
            "right": right_quality_summary,
        },
        "thresholds": thresholds,
        "stats": {
            "all": summarize_rows(aligned),
        },
        "series": series,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def browse_directory(path: Path, browse_root: Path, server_root: Path) -> dict[str, Any]:
    path = path.resolve()
    browse_root = browse_root.resolve()
    server_root = server_root.resolve()
    if not is_within(path, browse_root):
        path = browse_root
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(f"Directory not found: {path}")

    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.name.startswith(".") or not child.is_dir():
            continue
        entries.append(
            {
                "name": child.name,
                "path": safe_relative(child, server_root),
                "isSession": is_session_dir(child),
            }
        )

    parent = path.parent if path != browse_root and is_within(path.parent, browse_root) else None
    return {
        "path": safe_relative(path, server_root),
        "parent": safe_relative(parent, server_root) if parent else None,
        "browseRoot": safe_relative(browse_root, server_root),
        "isSession": is_session_dir(path),
        "entries": entries,
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
      --tasi1: #0f766e;
      --tasi2: #65a30d;
      --tasi3: #047857;
      --tasi4: #111827;
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
      grid-template-columns: minmax(360px, 1fr) 150px 150px 150px auto auto;
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
    .secondary-btn {
      background: #ffffff;
      border-color: #cbd5e1;
      color: var(--ink);
    }
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
    .browser-panel {
      display: none;
      margin-bottom: 14px;
    }
    .browser-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .browser-path {
      color: var(--muted);
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dir-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 8px;
    }
    .dir-item {
      min-height: 42px;
      text-align: left;
      background: #fff;
      color: var(--ink);
      border-color: var(--line);
      padding: 6px 9px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-dir {
      border-color: #047857;
      color: #065f46;
      font-weight: 700;
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
  <p class="subtitle">读取指定采集目录，实时统计 TA612 1/2/3/4 路与左右 MLX90640 全帧 min / avg / max。</p>

  <section class="toolbar">
    <div>
      <label for="pathInput">采集数据路径</label>
      <input id="pathInput" />
    </div>
    <button class="secondary-btn" id="browseBtn">浏览</button>
    <div>
      <label for="filterMode">异常过滤</label>
      <select id="filterMode">
        <option value="physical">采集质量</option>
        <option value="range">温区阈值</option>
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

  <section class="panel browser-panel" id="browserPanel">
    <div class="browser-header">
      <div class="browser-path" id="browserPath"></div>
      <button class="secondary-btn" id="closeBrowserBtn">关闭</button>
    </div>
    <div class="dir-list" id="dirList"></div>
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
        <span class="hint">采集质量按 subpage 流判断；温区阈值按 &lt;0 / &gt;200°C 判断</span>
      </div>
      <div id="thresholdTable"></div>
    </section>
  </div>

  <section class="panel footer-note">
    页面不会生成图片文件；趋势图是浏览器内的 SVG。采集质量过滤不依赖温度数值，而是剔除 subpage 重复、超长间隔、status/control 异常对应的 MLX 帧；温区阈值才使用 &gt;200°C、&lt;0°C 或 NaN 摘要规则；P95/P99 会裁掉正常峰值，适合离线看异常上界，不适合连续温变趋势。
  </section>
</main>

<script>
const DEFAULT_PATH = "__DEFAULT_SESSION__";
const COLORS = {
  tasi1: getCss("--tasi1"),
  tasi2: getCss("--tasi2"),
  tasi3: getCss("--tasi3"),
  tasi4: getCss("--tasi4"),
  left_min: getCss("--left-min"),
  left_avg: getCss("--left-avg"),
  left_max: getCss("--left-max"),
  right_min: getCss("--right-min"),
  right_avg: getCss("--right-avg"),
  right_max: getCss("--right-max")
};
const LABELS = {
  tasi1: "TA612-1",
  tasi2: "TA612-2",
  tasi3: "TA612-3 (MLX)",
  tasi4: "TA612-4 (加热片)",
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
  tasi1: false, tasi2: false, tasi3: true, tasi4: true,
  left_min: true, left_avg: true, left_max: true,
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
  byId("browseBtn").addEventListener("click", () => openBrowser(byId("pathInput").value.trim()));
  byId("closeBrowserBtn").addEventListener("click", () => byId("browserPanel").style.display = "none");
  byId("refreshSec").addEventListener("change", scheduleRefresh);
  renderLegend();
  scheduleRefresh();
  loadReport();
}

async function openBrowser(path) {
  const query = new URLSearchParams({ path: path || "captures" });
  const response = await fetch(`/api/browse?${query.toString()}`);
  const payload = await response.json();
  if (!response.ok) {
    byId("errorBox").textContent = payload.error || response.statusText;
    byId("errorBox").style.display = "block";
    return;
  }
  byId("browserPanel").style.display = "block";
  byId("browserPath").textContent = payload.path;
  const entries = [];
  if (payload.isSession) {
    entries.push(`<button class="dir-item session-dir" data-path="${escapeHtml(payload.path)}" data-session="1">使用当前文件夹  [session]</button>`);
  }
  if (payload.parent) {
    entries.push(`<button class="dir-item" data-path="${escapeHtml(payload.parent)}">..</button>`);
  }
  entries.push(...payload.entries.map(item => `
    <button class="dir-item ${item.isSession ? "session-dir" : ""}" data-path="${escapeHtml(item.path)}" data-session="${item.isSession ? "1" : "0"}" title="${escapeHtml(item.name)}${item.isSession ? "  [session]" : ""}">
      ${escapeHtml(compactDirName(item.name))}${item.isSession ? "  [session]" : ""}
    </button>
  `));
  byId("dirList").innerHTML = entries.join("");
  byId("dirList").querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => {
      const selected = button.dataset.path;
      if (button.dataset.session === "1") {
        byId("pathInput").value = selected;
        byId("browserPanel").style.display = "none";
        loadReport();
      } else {
        openBrowser(selected);
      }
    });
  });
}

function compactDirName(name) {
  const suffixLength = 11;
  const prefixLength = 12;
  if (!name || name.length <= prefixLength + suffixLength + 1) return name;
  return `${name.slice(0, prefixLength)}…${name.slice(-suffixLength)}`;
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
    ["TA612", fmt(currentData.rates.tasiHz, 3), "Hz"],
    ["左质量过滤", currentData.counts.leftQualityBadRows || 0, qualityDetail(currentData.quality.left)],
    ["右质量过滤", currentData.counts.rightQualityBadRows || 0, qualityDetail(currentData.quality.right)]
  ];
  byId("cards").innerHTML = cards.map(([label, value, detail]) => `
    <div class="card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="detail">${escapeHtml(detail)}</div>
    </div>
  `).join("");
}

function qualityDetail(item) {
  if (!item) return "";
  return `repeat ${item.repeatedSubpages || 0}, gap ${item.longGaps || 0}`;
}

function renderLegend() {
  const keys = ["tasi1", "tasi2", "tasi3", "tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"];
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
  const keys = ["tasi1", "tasi2", "tasi3", "tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"].filter(key => visible[key]);
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
    const width = key.startsWith("tasi") || key.endsWith("_max") ? 2.4 : key.endsWith("_avg") ? 2.0 : 1.35;
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
  const rows = ["tasi1", "tasi2", "tasi3", "tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"].map(key => {
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
        if parsed.path == "/api/browse":
            self.send_browse(parsed.query)
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

    def send_browse(self, query: str) -> None:
        params = parse_qs(query)
        raw_path = params.get("path", [str(self.server.browse_root)])[0]  # type: ignore[attr-defined]
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.server.root / path).resolve()  # type: ignore[attr-defined]
        try:
            payload = browse_directory(
                path,
                self.server.browse_root,  # type: ignore[attr-defined]
                self.server.root,  # type: ignore[attr-defined]
            )
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
    parser.add_argument("--browse-root", default="captures", help="Directory tree exposed by the web folder browser")
    args = parser.parse_args()

    root = Path.cwd().resolve()
    browse_root = Path(args.browse_root).expanduser()
    if not browse_root.is_absolute():
        browse_root = (root / browse_root).resolve()
    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    server.root = root  # type: ignore[attr-defined]
    server.default_session = args.session  # type: ignore[attr-defined]
    server.browse_root = browse_root  # type: ignore[attr-defined]
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
