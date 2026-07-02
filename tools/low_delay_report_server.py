#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import struct
import threading
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


DEFAULT_BROWSE_ROOT = "captures"
THERMAL_WIDTH = 32
THERMAL_HEIGHT = 24
THERMAL_PIXELS = THERMAL_WIDTH * THERMAL_HEIGHT
MLX_STARTUP_ARTIFACT_AVG_C = 80.0
MLX_STARTUP_ARTIFACT_MAX_C = 150.0
SESSION_CACHE_LOCK = threading.Lock()
SESSION_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], dict[str, Any]]] = {}
SESSION_RESPONSE_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], bytes, bytes]] = {}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    if not math.isfinite(result):
        return None
    return result


def mean(values: list[float]) -> float | None:
    valid = [value for value in values if math.isfinite(value)]
    if not valid:
        return None
    return sum(valid) / len(valid)


def is_mlx_startup_artifact_row(row: dict[str, str]) -> bool:
    avg_c = parse_float(row.get("mlx_avg_c"))
    max_c = parse_float(row.get("mlx_max_c"))
    return (
        avg_c is not None
        and max_c is not None
        and avg_c >= MLX_STARTUP_ARTIFACT_AVG_C
        and max_c >= MLX_STARTUP_ARTIFACT_MAX_C
    )


def read_temperature_frame(session: Path, channel: str, offset: int | None) -> list[float | None] | None:
    if offset is None or offset < 0:
        return None
    path = session / "temp" / f"{channel}_to.f32le"
    if not path.exists():
        return None
    record_bytes = THERMAL_PIXELS * 4
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(record_bytes)
    if len(data) != record_bytes:
        return None
    values = struct.unpack("<" + "f" * THERMAL_PIXELS, data)
    return [float(value) if math.isfinite(value) else None for value in values]


def window_heatmaps(session: Path, rows: list[dict[str, str]], trigger_indexes: list[str]) -> dict[str, Any] | None:
    by_trigger: dict[str, Any] = {}
    all_values: list[float] = []
    all_positions: list[float] = []
    all_segment_starts: list[float] = []
    all_segment_ends: list[float] = []
    for trigger_index in trigger_indexes:
        channels: dict[str, Any] = {}
        for channel in ("left", "right"):
            candidates = sorted(
                [
                    row
                    for row in rows
                    if row.get("trigger_index") == trigger_index
                    and row.get("mlx_channel") == channel
                    and not is_mlx_startup_artifact_row(row)
                ],
                key=lambda row: parse_float(row.get("trigger_offset_ms")) if parse_float(row.get("trigger_offset_ms")) is not None else math.inf,
            )
            frames: list[dict[str, Any]] = []
            for row in candidates:
                offset = parse_float(row.get("mlx_to_offset_bytes"))
                pixels = read_temperature_frame(session, channel, int(offset) if offset is not None else None)
                if pixels is None:
                    continue
                valid = [value for value in pixels if value is not None]
                all_values.extend(valid)
                position_x_mm = parse_float(row.get("position_x_mm"))
                position_travel_pct = parse_float(row.get("position_travel_pct"))
                position_velocity_mm_s = parse_float(row.get("position_velocity_mm_s"))
                segment_start_mm = parse_float(row.get("segment_start_mm"))
                segment_end_mm = parse_float(row.get("segment_end_mm"))
                segment_center_mm = parse_float(row.get("segment_center_mm"))
                if position_x_mm is not None:
                    all_positions.append(position_x_mm)
                if segment_start_mm is not None:
                    all_segment_starts.append(segment_start_mm)
                if segment_end_mm is not None:
                    all_segment_ends.append(segment_end_mm)
                frames.append(
                    {
                        "channel": channel,
                        "timestamp": row.get("mlx_timestamp_east8", ""),
                        "offsetMs": parse_float(row.get("trigger_offset_ms")),
                        "toOffsetBytes": int(offset) if offset is not None else None,
                        "width": THERMAL_WIDTH,
                        "height": THERMAL_HEIGHT,
                        "min": min(valid) if valid else None,
                        "max": max(valid) if valid else None,
                        "avg": parse_float(row.get("mlx_avg_c")),
                        "center": parse_float(row.get("mlx_center_c")),
                        "positionXMm": position_x_mm,
                        "positionTravelPct": position_travel_pct,
                        "positionVelocityMmS": position_velocity_mm_s,
                        "segmentStartMm": segment_start_mm,
                        "segmentEndMm": segment_end_mm,
                        "segmentCenterMm": segment_center_mm,
                        "pixels": pixels,
                    }
                )
            if not frames:
                continue
            peak_index = max(
                range(len(frames)),
                key=lambda index: frames[index]["max"] if frames[index]["max"] is not None else -math.inf,
            )
            channels[channel] = {
                "channel": channel,
                "width": THERMAL_WIDTH,
                "height": THERMAL_HEIGHT,
                "peakFrameIndex": peak_index,
                "frames": frames,
            }
        if channels:
            by_trigger[trigger_index] = {"triggerIndex": trigger_index, "channels": channels}
    if not by_trigger:
        return None
    observed_min = min(all_values) if all_values else 20.0
    observed_max = max(all_values) if all_values else 60.0
    scale_min = math.floor(min(20.0, observed_min))
    scale_max = math.ceil(max(60.0, observed_max))
    selected = next((index for index in reversed(trigger_indexes) if index in by_trigger), "")
    return {
        "selectedTriggerIndex": selected,
        "triggerIndexes": list(by_trigger.keys()),
        "scale": {"min": scale_min, "max": scale_max, "observedMin": observed_min, "observedMax": observed_max},
        "position": {
            "hasPosition": bool(all_positions),
            "minXMm": min(all_positions) if all_positions else None,
            "maxXMm": max(all_positions) if all_positions else None,
            "travelStartMm": min(all_segment_starts) if all_segment_starts else (min(all_positions) if all_positions else None),
            "travelEndMm": max(all_segment_ends) if all_segment_ends else (max(all_positions) if all_positions else None),
        },
        "byTrigger": by_trigger,
    }


def latest_low_delay_session(root: Path) -> Path | None:
    sessions = sorted(root.glob("mac_dual_mlx_tasi_low_delay_*"), key=lambda path: path.name, reverse=True)
    for session in sessions:
        if (session / "trigger_window_summary.csv").exists():
            return session
    return None


def resolve_session(path_text: str | None, project_root: Path, browse_root: Path) -> Path:
    if path_text:
        session = Path(path_text).expanduser()
        if not session.is_absolute():
            session = (project_root / session).resolve()
    else:
        latest = latest_low_delay_session(browse_root)
        if latest is None:
            raise ValueError(f"No low-delay sessions found under {browse_root}")
        session = latest.resolve()
    if not session.exists() or not session.is_dir():
        raise ValueError(f"Session directory does not exist: {session}")
    return session


def read_session_label(session: Path) -> dict[str, str]:
    path = session / "report_label.json"
    if not path.exists():
        return {"label": "", "note": ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"label": "", "note": ""}
    label = str(payload.get("label") or "").strip()
    note = str(payload.get("note") or "").strip()
    return {"label": label, "note": note}


def write_session_label(session: Path, label: str, note: str = "") -> dict[str, str]:
    label = label.strip()
    note = note.strip()
    path = session / "report_label.json"
    if label or note:
        path.write_text(
            json.dumps({"label": label, "note": note}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif path.exists():
        path.unlink()
    clear_session_cache(session)
    return {"label": label, "note": note}


def clear_session_cache(session: Path) -> None:
    cache_key = str(session.resolve())
    with SESSION_CACHE_LOCK:
        SESSION_CACHE.pop(cache_key, None)
        SESSION_RESPONSE_CACHE.pop(cache_key, None)


def short_session_name(session: Path) -> str:
    prefix = "mac_dual_mlx_tasi_low_delay_"
    name = session.name
    if not name.startswith(prefix):
        return name
    stamp = name.removeprefix(prefix)
    try:
        dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    except ValueError:
        return name
    return dt.strftime("%m-%d %H:%M")


def list_low_delay_sessions(browse_root: Path) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for session in sorted(browse_root.glob("mac_dual_mlx_tasi_low_delay_*"), key=lambda path: path.name, reverse=True):
        if not session.is_dir():
            continue
        events_path = session / "trigger_events.csv"
        summary_path = session / "trigger_window_summary.csv"
        if not events_path.exists() or not summary_path.exists():
            continue
        event_rows = read_csv(events_path)
        summary_rows = read_csv(summary_path)
        first = event_rows[0] if event_rows else {}
        label_info = read_session_label(session)
        short_name = short_session_name(session)
        sessions.append(
            {
                "name": session.name,
                "shortName": short_name,
                "label": label_info["label"],
                "note": label_info["note"],
                "displayName": label_info["label"] or short_name,
                "path": str(session),
                "triggerCount": len(event_rows),
                "windowRows": len(summary_rows),
                "firstTrigger": first.get("trigger_timestamp_east8", ""),
            }
        )
    return sessions


def session_cache_signature(session: Path) -> tuple[tuple[str, int, int], ...]:
    paths = [
        session / "session.json",
        session / "encoder_position.csv",
        session / "trigger_events.csv",
        session / "trigger_window_summary.csv",
        session / "joined_summary.csv",
        session / "tasi_serial_frames.csv",
        session / "temp" / "left_to.f32le",
        session / "temp" / "right_to.f32le",
        session / "report_label.json",
    ]
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            signature.append((str(path.relative_to(session)), -1, -1))
            continue
        signature.append((str(path.relative_to(session)), int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


def cached_session_payload(session: Path) -> dict[str, Any]:
    session = session.resolve()
    cache_key = str(session)
    signature = session_cache_signature(session)
    with SESSION_CACHE_LOCK:
        cached = SESSION_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    payload = session_payload(session)
    with SESSION_CACHE_LOCK:
        SESSION_CACHE[cache_key] = (signature, payload)
        if len(SESSION_CACHE) > 12:
            oldest_key = next(iter(SESSION_CACHE))
            if oldest_key != cache_key:
                SESSION_CACHE.pop(oldest_key, None)
    return payload


def cached_session_response(session: Path) -> tuple[bytes, bytes]:
    session = session.resolve()
    cache_key = str(session)
    signature = session_cache_signature(session)
    with SESSION_CACHE_LOCK:
        cached = SESSION_RESPONSE_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2]
    payload = cached_session_payload(session)
    plain = json.dumps(payload, ensure_ascii=False, default=json_default, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(plain, compresslevel=1) if len(plain) > 1024 else plain
    with SESSION_CACHE_LOCK:
        SESSION_RESPONSE_CACHE[cache_key] = (signature, plain, compressed)
        if len(SESSION_RESPONSE_CACHE) > 12:
            oldest_key = next(iter(SESSION_RESPONSE_CACHE))
            if oldest_key != cache_key:
                SESSION_RESPONSE_CACHE.pop(oldest_key, None)
    return plain, compressed


def session_payload(session: Path) -> dict[str, Any]:
    events = read_csv(session / "trigger_events.csv")
    rows = read_csv(session / "trigger_window_summary.csv")
    joined_rows = read_csv(session / "joined_summary.csv")
    tasi_rows = read_csv(session / "tasi_serial_frames.csv")
    label_info = read_session_label(session)

    by_trigger: dict[str, dict[str, Any]] = {}
    for event in events:
        index = event.get("trigger_index", "")
        by_trigger[index] = {
            "index": index,
            "timestamp": event.get("trigger_timestamp_east8", ""),
            "windowStart": event.get("window_start_east8", ""),
            "windowEnd": event.get("window_end_east8", ""),
            "windowSeconds": parse_float(event.get("window_seconds")),
            "io": event.get("trigger_io", ""),
            "edge": event.get("trigger_edge", ""),
            "reportHex": event.get("trigger_report_hex", ""),
            "series": defaultdict(list),
            "summary": {},
        }

    for row in rows:
        if is_mlx_startup_artifact_row(row):
            continue
        trigger = row.get("trigger_index", "")
        if trigger not in by_trigger:
            by_trigger[trigger] = {
                "index": trigger,
                "timestamp": "",
                "windowStart": "",
                "windowEnd": "",
                "windowSeconds": None,
                "io": "",
                "edge": "",
                "reportHex": "",
                "series": defaultdict(list),
                "summary": {},
            }
        channel = row.get("mlx_channel", "")
        point = {
            "offsetMs": parse_float(row.get("trigger_offset_ms")),
            "timestamp": row.get("mlx_timestamp_east8", ""),
            "avg": parse_float(row.get("mlx_avg_c")),
            "center": parse_float(row.get("mlx_center_c")),
            "min": parse_float(row.get("mlx_min_c")),
            "max": parse_float(row.get("mlx_max_c")),
            "ta": parse_float(row.get("mlx_ta_c")),
            "tasiAgeMs": parse_float(row.get("tasi_age_ms")),
            "tasi": [
                parse_float(row.get("tasi_channel1_c")),
                parse_float(row.get("tasi_channel2_c")),
                parse_float(row.get("tasi_channel3_c")),
                parse_float(row.get("tasi_channel4_c")),
            ],
        }
        if channel:
            by_trigger[trigger]["series"][channel].append(point)

    for trigger in by_trigger.values():
        series = trigger["series"]
        clean_series: dict[str, list[dict[str, Any]]] = {}
        summary: dict[str, Any] = {}
        for channel, points in series.items():
            sorted_points = sorted(
                points,
                key=lambda point: point["offsetMs"] if point["offsetMs"] is not None else float("inf"),
            )
            clean_series[channel] = sorted_points
            avgs = [point["avg"] for point in sorted_points if point["avg"] is not None]
            centers = [point["center"] for point in sorted_points if point["center"] is not None]
            mins = [point["min"] for point in sorted_points if point["min"] is not None]
            maxes = [point["max"] for point in sorted_points if point["max"] is not None]
            summary[channel] = {
                "frames": len(sorted_points),
                "avgMean": mean(avgs),
                "centerMean": mean(centers),
                "minObserved": min(mins) if mins else None,
                "maxObserved": max(maxes) if maxes else None,
            }
        tasi_values: list[list[float]] = [[], [], [], []]
        tasi_ages: list[float] = []
        for points in clean_series.values():
            for point in points:
                if point["tasiAgeMs"] is not None:
                    tasi_ages.append(point["tasiAgeMs"])
                for index, value in enumerate(point["tasi"]):
                    if value is not None:
                        tasi_values[index].append(value)
        summary["tasi"] = {
            "channels": [mean(values) for values in tasi_values],
            "avgMean": mean([value for values in tasi_values for value in values]),
            "ageMeanMs": mean(tasi_ages),
        }
        trigger["series"] = clean_series
        trigger["summary"] = summary

    trigger_list = sorted(
        by_trigger.values(),
        key=lambda trigger: int(trigger["index"]) if str(trigger["index"]).isdigit() else 0,
    )
    trigger_indexes = [str(trigger["index"]) for trigger in trigger_list]
    return {
        "session": str(session),
        "sessionName": session.name,
        "shortName": short_session_name(session),
        "label": label_info["label"],
        "note": label_info["note"],
        "displayName": label_info["label"] or short_session_name(session),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "triggers": len(trigger_list),
            "triggerWindowRows": len(rows),
            "joinedRows": len(joined_rows),
            "tasiRows": len(tasi_rows),
        },
        "triggers": trigger_list,
        "heatmaps": window_heatmaps(session, rows, trigger_indexes),
    }


def json_default(value: Any) -> Any:
    if isinstance(value, defaultdict):
        return dict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>低延迟红外测温报告</title>
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
header {
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
h1 { margin: 0 0 4px; font-size: 25px; font-weight: 700; letter-spacing: 0; }
.subtitle { color: var(--muted); margin: 0 0 14px; font-size: 14px; }
.pathbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.autoRefresh {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
}
.autoRefresh input { width: auto; }
.labelbar {
  display: grid;
  grid-template-columns: auto minmax(220px, 1fr) auto minmax(220px, 1fr) auto auto;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}
.labelInput {
  width: 100%;
}
.sessionSelect {
  width: min(360px, 100%);
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  font: inherit;
}
input {
  width: min(520px, 100%);
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font: inherit;
}
button {
  border: 1px solid var(--line);
  background: #111827;
  border-color: #111827;
  color: #fff;
  border-radius: 6px;
  padding: 8px 12px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
main { max-width: 1480px; margin: 0 auto; padding: 22px; }
.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.metric, .panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
.metric { padding: 12px 14px; }
.label { color: var(--muted); font-size: 12px; }
.value { margin-top: 4px; font-size: 22px; font-weight: 650; }
.panel { padding: 14px; margin-bottom: 14px; }
.panel-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.panel-title h2 { margin: 0; font-size: 18px; }
.hint { color: var(--muted); font-size: 12px; }
.explain {
  margin: 0 0 12px;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f9fafb;
  color: #374151;
  font-size: 13px;
  line-height: 1.5;
}
.method-diagram {
  margin: 0 0 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
  overflow: hidden;
}
.method-diagram svg {
  width: 100%;
  height: auto;
  display: block;
}
.chart-wrap { height: 560px; }
.chart-analysis {
  display: none;
  margin-top: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f9fafb;
  padding: 10px 12px;
  font-size: 13px;
}
.chart-analysis.active { display: block; }
.chart-analysis h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.chart-analysis-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.chart-analysis table {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}
svg { width: 100%; height: 100%; display: block; }
.chart-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 14px;
  align-items: stretch;
}
.legend {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: #fff;
}
.legend h3 { margin: 0 0 8px; font-size: 13px; }
.legend-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.legend-reset {
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 5px;
}
.legend-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.legend-column {
  min-width: 0;
}
.legend-column-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
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
.thermal-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.comparison-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.comparison-block {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: #fff;
}
.comparison-block h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.thermal-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.thermal-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
}
.thermal-title { font-size: 14px; font-weight: 700; }
.thermal-meta { color: var(--muted); font-size: 12px; }
.thermal-canvas {
  width: 100%;
  aspect-ratio: 4 / 3;
  display: block;
  image-rendering: pixelated;
  background: #111827;
}
.thermal-subtitle {
  padding: 9px 12px 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  border-top: 1px solid var(--line);
}
.thermal-scale {
  height: 12px;
  margin: 10px 12px 12px;
  border-radius: 999px;
  background: linear-gradient(90deg, #1e40af 0%, #22d3ee 25%, #eab308 52%, #f97316 74%, #b91c1c 100%);
}
.histogram-canvas {
  width: calc(100% - 24px);
  height: 92px;
  display: block;
  margin: 0 12px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
.panorama-canvas {
  width: 100%;
  display: block;
  image-rendering: pixelated;
  background: #111827;
}
.panorama-meta {
  padding: 0 12px 12px;
  color: var(--muted);
  font-size: 12px;
}
.panorama-speed-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
}
.panorama-speed-paths {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  width: 100%;
}
.panorama-speed-paths label {
  display: grid;
  gap: 4px;
  color: var(--muted);
  font-size: 12px;
}
.panorama-speed-paths input {
  width: 100%;
  min-width: 0;
}
.gaussian-wrap {
  width: 100%;
  min-height: 430px;
}
.gaussian-wrap svg {
  width: 100%;
  height: 430px;
  display: block;
}
.gaussian-note {
  color: var(--muted);
  font-size: 12px;
  margin-top: 8px;
}
.gaussian-toggles {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 10px 0 2px;
  color: var(--muted);
  font-size: 13px;
}
.gaussian-toggles label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}
.gaussian-source-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0 4px;
}
.gaussian-source-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #f8fafc;
  color: #1f2937;
  font-size: 12px;
}
.gaussian-source-chip button {
  border: 0;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0;
}
.thermal-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.segmented {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 7px;
  overflow: hidden;
  background: #fff;
}
.segmented button {
  border: 0;
  border-radius: 0;
  background: #fff;
  color: var(--ink);
  padding: 6px 10px;
  font-size: 13px;
}
.segmented button.active {
  background: #111827;
  color: #fff;
}
.speedSelect {
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  font: inherit;
  font-size: 13px;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 600; }
.muted { color: var(--muted); }
.error { color: #a33a3a; font-weight: 600; }
@media (max-width: 680px) {
  header, main { padding-left: 14px; padding-right: 14px; }
  .labelbar { grid-template-columns: 1fr; }
  .chart-wrap { height: 380px; }
  .chart-grid { grid-template-columns: 1fr; }
  .chart-analysis-grid { grid-template-columns: 1fr; }
  .legend-columns { grid-template-columns: 1fr; }
  .comparison-grid { grid-template-columns: 1fr; }
  .thermal-grid { grid-template-columns: 1fr; }
  .panorama-speed-grid,
  .panorama-speed-paths { grid-template-columns: 1fr; }
  table { font-size: 12px; }
}
</style>
</head>
<body>
<header>
  <h1>低延迟红外测温报告</h1>
  <p class="subtitle">按 IO 触发次数汇总左/右 MLX90640 平均温度与 TA612 四路平均温度。</p>
  <div class="pathbar">
    <span class="label">静止</span>
    <select id="staticSessionSelect" class="sessionSelect" aria-label="static low-delay session list">
      <option value="">正在加载目录...</option>
    </select>
    <input id="staticSessionInput" aria-label="static session path" placeholder="captures/mac_dual_mlx_tasi_low_delay_...">
    <span class="label">滑动</span>
    <select id="movingSessionSelect" class="sessionSelect" aria-label="moving low-delay session list">
      <option value="">正在加载目录...</option>
    </select>
    <input id="movingSessionInput" aria-label="moving session path" placeholder="captures/mac_dual_mlx_tasi_low_delay_...">
    <button id="loadBtn">加载</button>
    <button id="refreshBtn">刷新</button>
    <label class="autoRefresh"><input id="autoRefreshToggle" type="checkbox" checked> 自动 2s</label>
  </div>
  <div class="labelbar">
    <span class="label">静止标注</span>
    <input id="staticLabelInput" class="labelInput" aria-label="static session label" placeholder="例如：静止-90度-32Hz-第1组">
    <button id="saveStaticLabelBtn" type="button">保存静止标注</button>
    <span class="label">滑动标注</span>
    <input id="movingLabelInput" class="labelInput" aria-label="moving session label" placeholder="例如：滑动-90度-32Hz-第1组">
    <button id="saveMovingLabelBtn" type="button">保存滑动标注</button>
  </div>
</header>
<main>
  <div id="status" class="muted">正在加载...</div>
  <section class="summary" id="summary"></section>
  <section class="panel">
    <div class="panel-title">
      <h2>温度检测折线图</h2>
      <div class="thermal-controls">
        <div class="segmented" aria-label="temperature chart mode">
          <button id="chartTriggerModeBtn" type="button" class="active">检测次序</button>
          <button id="chartRowModeBtn" type="button">流程图行</button>
        </div>
        <span class="hint" id="chartModeHint">每次 IO 触发汇总为一个点，单位 °C</span>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-wrap"><svg id="chart" role="img" aria-label="trigger temperature trend chart"></svg></div>
      <aside class="legend" id="legend"></aside>
    </div>
    <div id="chartPeakAnalysis" class="chart-analysis"></div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h2>红外伪彩图</h2>
      <div class="thermal-controls">
        <div class="segmented" aria-label="thermal display mode">
          <button id="thermalPeakBtn" type="button" class="active">温度最高帧</button>
          <button id="thermalPlayBtn" type="button">播放</button>
          <button id="thermalPauseBtn" type="button">暂停</button>
          <button id="thermalManualBtn" type="button">手动</button>
          <button id="thermalLatestBtn" type="button">最新帧</button>
          <button id="thermalRoiBtn" type="button">误差范围框</button>
        </div>
        <select id="thermalSpeed" class="speedSelect" aria-label="thermal animation speed">
          <option value="500">0.5x</option>
          <option value="250">1x</option>
          <option value="125" selected>2x</option>
          <option value="63">4x</option>
        </select>
        <select id="thermalPlayScopeSelect" class="speedSelect" aria-label="thermal animation scope">
          <option value="frames" selected>同一次数播放帧</option>
          <option value="triggers">固定帧按次数播放</option>
        </select>
        <select id="subpageShiftRowsSelect" class="speedSelect" aria-label="subpage correction rows">
          <option value="1" selected>复原下移 1 行</option>
          <option value="2">复原下移 2 行</option>
          <option value="3">复原下移 3 行</option>
        </select>
        <select id="thermalTriggerSelect" class="speedSelect" aria-label="thermal trigger">
          <option value="">最新触发</option>
        </select>
        <select id="thermalFrameSelect" class="speedSelect" aria-label="thermal frame">
          <option value="0">第 1 帧</option>
        </select>
        <span class="hint">循环</span>
        <select id="thermalLoopStartSelect" class="speedSelect" aria-label="thermal loop start frame">
          <option value="0">第 1 帧起</option>
        </select>
        <select id="thermalLoopEndSelect" class="speedSelect" aria-label="thermal loop end frame">
          <option value="0">第 1 帧止</option>
        </select>
        <span class="hint">选择 IO 触发窗口，红色为高温</span>
      </div>
    </div>
    <div class="comparison-grid">
      <div class="comparison-block">
        <h3>静止红外伪彩图</h3>
        <div class="thermal-grid">
          <div class="thermal-card">
            <div class="thermal-head">
              <div class="thermal-title">左侧传感器（静）</div>
              <div class="thermal-meta" id="staticLeftThermalMeta">等待数据...</div>
            </div>
            <canvas id="staticLeftThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
            <canvas id="staticLeftHistogramCanvas" class="histogram-canvas" width="320" height="92"></canvas>
          </div>
          <div class="thermal-card">
            <div class="thermal-head">
              <div class="thermal-title">右侧传感器（静）</div>
              <div class="thermal-meta" id="staticRightThermalMeta">等待数据...</div>
            </div>
            <canvas id="staticRightThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
            <canvas id="staticRightHistogramCanvas" class="histogram-canvas" width="320" height="92"></canvas>
          </div>
        </div>
      </div>
      <div class="comparison-block">
        <h3>滑动红外伪彩图</h3>
        <div class="thermal-grid">
          <div class="thermal-card">
            <div class="thermal-head">
              <div class="thermal-title">左侧传感器（动）</div>
              <div class="thermal-meta" id="movingLeftThermalMeta">等待数据...</div>
            </div>
            <canvas id="movingLeftThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
            <canvas id="movingLeftHistogramCanvas" class="histogram-canvas" width="320" height="92"></canvas>
            <div class="thermal-subtitle">subpage 下移复原版</div>
            <canvas id="movingLeftCorrectedThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
          </div>
          <div class="thermal-card">
            <div class="thermal-head">
              <div class="thermal-title">右侧传感器（动）</div>
              <div class="thermal-meta" id="movingRightThermalMeta">等待数据...</div>
            </div>
            <canvas id="movingRightThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
            <canvas id="movingRightHistogramCanvas" class="histogram-canvas" width="320" height="92"></canvas>
            <div class="thermal-subtitle">subpage 下移复原版</div>
            <canvas id="movingRightCorrectedThermalCanvas" class="thermal-canvas" width="320" height="240"></canvas>
            <div class="thermal-scale"></div>
          </div>
        </div>
      </div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h2>滑动伪彩图失真率统计</h2>
      <div class="thermal-controls">
        <select id="distortionTriggerSelect" class="speedSelect" aria-label="distortion trigger">
          <option value="">共同触发窗口</option>
        </select>
        <select id="distortionFrameSelect" class="speedSelect" aria-label="distortion frame">
          <option value="0">第 1 帧</option>
        </select>
        <button id="addStaticGaussianBtn" type="button">添加当前静止</button>
        <button id="addMovingGaussianBtn" type="button">添加当前滑动</button>
        <button id="clearGaussianSourcesBtn" type="button">清空比较</button>
        <span class="hint">以静止伪彩图为基准，统计滑动伪彩图的像素温差失真率</span>
      </div>
    </div>
    <div class="gaussian-wrap">
      <div id="gaussianSourceList" class="gaussian-source-list"></div>
      <div class="gaussian-toggles" aria-label="gaussian curve toggles">
        <label><input type="checkbox" data-distortion-series="static-left" checked> 静止-左</label>
        <label><input type="checkbox" data-distortion-series="moving-left" checked> 滑动-左</label>
        <label><input type="checkbox" data-distortion-series="static-right" checked> 静止-右</label>
        <label><input type="checkbox" data-distortion-series="moving-right" checked> 滑动-右</label>
      </div>
      <svg id="distortionGaussianChart" role="img" aria-label="sliding pseudocolor gaussian distribution chart"></svg>
      <div id="distortionGaussianNote" class="gaussian-note"></div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h2>拼接红外图</h2>
      <div class="thermal-controls">
        <select id="panoramaTriggerSelect" class="speedSelect" aria-label="panorama trigger">
          <option value="">最新触发</option>
        </select>
        <label class="autoRefresh">最小间隔 <input id="panoramaMinGapInput" type="number" min="1" max="200" step="1" value="19" style="width:72px"> frame</label>
        <label class="autoRefresh">中心 <input id="panoramaAnchorMmInput" type="number" min="0" max="600" step="1" value="300" style="width:76px"> mm</label>
        <label class="autoRefresh">目标间隔 <input id="panoramaStepMmInput" type="number" min="1" max="300" step="1" value="55" style="width:76px"> mm</label>
        <label class="autoRefresh"><input id="panoramaUseCorrectedToggle" type="checkbox"> 使用 subpage 复原版</label>
        <span class="hint">滑动列使用当前滑动目录；速度列按对应数据目录并列对比</span>
        <div class="panorama-speed-paths">
          <label>100mm/s 数据目录
            <input id="panoramaSpeed100Input" class="labelInput" aria-label="100mm/s panorama session path" placeholder="默认使用当前滑动目录">
          </label>
          <label>200mm/s 数据目录
            <input id="panoramaSpeed200Input" class="labelInput" aria-label="200mm/s panorama session path" placeholder="captures/mac_dual_mlx_tasi_low_delay_...">
          </label>
          <label>300mm/s 数据目录
            <input id="panoramaSpeed300Input" class="labelInput" aria-label="300mm/s panorama session path" placeholder="captures/mac_dual_mlx_tasi_low_delay_...">
          </label>
        </div>
      </div>
    </div>
    <div class="panorama-speed-grid">
      <div class="thermal-card">
        <div class="thermal-head">
          <div class="thermal-title">静止拼接图</div>
          <div class="thermal-meta" id="panoramaStaticMeta">等待数据...</div>
        </div>
        <canvas id="panoramaStaticCanvas" class="panorama-canvas" width="320" height="240"></canvas>
        <div class="thermal-scale"></div>
        <div class="panorama-meta" id="panoramaStaticDetail"></div>
      </div>
      <div class="thermal-card">
        <div class="thermal-head">
          <div class="thermal-title">滑动拼接图</div>
          <div class="thermal-meta" id="panoramaMovingMeta">等待数据...</div>
        </div>
        <canvas id="panoramaMovingCanvas" class="panorama-canvas" width="320" height="240"></canvas>
        <div class="thermal-scale"></div>
        <div class="panorama-meta" id="panoramaMovingDetail"></div>
      </div>
      <div class="thermal-card">
        <div class="thermal-head">
          <div class="thermal-title">100mm/s 拼接图</div>
          <div class="thermal-meta" id="panoramaSpeed100Meta">等待数据...</div>
        </div>
        <canvas id="panoramaSpeed100Canvas" class="panorama-canvas" width="320" height="240"></canvas>
        <div class="thermal-scale"></div>
        <div class="panorama-meta" id="panoramaSpeed100Detail"></div>
      </div>
      <div class="thermal-card">
        <div class="thermal-head">
          <div class="thermal-title">200mm/s 拼接图</div>
          <div class="thermal-meta" id="panoramaSpeed200Meta">等待数据...</div>
        </div>
        <canvas id="panoramaSpeed200Canvas" class="panorama-canvas" width="320" height="240"></canvas>
        <div class="thermal-scale"></div>
        <div class="panorama-meta" id="panoramaSpeed200Detail"></div>
      </div>
      <div class="thermal-card">
        <div class="thermal-head">
          <div class="thermal-title">300mm/s 拼接图</div>
          <div class="thermal-meta" id="panoramaSpeed300Meta">等待数据...</div>
        </div>
        <canvas id="panoramaSpeed300Canvas" class="panorama-canvas" width="320" height="240"></canvas>
        <div class="thermal-scale"></div>
        <div class="panorama-meta" id="panoramaSpeed300Detail"></div>
      </div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h2>误差分析</h2>
      <div class="thermal-controls">
        <select id="repeatFrameSelect" class="speedSelect" aria-label="repeatability frame">
          <option value="first4" selected>前 4 帧</option>
        </select>
        <span class="hint">默认只统计前 4 帧；以 Trigger #1 为参照，按最高温 -10°C 的核心热区包围盒中心对齐后，在热源 ROI 内统计绝对温差，单位 °C</span>
      </div>
    </div>
    <p class="explain">把第 1 次测量当标准，只看热源最核心的区域，把后面几次测量的热源位置对齐后，逐格比较温度差，最后看大多数格子的误差有多大。</p>
    <div class="method-diagram" aria-label="误差计算示意图">
      <svg viewBox="0 0 1120 330" role="img">
        <defs>
          <radialGradient id="hotSpot" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stop-color="#b91c1c"/>
            <stop offset="45%" stop-color="#f97316"/>
            <stop offset="72%" stop-color="#facc15"/>
            <stop offset="100%" stop-color="#22d3ee" stop-opacity="0"/>
          </radialGradient>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"></path>
          </marker>
        </defs>
        <rect x="0" y="0" width="1120" height="330" fill="#ffffff"></rect>
        <text x="26" y="34" font-size="18" font-weight="700" fill="#111827">误差计算示意图</text>
        <text x="26" y="58" font-size="13" fill="#5b6472">以第 1 次测量为标准，只比较前 4 帧里的核心热区。</text>

        <g transform="translate(34 88)">
          <rect x="0" y="0" width="190" height="170" rx="8" fill="#f8fafc" stroke="#d9dee7"></rect>
          <text x="18" y="28" font-size="14" font-weight="700" fill="#111827">1. 参照帧</text>
          <text x="18" y="50" font-size="12" fill="#5b6472">Trigger #1 · 当前帧</text>
          <rect x="42" y="68" width="106" height="78" rx="5" fill="#1e40af"></rect>
          <ellipse cx="95" cy="107" rx="43" ry="30" fill="url(#hotSpot)"></ellipse>
          <ellipse cx="95" cy="107" rx="18" ry="13" fill="#b91c1c" opacity="0.92"></ellipse>
          <rect x="75" y="91" width="40" height="32" fill="none" stroke="#fef08a" stroke-width="5"></rect>
          <rect x="75" y="91" width="40" height="32" fill="none" stroke="#111827" stroke-width="1.5"></rect>
          <text x="38" y="160" font-size="12" fill="#374151">最高温 - 10°C 核心热区</text>
        </g>

        <line x1="242" y1="170" x2="322" y2="170" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"></line>

        <g transform="translate(340 88)">
          <rect x="0" y="0" width="210" height="170" rx="8" fill="#f8fafc" stroke="#d9dee7"></rect>
          <text x="18" y="28" font-size="14" font-weight="700" fill="#111827">2. 后续测量</text>
          <text x="18" y="50" font-size="12" fill="#5b6472">Trigger #2 - #N · 同一帧</text>
          <rect x="50" y="68" width="106" height="78" rx="5" fill="#1e40af"></rect>
          <ellipse cx="88" cy="119" rx="43" ry="30" fill="url(#hotSpot)"></ellipse>
          <ellipse cx="88" cy="119" rx="18" ry="13" fill="#b91c1c" opacity="0.92"></ellipse>
          <rect x="68" y="103" width="40" height="32" fill="none" stroke="#fef08a" stroke-width="5"></rect>
          <path d="M 128 92 L 88 92 L 88 103" fill="none" stroke="#111827" stroke-width="1.8" marker-end="url(#arrow)"></path>
          <text x="52" y="160" font-size="12" fill="#374151">热源可能发生偏移 dx/dy</text>
        </g>

        <line x1="568" y1="170" x2="648" y2="170" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"></line>

        <g transform="translate(666 88)">
          <rect x="0" y="0" width="190" height="170" rx="8" fill="#f8fafc" stroke="#d9dee7"></rect>
          <text x="18" y="28" font-size="14" font-weight="700" fill="#111827">3. 位置对齐</text>
          <text x="18" y="50" font-size="12" fill="#5b6472">让核心热区中心重合</text>
          <rect x="42" y="68" width="106" height="78" rx="5" fill="#e0f2fe" stroke="#93c5fd"></rect>
          <rect x="70" y="88" width="42" height="34" fill="none" stroke="#fef08a" stroke-width="5"></rect>
          <rect x="76" y="94" width="42" height="34" fill="none" stroke="#ef4444" stroke-width="3" stroke-dasharray="6 4"></rect>
          <line x1="118" y1="111" x2="112" y2="105" stroke="#111827" stroke-width="2" marker-end="url(#arrow)"></line>
          <text x="32" y="160" font-size="12" fill="#374151">先对齐，再比较</text>
        </g>

        <line x1="874" y1="170" x2="954" y2="170" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"></line>

        <g transform="translate(972 88)">
          <rect x="0" y="0" width="120" height="170" rx="8" fill="#f8fafc" stroke="#d9dee7"></rect>
          <text x="18" y="28" font-size="14" font-weight="700" fill="#111827">4. 统计</text>
          <text x="18" y="56" font-size="12" fill="#374151">逐格温差</text>
          <text x="18" y="78" font-size="12" fill="#374151">|Tn - T1|</text>
          <line x1="18" y1="104" x2="96" y2="104" stroke="#d9dee7"></line>
          <text x="18" y="126" font-size="12" fill="#374151">P50</text>
          <text x="18" y="146" font-size="12" fill="#374151">P90 / P95</text>
        </g>
      </svg>
    </div>
    <div class="comparison-grid">
      <div class="comparison-block">
        <h3>静止</h3>
        <div style="overflow:auto"><table id="staticRepeatabilityTable"></table></div>
      </div>
      <div class="comparison-block">
        <h3>滑动</h3>
        <div style="overflow:auto"><table id="movingRepeatabilityTable"></table></div>
      </div>
    </div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h2>触发数据汇总</h2>
      <div class="segmented" aria-label="summary table data source">
        <button id="summaryStaticBtn" type="button">静止</button>
        <button id="summaryMovingBtn" type="button" class="active">滑动</button>
      </div>
    </div>
    <div style="overflow:auto"><table id="summaryTable"></table></div>
  </section>
</main>
<script>
const state = { data: null, staticData: null, movingData: null, panoramaSpeedData: { "100": null, "200": null, "300": null } };
const qs = new URLSearchParams(location.search);
const initialStaticPath = qs.get("static") || "";
const initialMovingPath = qs.get("moving") || qs.get("path") || "";
const initialPanoramaSpeedPaths = {
  "100": qs.get("speed100") || "",
  "200": qs.get("speed200") || "",
  "300": qs.get("speed300") || "",
};
const staticSessionSelect = document.getElementById("staticSessionSelect");
const staticSessionInput = document.getElementById("staticSessionInput");
const staticLabelInput = document.getElementById("staticLabelInput");
const movingSessionSelect = document.getElementById("movingSessionSelect");
const movingSessionInput = document.getElementById("movingSessionInput");
const movingLabelInput = document.getElementById("movingLabelInput");
const autoRefreshToggle = document.getElementById("autoRefreshToggle");
const thermalPeakBtn = document.getElementById("thermalPeakBtn");
const thermalPlayBtn = document.getElementById("thermalPlayBtn");
const thermalPauseBtn = document.getElementById("thermalPauseBtn");
const thermalManualBtn = document.getElementById("thermalManualBtn");
const thermalLatestBtn = document.getElementById("thermalLatestBtn");
const thermalRoiBtn = document.getElementById("thermalRoiBtn");
const thermalSpeed = document.getElementById("thermalSpeed");
const thermalPlayScopeSelect = document.getElementById("thermalPlayScopeSelect");
const subpageShiftRowsSelect = document.getElementById("subpageShiftRowsSelect");
const thermalTriggerSelect = document.getElementById("thermalTriggerSelect");
const thermalFrameSelect = document.getElementById("thermalFrameSelect");
const thermalLoopStartSelect = document.getElementById("thermalLoopStartSelect");
const thermalLoopEndSelect = document.getElementById("thermalLoopEndSelect");
const distortionTriggerSelect = document.getElementById("distortionTriggerSelect");
const distortionFrameSelect = document.getElementById("distortionFrameSelect");
const distortionSeriesInputs = Array.from(document.querySelectorAll("[data-distortion-series]"));
const addStaticGaussianBtn = document.getElementById("addStaticGaussianBtn");
const addMovingGaussianBtn = document.getElementById("addMovingGaussianBtn");
const clearGaussianSourcesBtn = document.getElementById("clearGaussianSourcesBtn");
const gaussianSourceList = document.getElementById("gaussianSourceList");
const panoramaTriggerSelect = document.getElementById("panoramaTriggerSelect");
const panoramaMinGapInput = document.getElementById("panoramaMinGapInput");
const panoramaAnchorMmInput = document.getElementById("panoramaAnchorMmInput");
const panoramaStepMmInput = document.getElementById("panoramaStepMmInput");
const panoramaUseCorrectedToggle = document.getElementById("panoramaUseCorrectedToggle");
const panoramaSpeedInputs = {
  "100": document.getElementById("panoramaSpeed100Input"),
  "200": document.getElementById("panoramaSpeed200Input"),
  "300": document.getElementById("panoramaSpeed300Input"),
};
const repeatFrameSelect = document.getElementById("repeatFrameSelect");
const summaryStaticBtn = document.getElementById("summaryStaticBtn");
const summaryMovingBtn = document.getElementById("summaryMovingBtn");
const chartTriggerModeBtn = document.getElementById("chartTriggerModeBtn");
const chartRowModeBtn = document.getElementById("chartRowModeBtn");
const chartModeHint = document.getElementById("chartModeHint");
const chartPeakAnalysis = document.getElementById("chartPeakAnalysis");
staticSessionInput.value = initialStaticPath;
movingSessionInput.value = initialMovingPath;
panoramaSpeedInputs["100"].value = initialPanoramaSpeedPaths["100"];
panoramaSpeedInputs["200"].value = initialPanoramaSpeedPaths["200"];
panoramaSpeedInputs["300"].value = initialPanoramaSpeedPaths["300"];
const SERIES_KEYS = ["tasi1", "tasi2", "tasi3", "tasi4", "left_min", "left_avg", "left_max", "right_min", "right_avg", "right_max"];
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
  right_max: getCss("--right-max"),
};
const LABELS = {
  tasi1: "TA612-1",
  tasi2: "TA612-2",
  tasi3: "TA612-3",
  tasi4: "TA612-4",
  left_min: "左 min",
  left_avg: "左 avg",
  left_max: "左 max",
  right_min: "右 min",
  right_avg: "右 avg",
  right_max: "右 max",
};
let visible = {
  "static:tasi1": false,
  "static:tasi2": false,
  "static:tasi3": true,
  "static:tasi4": true,
  "static:left_min": true,
  "static:left_avg": true,
  "static:left_max": true,
  "static:right_min": true,
  "static:right_avg": true,
  "static:right_max": true,
  "moving:tasi1": false,
  "moving:tasi2": false,
  "moving:tasi3": true,
  "moving:tasi4": true,
  "moving:left_min": true,
  "moving:left_avg": true,
  "moving:left_max": true,
  "moving:right_min": true,
  "moving:right_avg": true,
  "moving:right_max": true,
};
let legendHiddenMemory = { static: null, moving: null };
let refreshTimer = null;
let animationTimer = null;
let loading = false;
let lastCounts = null;
let thermalMode = "peak";
let chartMode = "trigger";
let showThermalRoi = false;
let thermalPlayScope = "frames";
let summaryTableMode = "moving";
let animationFrameIndex = 0;
let manualFrameIndex = 0;
let lastHeatmapTriggerIndex = null;
let selectedThermalTrigger = "";
let lastThermalTriggerOptionsKey = "";
let lastThermalFrameOptionsKey = "";
let lastThermalLoopOptionsKey = "";
let distortionDatasets = [];
let selectedPanoramaTrigger = "";
let lastPanoramaTriggerOptionsKey = "";
let thermalLoopRangeInitialized = false;
let lastAutoRefreshSkip = 0;
let lastRepeatFrameOptionsKey = "";
let repeatabilityStatsCache = new Map();
let chartRowCache = new WeakMap();
let deferredRenderTimer = null;
let deferredRenderToken = 0;
let autoRefreshInFlight = false;

function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmt(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : "";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function avg(values) {
  const valid = values.filter(Number.isFinite);
  return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : NaN;
}

async function fetchSession(path) {
  const url = "/api/session" + (path ? "?path=" + encodeURIComponent(path) : "");
  const res = await fetch(url, { cache: "no-store" });
  const payload = await res.json();
  if (!res.ok) throw new Error(payload.error || "Failed to load session");
  return payload;
}

async function saveSessionLabel(path, label) {
  if (!path) throw new Error("请先选择一个数据目录");
  const res = await fetch("/api/session-label", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, label }),
  });
  const payload = await res.json();
  if (!res.ok) throw new Error(payload.error || "保存标注失败");
  return payload;
}

async function loadData(options = {}) {
  if (loading) return;
  loading = true;
  const staticPath = staticSessionInput.value.trim();
  const movingPath = movingSessionInput.value.trim();
  const speedPaths = {
    "100": panoramaSpeedInputs["100"].value.trim(),
    "200": panoramaSpeedInputs["200"].value.trim(),
    "300": panoramaSpeedInputs["300"].value.trim(),
  };
  if (!options.silent) document.getElementById("status").textContent = "正在加载...";
  try {
    const [staticData, movingData] = await Promise.all([
      staticPath ? fetchSession(staticPath) : Promise.resolve(null),
      movingPath ? fetchSession(movingPath) : fetchSession(""),
    ]);
    const panoramaSpeedData = {};
    for (const speed of ["100", "200", "300"]) {
      const path = speedPaths[speed];
      if (path) {
        panoramaSpeedData[speed] = await fetchSession(path);
      } else {
        panoramaSpeedData[speed] = null;
      }
    }
    state.staticData = staticData;
    state.movingData = movingData;
    state.panoramaSpeedData = panoramaSpeedData;
    state.data = state.movingData || state.staticData;
    if (summaryTableMode === "moving" && !state.movingData && state.staticData) summaryTableMode = "static";
    if (summaryTableMode === "static" && !state.staticData && state.movingData) summaryTableMode = "moving";
    repeatabilityStatsCache.clear();
    chartRowCache = new WeakMap();
    if (state.staticData) staticSessionInput.value = state.staticData.session;
    if (state.movingData) movingSessionInput.value = state.movingData.session;
    if (document.activeElement !== staticLabelInput) {
      staticLabelInput.value = state.staticData?.label || "";
    }
    if (document.activeElement !== movingLabelInput) {
      movingLabelInput.value = state.movingData?.label || "";
    }
    const params = new URLSearchParams();
    if (staticSessionInput.value.trim()) params.set("static", staticSessionInput.value.trim());
    if (movingSessionInput.value.trim()) params.set("moving", movingSessionInput.value.trim());
    if (panoramaSpeedInputs["100"].value.trim()) params.set("speed100", panoramaSpeedInputs["100"].value.trim());
    if (panoramaSpeedInputs["200"].value.trim()) params.set("speed200", panoramaSpeedInputs["200"].value.trim());
    if (panoramaSpeedInputs["300"].value.trim()) params.set("speed300", panoramaSpeedInputs["300"].value.trim());
    history.replaceState(null, "", "?" + params.toString());
    render();
  } finally {
    loading = false;
  }
}

async function loadSessionList() {
  const res = await fetch("/api/sessions", { cache: "no-store" });
  const payload = await res.json();
  if (!res.ok) throw new Error(payload.error || "读取数据目录失败");
  const options = payload.sessions.map(item => {
    const detail = `${item.displayName || item.shortName || item.name} · ${item.triggerCount}次 · rows=${item.windowRows}`;
    return `<option value="${escapeHtml(item.path)}">${escapeHtml(detail)}</option>`;
  }).join("");
  staticSessionSelect.innerHTML = `<option value="">选择静止目录...</option>${options}`;
  movingSessionSelect.innerHTML = `<option value="">选择滑动目录...</option>${options}`;
  if (staticSessionInput.value) {
    const current = [...staticSessionSelect.options].find(option => option.value === staticSessionInput.value);
    if (current) staticSessionSelect.value = current.value;
  }
  if (movingSessionInput.value) {
    const current = [...movingSessionSelect.options].find(option => option.value === movingSessionInput.value);
    if (current) movingSessionSelect.value = current.value;
  } else if (movingSessionSelect.options.length > 1) {
    movingSessionSelect.selectedIndex = 1;
    movingSessionInput.value = movingSessionSelect.value;
  }
}

function render() {
  const data = state.data;
  const counts = data.counts || {};
  const changed = !lastCounts || counts.triggers !== lastCounts.triggers || counts.triggerWindowRows !== lastCounts.triggerWindowRows;
  lastCounts = { triggers: counts.triggers, triggerWindowRows: counts.triggerWindowRows };
  const liveText = autoRefreshToggle.checked ? "自动刷新开启" : "自动刷新关闭";
  const changedText = changed ? "已更新" : "暂无新数据";
  const staticName = state.staticData?.displayName || state.staticData?.sessionName || "未选择静止目录";
  const movingName = state.movingData?.displayName || state.movingData?.sessionName || "未选择滑动目录";
  const currentName = data.displayName || data.sessionName;
  document.getElementById("status").textContent = `静止: ${staticName} · 滑动: ${movingName} · 当前显示: ${currentName} · ${liveText} · ${changedText}`;
  const summaryItems = [];
  [
    ["静止", state.staticData],
    ["滑动", state.movingData],
  ].forEach(([label, item]) => {
    const itemCounts = item?.counts || {};
    summaryItems.push([`${label} Triggers`, itemCounts.triggers ?? "-"]);
    summaryItems.push([`${label} Window Rows`, itemCounts.triggerWindowRows ?? "-"]);
    summaryItems.push([`${label} TA612 Rows`, itemCounts.tasiRows ?? "-"]);
  });
  document.getElementById("summary").innerHTML = summaryItems.map(([label, value]) => `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");

  renderChart();
  renderLegend();
  renderTable();
  scheduleDeferredPanels();
}

function scheduleDeferredPanels() {
  deferredRenderToken += 1;
  const token = deferredRenderToken;
  if (deferredRenderTimer) {
    clearTimeout(deferredRenderTimer);
    deferredRenderTimer = null;
  }
  deferredRenderTimer = setTimeout(() => {
    requestAnimationFrame(() => {
      if (token !== deferredRenderToken) return;
      renderHeatmaps();
      renderDistortion();
      renderPanorama();
      setTimeout(() => {
        if (token !== deferredRenderToken) return;
        renderRepeatability();
      }, 0);
    });
  }, 60);
}

function triggerRows(data) {
  return (data?.triggers || []).map(t => {
    const left = t.summary?.left || {};
    const right = t.summary?.right || {};
    const tasi = t.summary?.tasi || {};
    return {
      index: t.index,
      timestamp: t.timestamp,
      x: Number(t.index),
      left_min: left.minObserved,
      left_avg: left.avgMean,
      left_max: left.maxObserved,
      right_min: right.minObserved,
      right_avg: right.avgMean,
      right_max: right.maxObserved,
      tasi1: (tasi.channels || [])[0],
      tasi2: (tasi.channels || [])[1],
      tasi3: (tasi.channels || [])[2],
      tasi4: (tasi.channels || [])[3],
      leftFrames: left.frames || 0,
      rightFrames: right.frames || 0,
      tasiAgeMs: tasi.ageMeanMs,
    };
  });
}

function rowPhysicalMm(frame, rawRowIndex) {
  const start = Number(frame?.segmentStartMm);
  const end = Number(frame?.segmentEndMm);
  const height = Number(frame?.height) || 24;
  if (Number.isFinite(start) && Number.isFinite(end) && height > 0) {
    return start + (height - rawRowIndex - 0.5) * (end - start) / height;
  }
  const center = Number(frame?.positionXMm ?? frame?.segmentCenterMm);
  const width = Number.isFinite(start) && Number.isFinite(end) ? Math.abs(end - start) : 110;
  if (Number.isFinite(center) && width > 0 && height > 0) {
    return center - width / 2 + (height - rawRowIndex - 0.5) * width / height;
  }
  return NaN;
}

function rowThermalRows(data) {
  if (!data) return [];
  const cached = chartRowCache.get(data);
  if (cached) return cached;
  const heatmaps = data?.heatmaps;
  const byTrigger = heatmaps?.byTrigger || {};
  const triggerIndexes = (data?.triggers || [])
    .map(trigger => String(trigger.index))
    .filter(index => byTrigger[index])
    .sort((a, b) => {
      const frameA = byTrigger[a]?.channels?.left?.frames?.[0] || byTrigger[a]?.channels?.right?.frames?.[0] || {};
      const frameB = byTrigger[b]?.channels?.left?.frames?.[0] || byTrigger[b]?.channels?.right?.frames?.[0] || {};
      const posA = framePositionMm(frameA);
      const posB = framePositionMm(frameB);
      if (Number.isFinite(posA) && Number.isFinite(posB) && posA !== posB) return posA - posB;
      return Number(a) - Number(b);
    });
  const rows = [];
  triggerIndexes.forEach(triggerIndex => {
    const trigger = byTrigger[triggerIndex];
    const rowStatsByChannel = {};
    const rowMmByChannel = {};
    ["left", "right"].forEach(channel => {
      const heatmap = trigger.channels?.[channel];
      const frames = heatmap?.frames || [];
      if (!frames.length) return;
      const frame = frames[Math.max(0, Math.min(frames.length - 1, Number(heatmap.peakFrameIndex) || 0))];
      const pixels = frame?.pixels || [];
      if (pixels.length < 32 * 24) return;
      rowMmByChannel[channel] = Array.from({ length: 24 }, (_, rawRowIndex) => rowPhysicalMm(frame, rawRowIndex));
      rowStatsByChannel[channel] = Array.from({ length: 24 }, (_, rowIndex) => {
        const values = pixels.slice(rowIndex * 32, rowIndex * 32 + 32).filter(Number.isFinite);
        if (!values.length) return { min: NaN, avg: NaN, max: NaN };
        return {
          min: Math.min(...values),
          avg: values.reduce((sum, value) => sum + value, 0) / values.length,
          max: Math.max(...values),
        };
      });
    });
    for (let visualRowIndex = 0; visualRowIndex < 24; visualRowIndex += 1) {
      const rawRowIndex = 23 - visualRowIndex;
      const left = rowStatsByChannel.left?.[rawRowIndex] || {};
      const right = rowStatsByChannel.right?.[rawRowIndex] || {};
      const leftMm = rowMmByChannel.left?.[rawRowIndex];
      const rightMm = rowMmByChannel.right?.[rawRowIndex];
      const rowMm = Number.isFinite(leftMm) ? leftMm : rightMm;
      rows.push({
        index: `${triggerIndex}-${visualRowIndex + 1}`,
        triggerIndex,
        rowIndex: visualRowIndex + 1,
        rawRowIndex: rawRowIndex + 1,
        x: rows.length + 1,
        rowMm,
        left_row_mm: leftMm,
        right_row_mm: rightMm,
        left_min: left.min,
        left_avg: left.avg,
        left_max: left.max,
        right_min: right.min,
        right_avg: right.avg,
        right_max: right.max,
      });
    }
  });
  chartRowCache.set(data, rows);
  return rows;
}

function renderLegend() {
  const groups = [
    { id: "static", name: "静", dash: "repeating-linear-gradient(90deg, currentColor 0 8px, transparent 8px 13px)" },
    { id: "moving", name: "动", dash: "currentColor" },
  ];
  document.getElementById("legend").innerHTML = `
    <div class="legend-head">
      <h3>图例</h3>
      <button id="legendResetBtn" type="button" class="legend-reset">重置</button>
    </div>
    <div class="hint">每列顶部可一键隐藏；再次打开会恢复隐藏前的勾选</div>
    <div class="legend-columns">` + groups.map(group => {
    const groupKeys = SERIES_KEYS.map(key => `${group.id}:${key}`);
    const checkedCount = groupKeys.filter(key => visible[key]).length;
    return `
    <div class="legend-column">
      <div class="legend-column-title">
        <span>${group.name}</span>
        <label style="margin:0;">
          <input type="checkbox" data-group-toggle="${group.id}" ${checkedCount ? "checked" : ""}>
          全部
        </label>
      </div>
      ${SERIES_KEYS.map(key => {
      const visibleKey = `${group.id}:${key}`;
      const swatchStyle = group.id === "static"
        ? `color:${COLORS[key]};background:${group.dash}`
        : `background:${COLORS[key]}`;
      return `
        <label>
          <input type="checkbox" data-key="${visibleKey}" ${visible[visibleKey] ? "checked" : ""}>
          <span class="swatch" style="${swatchStyle}"></span>
          ${LABELS[key]}（${group.name}）
        </label>
      `;
      }).join("")}
    </div>
  `;
  }).join("") + `</div>`;
  document.getElementById("legendResetBtn").addEventListener("click", () => {
    ["static", "moving"].forEach(groupId => {
      SERIES_KEYS.forEach(key => {
        visible[`${groupId}:${key}`] = true;
      });
      legendHiddenMemory[groupId] = null;
    });
    renderLegend();
    renderChart();
  });
  document.querySelectorAll("#legend input[data-group-toggle]").forEach(input => {
    const groupId = input.dataset.groupToggle;
    const groupKeys = SERIES_KEYS.map(key => `${groupId}:${key}`);
    const checkedCount = groupKeys.filter(key => visible[key]).length;
    input.indeterminate = checkedCount > 0 && checkedCount < groupKeys.length;
    input.addEventListener("change", () => {
      if (input.checked) {
        const remembered = legendHiddenMemory[groupId];
        groupKeys.forEach(key => {
          visible[key] = remembered ? Boolean(remembered[key]) : true;
        });
      } else {
        legendHiddenMemory[groupId] = Object.fromEntries(groupKeys.map(key => [key, visible[key]]));
        groupKeys.forEach(key => {
          visible[key] = false;
        });
      }
      renderLegend();
      renderChart();
    });
  });
  document.querySelectorAll("#legend input[data-key]").forEach(input => {
    input.addEventListener("change", () => {
      visible[input.dataset.key] = input.checked;
      renderLegend();
      renderChart();
    });
  });
}

function chartRowMmPerPixel(datasets) {
  const diffs = [];
  datasets.forEach(dataset => {
    for (let index = 1; index < dataset.rows.length; index += 1) {
      const prev = dataset.rows[index - 1];
      const row = dataset.rows[index];
      if (prev.triggerIndex !== row.triggerIndex) continue;
      const diff = Math.abs(Number(row.rowMm) - Number(prev.rowMm));
      if (Number.isFinite(diff) && diff > 0) diffs.push(diff);
    }
  });
  return diffs.length ? diffs.reduce((sum, value) => sum + value, 0) / diffs.length : 110 / 24;
}

function chartMaxPeaks(datasets) {
  const maxKeys = ["left_max", "right_max"];
  const peaks = [];
  datasets.forEach(dataset => {
    maxKeys.forEach(key => {
      if (!visible[`${dataset.id}:${key}`]) return;
      const points = dataset.rows
        .map(row => ({
          datasetId: dataset.id,
          datasetName: dataset.name,
          key,
          label: `${LABELS[key]}（${dataset.name}）`,
          x: Number(row.x),
          rowMm: Number(row[`${key.startsWith("left") ? "left" : "right"}_row_mm`] ?? row.rowMm),
          temp: Number(row[key]),
          triggerIndex: row.triggerIndex,
          rowIndex: row.rowIndex,
        }))
        .filter(point => Number.isFinite(point.x) && Number.isFinite(point.temp));
      if (!points.length) return;
      peaks.push(points.reduce((best, point) => point.temp > best.temp ? point : best, points[0]));
    });
  });
  return peaks;
}

function renderChartPeakAnalysis(datasets) {
  if (!chartPeakAnalysis) return;
  if (chartMode !== "rows") {
    chartPeakAnalysis.classList.remove("active");
    chartPeakAnalysis.innerHTML = "";
    return;
  }
  const peaks = chartMaxPeaks(datasets);
  if (!peaks.length) {
    chartPeakAnalysis.classList.add("active");
    chartPeakAnalysis.innerHTML = `<h3>max 峰值分析</h3><div class="muted">当前流程图行模式没有可分析的 max 曲线。</div>`;
    return;
  }
  const mmPerPixel = chartRowMmPerPixel(datasets);
  const pairs = [];
  for (let i = 0; i < peaks.length; i += 1) {
    for (let j = i + 1; j < peaks.length; j += 1) {
      const a = peaks[i];
      const b = peaks[j];
      const pixelDiff = Math.abs(a.x - b.x);
      const physicalDiff = Number.isFinite(a.rowMm) && Number.isFinite(b.rowMm)
        ? Math.abs(a.rowMm - b.rowMm)
        : pixelDiff * mmPerPixel;
      pairs.push({ a, b, tempDiff: Math.abs(a.temp - b.temp), pixelDiff, physicalDiff });
    }
  }
  const peakRows = peaks
    .sort((a, b) => b.temp - a.temp)
    .map(peak => `
      <tr>
        <td>${escapeHtml(peak.label)}</td>
        <td>${fmt(peak.temp)} °C</td>
        <td>${fmt(peak.x, 0)} px</td>
        <td>${fmt(peak.rowMm, 1)} mm</td>
        <td>#${escapeHtml(peak.triggerIndex)} / 行 ${fmt(peak.rowIndex, 0)}</td>
      </tr>
    `).join("");
  const pairRows = pairs.length ? pairs
    .sort((a, b) => b.tempDiff - a.tempDiff)
    .map(pair => `
      <tr>
        <td>${escapeHtml(pair.a.label)} ↔ ${escapeHtml(pair.b.label)}</td>
        <td>${fmt(pair.tempDiff)} °C</td>
        <td>${fmt(pair.pixelDiff, 0)} px</td>
        <td>${fmt(pair.physicalDiff, 1)} mm</td>
      </tr>
    `).join("") : `<tr><td colspan="4" class="muted">当前只有一条 max 曲线，暂无曲线间差值。</td></tr>`;
  chartPeakAnalysis.classList.add("active");
  chartPeakAnalysis.innerHTML = `
    <h3>max 峰值分析</h3>
    <div class="hint">x 轴像素差按拼接红外图底部→顶部的流程图行计算；毫米差优先使用每帧 segment_start/end，缺失时按 ${fmt(mmPerPixel, 2)} mm/px 估算。</div>
    <div class="chart-analysis-grid">
      <div>
        <div class="label">每条 max 曲线最高点</div>
        <div style="overflow:auto">
          <table>
            <thead><tr><th>曲线</th><th>最高温</th><th>x 坐标</th><th>物理位置</th><th>来源</th></tr></thead>
            <tbody>${peakRows}</tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="label">max 峰值之间差距</div>
        <div style="overflow:auto">
          <table>
            <thead><tr><th>对比</th><th>最高温温差</th><th>x 像素差</th><th>约 mm 差</th></tr></thead>
            <tbody>${pairRows}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

function renderChart() {
  const svg = document.getElementById("chart");
  chartTriggerModeBtn.classList.toggle("active", chartMode === "trigger");
  chartRowModeBtn.classList.toggle("active", chartMode === "rows");
  chartModeHint.textContent = chartMode === "rows"
    ? "按拼接红外图从底部到顶部逐行统计 32 个像素的 min/avg/max"
    : "每次 IO 触发汇总为一个点，单位 °C";
  const rowBuilder = chartMode === "rows" ? rowThermalRows : triggerRows;
  const datasets = [
    { id: "static", name: "静", data: state.staticData, dash: "6 5", opacity: 0.78 },
    { id: "moving", name: "动", data: state.movingData, dash: "", opacity: 0.94 },
  ].map(item => ({ ...item, rows: rowBuilder(item.data) })).filter(item => item.rows.length);
  renderChartPeakAnalysis(datasets);
  if (!datasets.length) {
    svg.innerHTML = "";
    return;
  }
  const values = datasets.flatMap(dataset => {
    const keys = SERIES_KEYS.filter(key => visible[`${dataset.id}:${key}`] && (chartMode === "trigger" || !key.startsWith("tasi")));
    return dataset.rows.flatMap(row => keys.map(key => row[key]));
  }).filter(Number.isFinite);
  if (!values.length) {
    svg.innerHTML = `<text x="24" y="42" fill="#5b6472">没有可绘制数据</text>`;
    return;
  }
  const width = 1280, height = 680;
  const margin = { left: 66, right: 22, top: 30, bottom: 62 };
  const xValues = datasets.flatMap(dataset => dataset.rows.map(row => row.x)).filter(Number.isFinite);
  const xMin = Math.min(...xValues);
  const xMax = Math.max(...xValues);
  const yMinRaw = Math.min(...values);
  const yMaxRaw = Math.max(...values);
  const yMin = Math.floor((yMinRaw - 0.6) / 1) * 1;
  const yMax = Math.ceil((yMaxRaw + 0.6) / 1) * 1;
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const x = value => margin.left + (value - xMin) / (xMax - xMin || 1) * plotW;
  const y = value => margin.top + plotH - (value - yMin) / (yMax - yMin || 1) * plotH;
  const lines = [];
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  lines.push(`<rect x="${margin.left}" y="${margin.top}" width="${plotW}" height="${plotH}" fill="#ffffff"></rect>`);
  if (chartMode === "trigger") {
    [...new Set(xValues)].sort((a, b) => a - b).forEach(value => {
      lines.push(`<line x1="${x(value).toFixed(1)}" y1="${margin.top}" x2="${x(value).toFixed(1)}" y2="${margin.top + plotH}" stroke="#eef2f7"></line>`);
      lines.push(`<text x="${x(value).toFixed(1)}" y="${margin.top + plotH + 24}" text-anchor="middle" font-size="12">#${value}</text>`);
    });
  } else {
    const rowMax = Math.max(...xValues);
    for (let value = 1; value <= rowMax; value += 24) {
      lines.push(`<line x1="${x(value).toFixed(1)}" y1="${margin.top}" x2="${x(value).toFixed(1)}" y2="${margin.top + plotH}" stroke="#eef2f7"></line>`);
    }
  }
  const yStep = (yMax - yMin) > 8 ? 2 : 1;
  for (let temp = yMin; temp <= yMax; temp += yStep) {
    lines.push(`<line x1="${margin.left}" y1="${y(temp).toFixed(1)}" x2="${margin.left + plotW}" y2="${y(temp).toFixed(1)}" stroke="#e5e7eb"></line>`);
    lines.push(`<text x="${margin.left - 10}" y="${y(temp) + 4}" text-anchor="end" font-size="12">${temp}</text>`);
  }
  lines.push(`<line x1="${margin.left}" y1="${margin.top + plotH}" x2="${margin.left + plotW}" y2="${margin.top + plotH}" stroke="#374151"></line>`);
  lines.push(`<line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${margin.top + plotH}" stroke="#374151"></line>`);
  lines.push(`<text x="${margin.left + plotW / 2}" y="${height - 14}" text-anchor="middle" font-size="12">${chartMode === "rows" ? "流程图全长" : "IO 触发序号"}</text>`);
  lines.push(`<text x="18" y="${margin.top + plotH / 2}" transform="rotate(-90 18 ${margin.top + plotH / 2})" text-anchor="middle" font-size="12">温度 / °C</text>`);
  datasets.forEach(dataset => {
    SERIES_KEYS.filter(key => visible[`${dataset.id}:${key}`] && (chartMode === "trigger" || !key.startsWith("tasi"))).forEach(key => {
      let points = dataset.rows.map(row => ({ x: row.x, y: row[key] })).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
      if (!points.length) return;
      if (chartMode === "rows" && points.length > plotW) {
        const stride = Math.ceil(points.length / plotW);
        points = points.filter((_, index) => index % stride === 0 || index === points.length - 1);
      }
      const path = points.map((point, index) => `${index ? "L" : "M"}${x(point.x).toFixed(1)},${y(point.y).toFixed(1)}`).join(" ");
      const width = key.startsWith("tasi") || key.endsWith("_max") ? 2.4 : key.endsWith("_avg") ? 2.0 : 1.35;
      const dash = dataset.dash ? ` stroke-dasharray="${dataset.dash}"` : "";
      lines.push(`<path d="${path}" fill="none" stroke="${COLORS[key]}" stroke-width="${width}" opacity="${dataset.opacity}"${dash}></path>`);
      if (chartMode === "rows") return;
      points.forEach(point => {
        const radius = dataset.name === "静" ? 2.6 : 3.3;
        lines.push(`<circle cx="${x(point.x).toFixed(1)}" cy="${y(point.y).toFixed(1)}" r="${radius}" fill="${COLORS[key]}" opacity="${dataset.opacity}"><title>${LABELS[key]}（${dataset.name}） #${point.x}: ${fmt(point.y)}C</title></circle>`);
      });
    });
  });
  svg.innerHTML = lines.join("");
}

function thermalColor(value, min, max) {
  const t = Math.max(0, Math.min(1, (value - min) / (max - min || 1)));
  const stops = [
    [0.00, [30, 64, 175]],
    [0.25, [34, 211, 238]],
    [0.52, [234, 179, 8]],
    [0.74, [249, 115, 22]],
    [1.00, [185, 28, 28]],
  ];
  for (let i = 1; i < stops.length; i += 1) {
    const prev = stops[i - 1], next = stops[i];
    if (t <= next[0]) {
      const local = (t - prev[0]) / (next[0] - prev[0] || 1);
      return prev[1].map((v, idx) => Math.round(v + (next[1][idx] - v) * local));
    }
  }
  return stops[stops.length - 1][1];
}

function drawThermal(canvasId, metaId, frame, scaleMin, scaleMax, triggerIndex, roiBox = null) {
  const canvas = document.getElementById(canvasId);
  const meta = document.getElementById(metaId);
  const ctx = canvas.getContext("2d");
  const width = frame?.width || 32;
  const height = frame?.height || 24;
  const scale = 10;
  canvas.width = width * scale;
  canvas.height = height * scale;
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!frame || !Array.isArray(frame.pixels)) {
    ctx.fillStyle = "#111827";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (meta) meta.textContent = "No frame";
    return;
  }
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const value = frame.pixels[y * width + x];
      if (!Number.isFinite(value)) {
        ctx.fillStyle = "#111827";
      } else {
        const [r, g, b] = thermalColor(value, scaleMin, scaleMax);
        ctx.fillStyle = `rgb(${r},${g},${b})`;
      }
      ctx.fillRect(x * scale, y * scale, scale, scale);
    }
  }
  if (roiBox) {
    const x = roiBox.xmin * scale;
    const y = roiBox.ymin * scale;
    const w = (roiBox.xmax - roiBox.xmin + 1) * scale;
    const h = (roiBox.ymax - roiBox.ymin + 1) * scale;
    ctx.lineWidth = 5;
    ctx.strokeStyle = "rgba(17, 24, 39, 0.82)";
    ctx.strokeRect(x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2));
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#fef08a";
    ctx.strokeRect(x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2));
  }
  const frameText = Number.isFinite(frame.frameIndex) && Number.isFinite(frame.frameCount) ? ` · frame ${frame.frameIndex + 1}/${frame.frameCount}` : "";
  const offsetText = Number.isFinite(frame.offsetMs) ? ` · ${fmt(frame.offsetMs, 0)}ms` : "";
  const roiText = roiBox ? ` · 误差ROI ${roiBox.xmax - roiBox.xmin + 1}x${roiBox.ymax - roiBox.ymin + 1}` : "";
  if (meta) meta.textContent = `#${triggerIndex || ""}${frameText}${offsetText} · max ${fmt(frame.max)}C · avg ${fmt(frame.avg)}C · center ${fmt(frame.center)}C${roiText}`;
}

function neighborAverage(pixels, width, height, x, y, shiftedParity) {
  const values = [];
  [[-1, 0], [1, 0], [0, -1], [0, 1]].forEach(([dx, dy]) => {
    const nx = x + dx;
    const ny = y + dy;
    if (nx < 0 || ny < 0 || nx >= width || ny >= height) return;
    if (((nx + ny) & 1) === shiftedParity) return;
    const value = pixels[ny * width + nx];
    if (Number.isFinite(value)) values.push(value);
  });
  if (!values.length) return pixels[y * width + x];
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function hotterCheckerParity(frame) {
  const box = coreHotBoundingBox(frame, 10) || hotBoundingBox(frame, 50);
  const width = frame?.width || 32;
  const height = frame?.height || 24;
  if (!frame || !Array.isArray(frame.pixels) || !box) return 0;
  const sums = [0, 0];
  const counts = [0, 0];
  for (let y = box.ymin; y <= box.ymax; y += 1) {
    for (let x = box.xmin; x <= box.xmax; x += 1) {
      if (x < 0 || y < 0 || x >= width || y >= height) continue;
      const value = frame.pixels[y * width + x];
      if (!Number.isFinite(value)) continue;
      const parity = (x + y) & 1;
      sums[parity] += value;
      counts[parity] += 1;
    }
  }
  const avg0 = counts[0] ? sums[0] / counts[0] : -Infinity;
  const avg1 = counts[1] ? sums[1] / counts[1] : -Infinity;
  return avg1 > avg0 ? 1 : 0;
}

function selectedSubpageShiftRows() {
  const rows = Number(subpageShiftRowsSelect?.value);
  return Number.isFinite(rows) ? Math.max(1, Math.min(3, Math.floor(rows))) : 1;
}

function subpageShiftDownFrame(frame, shiftRows = 1) {
  if (!frame || !Array.isArray(frame.pixels)) return frame;
  const width = frame.width || 32;
  const height = frame.height || 24;
  const parity = hotterCheckerParity(frame);
  const rows = Math.max(1, Math.min(3, Math.floor(Number(shiftRows) || 1)));
  const source = frame.pixels.slice();
  const pixels = source.slice();
  for (let y = height - 1; y >= 0; y -= 1) {
    for (let x = 0; x < width; x += 1) {
      if (((x + y) & 1) !== parity) continue;
      const sourceIndex = y * width + x;
      const targetY = y + rows;
      pixels[sourceIndex] = neighborAverage(source, width, height, x, y, parity);
      if (targetY < height) {
        pixels[targetY * width + x] = source[sourceIndex];
      }
    }
  }
  const valid = pixels.filter(Number.isFinite);
  return {
    ...frame,
    pixels,
    min: valid.length ? Math.min(...valid) : frame.min,
    max: valid.length ? Math.max(...valid) : frame.max,
    avg: valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : frame.avg,
  };
}

function drawHistogram(canvasId, frame, scaleMin, scaleMax) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const width = 320;
  const height = 92;
  canvas.width = width;
  canvas.height = height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  const values = (frame?.pixels || []).filter(Number.isFinite);
  if (!values.length) {
    ctx.fillStyle = "#5b6472";
    ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    ctx.fillText("No histogram", 12, 24);
    return;
  }
  const bins = 28;
  const min = Number.isFinite(scaleMin) ? scaleMin : Math.floor(Math.min(...values));
  const max = Number.isFinite(scaleMax) && scaleMax > min ? scaleMax : Math.ceil(Math.max(...values) + 1);
  const counts = Array.from({ length: bins }, () => 0);
  values.forEach(value => {
    const index = Math.max(0, Math.min(bins - 1, Math.floor((value - min) / (max - min || 1) * bins)));
    counts[index] += 1;
  });
  const maxCount = Math.max(...counts, 1);
  const plot = { left: 34, top: 10, right: 8, bottom: 22 };
  const plotW = width - plot.left - plot.right;
  const plotH = height - plot.top - plot.bottom;
  ctx.strokeStyle = "#d9dee7";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(plot.left, plot.top);
  ctx.lineTo(plot.left, plot.top + plotH);
  ctx.lineTo(plot.left + plotW, plot.top + plotH);
  ctx.stroke();
  counts.forEach((count, index) => {
    const binValue = min + (index + 0.5) / bins * (max - min);
    const [r, g, b] = thermalColor(binValue, min, max);
    const barW = plotW / bins;
    const barH = count / maxCount * plotH;
    ctx.fillStyle = `rgb(${r},${g},${b})`;
    ctx.fillRect(plot.left + index * barW + 1, plot.top + plotH - barH, Math.max(1, barW - 2), barH);
  });
  const hot50 = values.filter(value => value >= 50).length;
  const hot70 = values.filter(value => value >= 70).length;
  ctx.fillStyle = "#5b6472";
  ctx.font = "10px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.fillText(`${fmt(min, 0)}C`, plot.left, height - 6);
  ctx.fillText(`${fmt(max, 0)}C`, plot.left + plotW - 24, height - 6);
  ctx.fillText(`>=50:${hot50} >=70:${hot70}`, plot.left + 82, height - 6);
}

function thermalFrames(channelData) {
  return Array.isArray(channelData?.frames) ? channelData.frames : [];
}

function selectedLoopRange(frameCount) {
  if (frameCount <= 0) return { start: 0, end: 0 };
  let start = Number(thermalLoopStartSelect.value);
  let end = Number(thermalLoopEndSelect.value);
  if (!Number.isFinite(start)) start = 0;
  if (!Number.isFinite(end)) end = frameCount - 1;
  start = Math.max(0, Math.min(frameCount - 1, Math.floor(start)));
  end = Math.max(0, Math.min(frameCount - 1, Math.floor(end)));
  if (end < start) end = start;
  return { start, end };
}

function thermalTriggerIndexes() {
  const staticIndexes = state.staticData?.heatmaps?.triggerIndexes || [];
  const movingIndexes = state.movingData?.heatmaps?.triggerIndexes || [];
  return [...new Set([...staticIndexes, ...movingIndexes])].sort((a, b) => Number(a) - Number(b));
}

function triggerForPlayback(heatmaps, fallbackIndex) {
  if (!(thermalMode === "play" || thermalMode === "pause") || thermalPlayScope !== "triggers") {
    return selectedThermalTrigger || fallbackIndex || "";
  }
  const indexes = thermalTriggerIndexes();
  if (!indexes.length) return selectedThermalTrigger || fallbackIndex || "";
  return indexes[animationFrameIndex % indexes.length];
}

function frameForMode(channelData, index) {
  const frames = thermalFrames(channelData);
  if (!frames.length) return null;
  let frameIndex = 0;
  if (thermalMode === "latest") {
    frameIndex = frames.length - 1;
  } else if (thermalMode === "play" || thermalMode === "pause") {
    if (thermalPlayScope === "triggers") {
      frameIndex = Math.max(0, Math.min(frames.length - 1, manualFrameIndex));
    } else {
      const range = selectedLoopRange(frames.length);
      frameIndex = range.start + (index % (range.end - range.start + 1));
    }
  } else if (thermalMode === "manual") {
    frameIndex = Math.max(0, Math.min(frames.length - 1, manualFrameIndex));
  } else {
    frameIndex = Math.max(0, Math.min(frames.length - 1, channelData.peakFrameIndex || 0));
  }
  return { ...frames[frameIndex], frameIndex, frameCount: frames.length };
}

function clippedRoiBox(box, width = 32, height = 24) {
  const xmin = Math.max(0, Math.min(width - 1, box.xmin));
  const xmax = Math.max(0, Math.min(width - 1, box.xmax));
  const ymin = Math.max(0, Math.min(height - 1, box.ymin));
  const ymax = Math.max(0, Math.min(height - 1, box.ymax));
  if (xmax < xmin || ymax < ymin) return null;
  return { xmin, xmax, ymin, ymax };
}

function errorRoiForDisplayedFrame(data, sensor, frame) {
  if (!showThermalRoi || !frame || !Number.isFinite(frame.frameIndex)) return null;
  if (!selectedRepeatFrameIndexes().includes(frame.frameIndex)) return null;
  const reference = frameForRepeat(data, sensor, "1", frame.frameIndex);
  const refBox = coreHotBoundingBox(reference, 10);
  const frameBox = coreHotBoundingBox(frame, 10);
  if (!reference || !refBox || !frameBox) return null;
  const dx = Math.round(frameBox.cx - refBox.cx);
  const dy = Math.round(frameBox.cy - refBox.cy);
  return clippedRoiBox({
    xmin: refBox.xmin + dx,
    xmax: refBox.xmax + dx,
    ymin: refBox.ymin + dy,
    ymax: refBox.ymax + dy,
  }, frame.width || 32, frame.height || 24);
}

function heatmapScaleBounds(...heatmapsList) {
  const mins = [20];
  const maxes = [60];
  heatmapsList.forEach(heatmaps => {
    const scale = heatmaps?.scale || {};
    if (Number.isFinite(scale.min)) mins.push(scale.min);
    if (Number.isFinite(scale.max)) maxes.push(scale.max);
    if (Number.isFinite(scale.observedMin)) mins.push(Math.floor(scale.observedMin));
    if (Number.isFinite(scale.observedMax)) maxes.push(Math.ceil(scale.observedMax));
  });
  const min = Math.min(...mins);
  const max = Math.max(...maxes);
  return { min, max: max > min ? max : min + 1 };
}

function commonHeatmapTriggerIndexes() {
  const staticIndexes = state.staticData?.heatmaps?.triggerIndexes || [];
  const movingIndexes = state.movingData?.heatmaps?.triggerIndexes || [];
  return staticIndexes.filter(index => movingIndexes.includes(index)).sort((a, b) => Number(a) - Number(b));
}

function fallbackDistortionDatasets() {
  const items = [];
  if (state.staticData) items.push({ id: "fallback-static", role: "static", data: state.staticData });
  if (state.movingData) items.push({ id: "fallback-moving", role: "moving", data: state.movingData });
  return items;
}

function activeDistortionDatasets() {
  return distortionDatasets.length ? distortionDatasets : fallbackDistortionDatasets();
}

function distortionDatasetName(item) {
  const source = item.data || {};
  const role = item.role === "static" ? "静" : "动";
  return `${source.displayName || source.shortName || source.sessionName || "未命名"}（${role}）`;
}

function distortionTriggerIndexes(items) {
  const set = new Set();
  items.forEach(item => {
    (item.data?.heatmaps?.triggerIndexes || []).forEach(index => set.add(index));
  });
  return [...set].sort((a, b) => Number(a) - Number(b));
}

function distortionFrameCount(items, triggerIndex) {
  let count = 0;
  items.forEach(item => {
    const channels = item.data?.heatmaps?.byTrigger?.[triggerIndex]?.channels || {};
    count = Math.max(count, thermalFrames(channels.left).length, thermalFrames(channels.right).length);
  });
  return count;
}

function renderGaussianSourceList() {
  if (!distortionDatasets.length) {
    gaussianSourceList.innerHTML = `<span class="hint">当前默认比较上方选择的静止和滑动目录；点“添加”后可叠加更多文件。</span>`;
    return;
  }
  gaussianSourceList.innerHTML = distortionDatasets.map(item => `
    <span class="gaussian-source-chip">
      ${escapeHtml(distortionDatasetName(item))}
      <button type="button" data-remove-gaussian="${escapeHtml(item.id)}" aria-label="remove gaussian source">×</button>
    </span>
  `).join("");
  gaussianSourceList.querySelectorAll("[data-remove-gaussian]").forEach(button => {
    button.addEventListener("click", () => {
      distortionDatasets = distortionDatasets.filter(item => item.id !== button.dataset.removeGaussian);
      renderDistortion();
    });
  });
}

function updateDistortionOptions() {
  const items = activeDistortionDatasets();
  const indexes = distortionTriggerIndexes(items);
  const latest = indexes[indexes.length - 1] || "";
  const currentTrigger = distortionTriggerSelect.value && indexes.includes(distortionTriggerSelect.value)
    ? distortionTriggerSelect.value
    : latest;
  distortionTriggerSelect.innerHTML = indexes.length
    ? indexes.map(index => `<option value="${index}">触发 #${index}</option>`).join("")
    : `<option value="">暂无触发窗口</option>`;
  distortionTriggerSelect.value = currentTrigger || "";

  const count = distortionFrameCount(items, currentTrigger);
  const previousFrame = Number(distortionFrameSelect.value);
  const frameIndex = Number.isFinite(previousFrame) ? Math.max(0, Math.min(count - 1, previousFrame)) : 0;
  distortionFrameSelect.innerHTML = count > 0
    ? Array.from({ length: count }, (_, index) => `<option value="${index}">第 ${index + 1} 帧</option>`).join("")
    : `<option value="0">暂无共同帧</option>`;
  distortionFrameSelect.value = String(frameIndex);
  distortionTriggerSelect.disabled = !indexes.length;
  distortionFrameSelect.disabled = count <= 0;
}

function frameAt(channelData, index) {
  const frames = thermalFrames(channelData);
  if (!frames.length) return null;
  const frameIndex = Math.max(0, Math.min(frames.length - 1, index));
  return { ...frames[frameIndex], frameIndex, frameCount: frames.length };
}

function frameBackground(frame) {
  const values = (frame?.pixels || []).filter(Number.isFinite).sort((a, b) => a - b);
  if (!values.length) return NaN;
  return values[Math.max(0, Math.min(values.length - 1, Math.floor(values.length * 0.08)))];
}

function distortionForFrames(staticFrame, movingFrame) {
  if (!staticFrame || !movingFrame || !Array.isArray(staticFrame.pixels) || !Array.isArray(movingFrame.pixels)) return null;
  const width = staticFrame.width || 32;
  const height = staticFrame.height || 24;
  const background = frameBackground(staticFrame);
  const strength = Math.max(1e-6, (staticFrame.max || Math.max(...staticFrame.pixels.filter(Number.isFinite))) - background);
  const allRates = [];
  const allDiffs = [];
  const roiRates = [];
  const roiDiffs = [];
  const roi = coreHotBoundingBox(staticFrame, 50);
  const staticBox = coreHotBoundingBox(staticFrame, 50);
  const movingBox = coreHotBoundingBox(movingFrame, 50);
  const dx = staticBox && movingBox ? Math.round(movingBox.cx - staticBox.cx) : 0;
  const dy = staticBox && movingBox ? Math.round(movingBox.cy - staticBox.cy) : 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const s = pixelAt(staticFrame, x, y);
      const m = pixelAt(movingFrame, x + dx, y + dy);
      if (!Number.isFinite(s) || !Number.isFinite(m)) continue;
      const diff = Math.abs(m - s);
      const rate = (diff / strength) * 100;
      allDiffs.push(diff);
      allRates.push(rate);
      if (roi && x >= roi.xmin && x <= roi.xmax && y >= roi.ymin && y <= roi.ymax) {
        roiDiffs.push(diff);
        roiRates.push(rate);
      }
    }
  }
  return {
    strength,
    background,
    whole: stats(allRates),
    wholeDiff: stats(allDiffs),
    roi: stats(roiRates),
    roiDiff: stats(roiDiffs),
    roiSize: roi ? `${roi.xmax - roi.xmin + 1}x${roi.ymax - roi.ymin + 1}` : "",
    dx,
    dy,
  };
}

function gaussianStats(frame) {
  const values = (frame?.pixels || []).filter(Number.isFinite);
  if (!values.length) return null;
  const meanValue = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce((sum, value) => sum + (value - meanValue) ** 2, 0) / values.length;
  const std = Math.max(Math.sqrt(variance), 0.001);
  return {
    values,
    mean: meanValue,
    std,
    min: Math.min(...values),
    max: Math.max(...values),
  };
}

function gaussianPdf(x, meanValue, std) {
  const z = (x - meanValue) / std;
  return Math.exp(-0.5 * z * z) / (std * Math.sqrt(2 * Math.PI));
}

function selectedDistortionSeriesIds() {
  return new Set(distortionSeriesInputs.filter(input => input.checked).map(input => input.dataset.distortionSeries));
}

function gaussianSeriesColor(index, channel) {
  const hue = (index * 47 + (channel === "left" ? 205 : 22)) % 360;
  return `hsl(${hue} 72% 44%)`;
}

function addDistortionDataset(role) {
  const data = role === "static" ? state.staticData : state.movingData;
  if (!data) {
    showError(role === "static" ? "请先选择并加载静止目录" : "请先选择并加载滑动目录");
    return;
  }
  const id = `${role}:${data.session}`;
  const existing = distortionDatasets.find(item => item.id === id);
  if (existing) {
    existing.data = data;
  } else {
    distortionDatasets.push({ id, role, data });
  }
  renderDistortion();
}

function renderDistortion() {
  updateDistortionOptions();
  renderGaussianSourceList();
  const triggerIndex = distortionTriggerSelect.value;
  const frameIndex = Number(distortionFrameSelect.value) || 0;
  const svg = document.getElementById("distortionGaussianChart");
  const note = document.getElementById("distortionGaussianNote");
  const datasets = activeDistortionDatasets();
  if (!datasets.length) {
    svg.innerHTML = `<text x="24" y="42" fill="#5b6472">请先选择或添加数据目录。</text>`;
    note.textContent = "";
    return;
  }
  if (!triggerIndex) {
    svg.innerHTML = `<text x="24" y="42" fill="#5b6472">没有触发窗口可比较。</text>`;
    note.textContent = "";
    return;
  }
  const selectedSeries = selectedDistortionSeriesIds();
  if (!selectedSeries.size) {
    svg.innerHTML = `<text x="24" y="42" fill="#5b6472">请至少勾选一条高斯曲线。</text>`;
    note.textContent = "";
    return;
  }
  const candidates = [];
  datasets.forEach((dataset, sourceIndex) => {
    const channels = dataset.data?.heatmaps?.byTrigger?.[triggerIndex]?.channels || {};
    ["left", "right"].forEach(channel => {
      const id = `${dataset.role}-${channel}`;
      if (!selectedSeries.has(id)) return;
      const sensorLabel = channel === "left" ? "左" : "右";
      candidates.push({
        id,
        label: `${distortionDatasetName(dataset)}-${sensorLabel}`,
        color: gaussianSeriesColor(sourceIndex, channel),
        dash: dataset.role === "static" ? "7 5" : "",
        frame: frameAt(channels[channel], frameIndex),
      });
    });
  });
  const series = candidates
    .map(item => ({ ...item, stats: gaussianStats(item.frame) }))
    .filter(item => item.stats);
  if (!series.length) {
    svg.innerHTML = `<text x="24" y="42" fill="#5b6472">没有可绘制的 768 像素温度数据。</text>`;
    note.textContent = "";
    return;
  }
  const width = 1120;
  const legendRows = Math.max(1, Math.ceil(series.length / 2));
  const height = 430 + Math.max(0, legendRows - 1) * 24;
  const margin = { left: 62, right: 24, top: 34 + legendRows * 20, bottom: 58 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const xMin = Math.floor(Math.min(...series.map(item => item.stats.min)) - 2);
  const xMax = Math.ceil(Math.max(...series.map(item => item.stats.max)) + 2);
  const x = value => margin.left + (value - xMin) / (xMax - xMin || 1) * plotW;
  const sampleXs = Array.from({ length: 180 }, (_, index) => xMin + (xMax - xMin) * index / 179);
  const yMax = Math.max(...series.flatMap(item => sampleXs.map(temp => gaussianPdf(temp, item.stats.mean, item.stats.std))));
  const y = value => margin.top + plotH - value / (yMax || 1) * plotH;
  const parts = [`<rect x="${margin.left}" y="${margin.top}" width="${plotW}" height="${plotH}" fill="#ffffff"></rect>`];
  const xStep = Math.max(1, Math.ceil((xMax - xMin) / 8));
  for (let temp = xMin; temp <= xMax; temp += xStep) {
    parts.push(`<line x1="${x(temp).toFixed(1)}" y1="${margin.top}" x2="${x(temp).toFixed(1)}" y2="${margin.top + plotH}" stroke="#eef2f7"></line>`);
    parts.push(`<text x="${x(temp).toFixed(1)}" y="${margin.top + plotH + 24}" text-anchor="middle" font-size="12">${temp}°C</text>`);
  }
  for (let i = 0; i <= 4; i += 1) {
    const value = yMax * i / 4;
    parts.push(`<line x1="${margin.left}" y1="${y(value).toFixed(1)}" x2="${margin.left + plotW}" y2="${y(value).toFixed(1)}" stroke="#eef2f7"></line>`);
  }
  parts.push(`<line x1="${margin.left}" y1="${margin.top + plotH}" x2="${margin.left + plotW}" y2="${margin.top + plotH}" stroke="#374151"></line>`);
  parts.push(`<line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${margin.top + plotH}" stroke="#374151"></line>`);
  parts.push(`<text x="${margin.left + plotW / 2}" y="${height - 16}" text-anchor="middle" font-size="12">像素温度 / °C</text>`);
  parts.push(`<text x="20" y="${margin.top + plotH / 2}" transform="rotate(-90 20 ${margin.top + plotH / 2})" text-anchor="middle" font-size="12">高斯概率密度</text>`);
  series.forEach(item => {
    const path = sampleXs.map((temp, index) => {
      const density = gaussianPdf(temp, item.stats.mean, item.stats.std);
      return `${index ? "L" : "M"}${x(temp).toFixed(1)},${y(density).toFixed(1)}`;
    }).join(" ");
    const dash = item.dash ? ` stroke-dasharray="${item.dash}"` : "";
    parts.push(`<path d="${path}" fill="none" stroke="${item.color}" stroke-width="3"${dash}></path>`);
  });
  const legendStartY = 24;
  series.forEach((item, index) => {
    const legendX = margin.left + 12 + (index % 2) * 500;
    const legendY = legendStartY + Math.floor(index / 2) * 22;
    parts.push(`<line x1="${legendX}" y1="${legendY}" x2="${legendX + 28}" y2="${legendY}" stroke="${item.color}" stroke-width="3"${item.dash ? ` stroke-dasharray="${item.dash}"` : ""}></line>`);
    parts.push(`<text x="${legendX + 36}" y="${legendY + 4}" font-size="12" fill="#111827">${escapeHtml(item.label)} μ=${fmt(item.stats.mean)} σ=${fmt(item.stats.std)}</text>`);
  });
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = parts.join("");
  note.textContent = `触发 #${triggerIndex} / 第 ${frameIndex + 1} 帧；共绘制 ${series.length} 条曲线。每条曲线由对应伪彩图的 768 个像素温度计算均值 μ 和标准差 σ 后拟合得到。虚线为静止，实线为滑动。`;
}

function renderHeatmaps() {
  updateThermalTriggerOptions();
  const staticHeatmaps = state.staticData?.heatmaps;
  const movingHeatmaps = state.movingData?.heatmaps;
  const staticTriggerIndex = triggerForPlayback(staticHeatmaps, staticHeatmaps?.selectedTriggerIndex || "");
  const movingTriggerIndex = triggerForPlayback(movingHeatmaps, movingHeatmaps?.selectedTriggerIndex || "");
  const staticChannels = staticHeatmaps?.byTrigger?.[staticTriggerIndex]?.channels || {};
  const movingChannels = movingHeatmaps?.byTrigger?.[movingTriggerIndex]?.channels || {};
  const combinedTriggerKey = thermalPlayScope === "triggers"
    ? `${thermalPlayScope}|${selectedThermalTrigger || "all"}`
    : `${thermalPlayScope}|${staticTriggerIndex}|${movingTriggerIndex}`;
  if (combinedTriggerKey !== lastHeatmapTriggerIndex) {
    lastHeatmapTriggerIndex = combinedTriggerKey || null;
    animationFrameIndex = 0;
  }
  updateThermalFrameOptions([staticChannels, movingChannels]);
  const staticLeftFrame = frameForMode(staticChannels.left, animationFrameIndex);
  const staticRightFrame = frameForMode(staticChannels.right, animationFrameIndex);
  const movingLeftFrame = frameForMode(movingChannels.left, animationFrameIndex);
  const movingRightFrame = frameForMode(movingChannels.right, animationFrameIndex);
  const { min, max } = heatmapScaleBounds(staticHeatmaps, movingHeatmaps);
  drawThermal("staticLeftThermalCanvas", "staticLeftThermalMeta", staticLeftFrame, min, max, staticTriggerIndex, errorRoiForDisplayedFrame(state.staticData, "left", staticLeftFrame));
  drawThermal("staticRightThermalCanvas", "staticRightThermalMeta", staticRightFrame, min, max, staticTriggerIndex, errorRoiForDisplayedFrame(state.staticData, "right", staticRightFrame));
  drawThermal("movingLeftThermalCanvas", "movingLeftThermalMeta", movingLeftFrame, min, max, movingTriggerIndex, errorRoiForDisplayedFrame(state.movingData, "left", movingLeftFrame));
  drawThermal("movingRightThermalCanvas", "movingRightThermalMeta", movingRightFrame, min, max, movingTriggerIndex, errorRoiForDisplayedFrame(state.movingData, "right", movingRightFrame));
  const subpageShiftRows = selectedSubpageShiftRows();
  drawThermal("movingLeftCorrectedThermalCanvas", null, subpageShiftDownFrame(movingLeftFrame, subpageShiftRows), min, max, movingTriggerIndex, null);
  drawThermal("movingRightCorrectedThermalCanvas", null, subpageShiftDownFrame(movingRightFrame, subpageShiftRows), min, max, movingTriggerIndex, null);
  drawHistogram("staticLeftHistogramCanvas", staticLeftFrame, min, max);
  drawHistogram("staticRightHistogramCanvas", staticRightFrame, min, max);
  drawHistogram("movingLeftHistogramCanvas", movingLeftFrame, min, max);
  drawHistogram("movingRightHistogramCanvas", movingRightFrame, min, max);
  updateThermalControls();
}

function updatePanoramaTriggerOptions() {
  const heatmaps = state.movingData?.heatmaps;
  const indexes = heatmaps?.triggerIndexes || [];
  const latest = heatmaps?.selectedTriggerIndex || "";
  if (selectedPanoramaTrigger && !indexes.includes(selectedPanoramaTrigger)) {
    selectedPanoramaTrigger = "";
  }
  const current = selectedPanoramaTrigger || "";
  const optionsKey = `${latest}|${indexes.join(",")}`;
  if (optionsKey !== lastPanoramaTriggerOptionsKey) {
    const options = `<option value="">最新触发${latest ? `（#${latest}）` : ""}</option>` + indexes.map(index => `<option value="${index}">触发 #${index}</option>`).join("");
    panoramaTriggerSelect.innerHTML = options || `<option value="">暂无滑动触发帧</option>`;
    lastPanoramaTriggerOptionsKey = optionsKey;
  }
  panoramaTriggerSelect.value = current;
}

function validPanoramaFrame(frame) {
  const box = coreHotBoundingBox(frame, 50);
  if (!frame || !box || !Number.isFinite(frame.max)) return null;
  if (frame.max < 50) return null;
  if (box.count < 6 || box.count > 260) return null;
  return box;
}

function selectedPanoramaMinGap() {
  const value = Number(panoramaMinGapInput?.value);
  return Number.isFinite(value) ? Math.max(1, Math.min(200, Math.round(value))) : 19;
}

function selectedPanoramaAnchorMm() {
  const value = Number(panoramaAnchorMmInput?.value);
  return Number.isFinite(value) ? value : 300;
}

function selectedPanoramaStepMm(defaultStepMm = 110) {
  const value = Number(panoramaStepMmInput?.value);
  return Number.isFinite(value) && value > 0 ? value : defaultStepMm;
}

function framePositionMm(frame) {
  const x = Number(frame?.positionXMm);
  if (Number.isFinite(x)) return x;
  const center = Number(frame?.segmentCenterMm);
  return Number.isFinite(center) ? center : NaN;
}

function frameSegmentWidthMm(frame) {
  const start = Number(frame?.segmentStartMm);
  const end = Number(frame?.segmentEndMm);
  return Number.isFinite(start) && Number.isFinite(end) && end > start ? end - start : NaN;
}

function positionFramesForChannel(heatmaps, channelName, useCorrected) {
  const frames = [];
  const seen = new Set();
  const shiftRows = selectedSubpageShiftRows();
  Object.values(heatmaps?.byTrigger || {}).forEach(trigger => {
    const channelData = trigger?.channels?.[channelName];
    thermalFrames(channelData).forEach((frame, sourceFrameIndex) => {
      const positionX = framePositionMm(frame);
      if (!Number.isFinite(positionX) || !Array.isArray(frame?.pixels)) return;
      const key = `${channelName}|${frame.toOffsetBytes ?? ""}|${frame.timestamp || ""}|${positionX.toFixed(3)}`;
      if (seen.has(key)) return;
      seen.add(key);
      const sourceFrame = useCorrected ? subpageShiftDownFrame(frame, shiftRows) : frame;
      frames.push({
        ...sourceFrame,
        positionX,
        triggerIndex: trigger.triggerIndex,
        sourceFrameIndex,
        frameIndex: frames.length,
      });
    });
  });
  return frames.sort((a, b) => {
    if (a.positionX !== b.positionX) return a.positionX - b.positionX;
    return String(a.timestamp || "").localeCompare(String(b.timestamp || ""));
  });
}

function buildPositionPanorama(heatmaps, channelName, useCorrected) {
  const frames = positionFramesForChannel(heatmaps, channelName, useCorrected);
  if (!frames.length) return null;
  const width = frames[0].width || 32;
  const sourceHeight = frames[0].height || 24;
  const fovSamples = frames.map(frameSegmentWidthMm).filter(Number.isFinite);
  const fovMotionMm = fovSamples.length ? avg(fovSamples) : 110;
  const anchorMm = selectedPanoramaAnchorMm();
  const stepMm = selectedPanoramaStepMm(fovMotionMm);
  const positionInfo = heatmaps?.position || {};
  const observedMin = Number.isFinite(Number(positionInfo.minXMm)) ? Number(positionInfo.minXMm) : Math.min(...frames.map(frame => frame.positionX));
  const observedMax = Number.isFinite(Number(positionInfo.maxXMm)) ? Number(positionInfo.maxXMm) : Math.max(...frames.map(frame => frame.positionX));
  if (!(observedMax >= observedMin) || !(stepMm > 0)) return null;
  const targetCenters = [anchorMm];
  for (let target = anchorMm - stepMm; target >= observedMin; target -= stepMm) {
    targetCenters.push(target);
  }
  for (let target = anchorMm + stepMm; target <= observedMax; target += stepMm) {
    targetCenters.push(target);
  }
  targetCenters.sort((a, b) => a - b);
  const selected = [];
  const usedFrameKeys = new Set();
  targetCenters.forEach(target => {
    const candidate = frames.reduce((best, frame) => {
      const distance = Math.abs(frame.positionX - target);
      const bestDistance = best ? Math.abs(best.positionX - target) : Infinity;
      return distance < bestDistance ? frame : best;
    }, null);
    if (!candidate) return;
    const key = `${candidate.toOffsetBytes ?? ""}|${candidate.timestamp || ""}|${candidate.positionX.toFixed(3)}`;
    if (usedFrameKeys.has(key)) return;
    usedFrameKeys.add(key);
    selected.push({
      target,
      frame: candidate,
      errorMm: candidate.positionX - target,
    });
  });
  if (!selected.length) return null;
  selected.sort((a, b) => a.target - b.target);
  const outputHeight = selected.length * sourceHeight;
  const pixels = Array.from({ length: width * outputHeight }, () => null);
  selected.forEach((item, index) => {
    const frameBaseY = (selected.length - 1 - index) * sourceHeight;
    for (let y = 0; y < sourceHeight; y += 1) {
      const targetY = frameBaseY + y;
      for (let x = 0; x < width; x += 1) {
        const value = item.frame.pixels[y * width + x];
        if (!Number.isFinite(value)) continue;
        pixels[targetY * width + x] = value;
      }
    }
  });
  const valid = pixels.filter(Number.isFinite);
  return {
    mode: "position-keyframes",
    width,
    height: outputHeight,
    pixels,
    min: valid.length ? Math.min(...valid) : NaN,
    max: valid.length ? Math.max(...valid) : NaN,
    avg: valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : NaN,
    usedFrames: selected.map((_, index) => index + 1),
    inputFrames: frames.length,
    selectedFrames: selected.length,
    selectedTargets: selected.map(item => ({
      target: item.target,
      actual: item.frame.positionX,
      error: item.errorMm,
    })),
    filledPixels: valid.length,
    fovMotionMm,
    anchorMm,
    stepMm,
    observedMin,
    observedMax,
  };
}

function buildPanorama(channelData, useCorrected) {
  const frames = thermalFrames(channelData).map((frame, index) => ({ ...frame, frameIndex: index, frameCount: thermalFrames(channelData).length }));
  const shiftRows = selectedSubpageShiftRows();
  const prepared = frames.map(frame => {
    const sourceFrame = useCorrected ? subpageShiftDownFrame(frame, shiftRows) : frame;
    return { frame: sourceFrame, box: validPanoramaFrame(sourceFrame) };
  });
  if (!prepared.length) return null;
  const width = prepared[0].frame.width || 32;
  const height = prepared[0].frame.height || 24;
  const validItems = prepared.filter(item => item.box);
  const selected = [];
  const completeHotItems = validItems.filter(item => item.box.ymin > 0 && item.box.ymax < height - 1);
  const baseline = (completeHotItems.length ? completeHotItems : validItems).reduce((best, item) => {
    const bestMax = Number.isFinite(best?.frame?.max) ? best.frame.max : -Infinity;
    const itemMax = Number.isFinite(item?.frame?.max) ? item.frame.max : -Infinity;
    return itemMax > bestMax ? item : best;
  }, null) || prepared[0];
  if (baseline) {
    const baselineIndex = baseline.frame.frameIndex;
    const frameGapForExit = selectedPanoramaMinGap();
    for (let index = baselineIndex; index >= 0; index -= frameGapForExit) {
      selected.push(prepared[index]);
    }
    for (let index = baselineIndex + frameGapForExit; index < prepared.length; index += frameGapForExit) {
      selected.push(prepared[index]);
    }
    selected.sort((a, b) => a.frame.frameIndex - b.frame.frameIndex);
  }
  if (!selected.length) {
    const fallback = prepared.reduce((best, item) => {
      const bestMax = Number.isFinite(best?.frame?.max) ? best.frame.max : -Infinity;
      const itemMax = Number.isFinite(item?.frame?.max) ? item.frame.max : -Infinity;
      return itemMax > bestMax ? item : best;
    }, prepared[0]);
    selected.push(fallback);
  }
  const outputHeight = selected.length * height;
  const pixels = Array.from({ length: width * outputHeight }, () => null);
  selected.forEach((item, index) => {
    const frameBaseY = (selected.length - 1 - index) * height;
    for (let y = 0; y < height; y += 1) {
      const targetY = frameBaseY + y;
      for (let x = 0; x < width; x += 1) {
        const value = item.frame.pixels[y * width + x];
        if (!Number.isFinite(value)) continue;
        pixels[targetY * width + x] = value;
      }
    }
  });
  const valid = pixels.filter(Number.isFinite);
  return {
    width,
    height: outputHeight,
    pixels,
    min: valid.length ? Math.min(...valid) : NaN,
    max: valid.length ? Math.max(...valid) : NaN,
    avg: valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : NaN,
    usedFrames: selected.map(item => item.frame.frameIndex + 1),
    candidateFrames: validItems.length,
    baselineFrame: baseline?.frame?.frameIndex + 1,
    exitFrameGap: selectedPanoramaMinGap(),
  };
}

function drawPanorama(canvasId, metaId, detailId, panorama, scaleMin, scaleMax) {
  const canvas = document.getElementById(canvasId);
  const meta = document.getElementById(metaId);
  const detail = document.getElementById(detailId);
  const ctx = canvas.getContext("2d");
  const width = panorama?.width || 32;
  const height = panorama?.height || 24;
  const scale = 10;
  canvas.width = width * scale;
  canvas.height = height * scale;
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!panorama || !Array.isArray(panorama.pixels)) {
    ctx.fillStyle = "#111827";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    meta.textContent = "暂无可拼接帧";
    detail.textContent = "旧拼接需要滑动数据中存在 max >= 50C 的有效热源帧；物理拼接需要采集文件包含 position_x_mm。";
    return;
  }
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const value = panorama.pixels[y * width + x];
      if (!Number.isFinite(value)) {
        ctx.fillStyle = "#111827";
      } else {
        const [r, g, b] = thermalColor(value, scaleMin, scaleMax);
        ctx.fillStyle = `rgb(${r},${g},${b})`;
      }
      ctx.fillRect((width - 1 - x) * scale, y * scale, scale, scale);
    }
  }
  if (panorama.mode === "position-keyframes") {
    const targets = (panorama.selectedTargets || [])
      .map(item => `${fmt(item.target, 0)}mm→${fmt(item.actual, 1)}mm`)
      .join("，");
    meta.textContent = `物理关键帧拼接 · ${panorama.selectedFrames} 张完整伪彩图 · 中心 ${fmt(panorama.anchorMm, 0)}mm · max ${fmt(panorama.max)}C`;
    detail.textContent = `以 ${fmt(panorama.anchorMm, 0)}mm 为中心，每隔 ${fmt(panorama.stepMm, 0)}mm 选择最接近的完整伪彩图并贴边拼接；图像底部接近较小 x，顶部接近较大 x。选中位置：${targets}。`;
    return;
  }
  meta.textContent = `视野关键帧拼接 · ${panorama.usedFrames.length} 张完整伪彩图 · max ${fmt(panorama.max)}C`;
  detail.textContent = `先找热源完整成像的基准帧 frame${panorama.baselineFrame}；再在同一个触发窗口内，按每约 ${panorama.exitFrameGap} 个 frame 的视野移出间隔，向前和向后记录完整伪彩图，后续图里有没有热源都照样记录。选中 frame：${panorama.usedFrames.map(index => `frame${index}`).join(", ")}。`;
}

function speedPanoramaFor(data, useCorrected) {
  const heatmaps = data?.heatmaps;
  const { min, max } = heatmapScaleBounds(heatmaps);
  if (!heatmaps) return { panorama: null, min, max, channel: "" };
  if (heatmaps?.position?.hasPosition) {
    const leftPanorama = buildPositionPanorama(heatmaps, "left", useCorrected);
    if (leftPanorama) return { panorama: leftPanorama, min, max, channel: "left" };
    const rightPanorama = buildPositionPanorama(heatmaps, "right", useCorrected);
    return { panorama: rightPanorama, min, max, channel: rightPanorama ? "right" : "" };
  }
  const triggerIndex = selectedPanoramaTrigger || heatmaps?.selectedTriggerIndex || "";
  const channels = heatmaps?.byTrigger?.[triggerIndex]?.channels || {};
  const leftPanorama = buildPanorama(channels.left, useCorrected);
  if (leftPanorama) return { panorama: leftPanorama, min, max, channel: "left" };
  const rightPanorama = buildPanorama(channels.right, useCorrected);
  return { panorama: rightPanorama, min, max, channel: rightPanorama ? "right" : "" };
}

function renderPanorama() {
  updatePanoramaTriggerOptions();
  const speedDatasets = state.panoramaSpeedData || {};
  const usePositionPanorama = Boolean(state.staticData?.heatmaps?.position?.hasPosition) ||
    Boolean(state.movingData?.heatmaps?.position?.hasPosition) ||
    ["100", "200", "300"].some(speed => Boolean(speedDatasets[speed]?.heatmaps?.position?.hasPosition));
  panoramaTriggerSelect.disabled = usePositionPanorama;
  panoramaMinGapInput.disabled = usePositionPanorama;
  const useCorrected = Boolean(panoramaUseCorrectedToggle.checked);
  const staticResult = speedPanoramaFor(state.staticData, useCorrected);
  drawPanorama("panoramaStaticCanvas", "panoramaStaticMeta", "panoramaStaticDetail", staticResult.panorama, staticResult.min, staticResult.max);
  if (staticResult.panorama && staticResult.channel) {
    const detail = document.getElementById("panoramaStaticDetail");
    const channelLabel = staticResult.channel === "left" ? "左侧传感器" : "右侧传感器";
    detail.textContent = `${channelLabel} · ${detail.textContent}`;
  }
  const movingResult = speedPanoramaFor(state.movingData, useCorrected);
  drawPanorama("panoramaMovingCanvas", "panoramaMovingMeta", "panoramaMovingDetail", movingResult.panorama, movingResult.min, movingResult.max);
  if (movingResult.panorama && movingResult.channel) {
    const detail = document.getElementById("panoramaMovingDetail");
    const channelLabel = movingResult.channel === "left" ? "左侧传感器" : "右侧传感器";
    detail.textContent = `${channelLabel} · ${detail.textContent}`;
  }
  ["100", "200", "300"].forEach(speed => {
    const result = speedPanoramaFor(speedDatasets[speed], useCorrected);
    drawPanorama(`panoramaSpeed${speed}Canvas`, `panoramaSpeed${speed}Meta`, `panoramaSpeed${speed}Detail`, result.panorama, result.min, result.max);
    if (result.panorama && result.channel) {
      const detail = document.getElementById(`panoramaSpeed${speed}Detail`);
      const channelLabel = result.channel === "left" ? "左侧传感器" : "右侧传感器";
      detail.textContent = `${channelLabel} · ${detail.textContent}`;
    }
  });
}

function updateThermalTriggerOptions() {
  const indexes = thermalTriggerIndexes();
  const latest = state.movingData?.heatmaps?.selectedTriggerIndex || state.staticData?.heatmaps?.selectedTriggerIndex || "";
  if (selectedThermalTrigger && !indexes.includes(selectedThermalTrigger)) {
    selectedThermalTrigger = "";
  }
  const current = selectedThermalTrigger || "";
  const optionsKey = `${latest}|${indexes.join(",")}`;
  if (optionsKey !== lastThermalTriggerOptionsKey) {
    const options = `<option value="">最新触发${latest ? `（#${latest}）` : ""}</option>` + indexes.map(index => `<option value="${index}">触发 #${index}</option>`).join("");
    thermalTriggerSelect.innerHTML = options || `<option value="">暂无触发帧</option>`;
    lastThermalTriggerOptionsKey = optionsKey;
  }
  thermalTriggerSelect.value = current;
}

function updateThermalFrameOptions(channelGroups) {
  const count = channelGroups.reduce((maxCount, channels) => {
    return Math.max(maxCount, thermalFrames(channels.left).length, thermalFrames(channels.right).length);
  }, 0);
  if (manualFrameIndex >= count) manualFrameIndex = Math.max(0, count - 1);
  const previousStart = Number(thermalLoopStartSelect.value);
  const previousEnd = Number(thermalLoopEndSelect.value);
  const optionsKey = String(count);
  if (optionsKey !== lastThermalFrameOptionsKey) {
    const options = Array.from({ length: count }, (_, index) => `<option value="${index}">第 ${index + 1} 帧</option>`).join("");
    thermalFrameSelect.innerHTML = options || `<option value="0">暂无帧</option>`;
    lastThermalFrameOptionsKey = optionsKey;
  }
  if (optionsKey !== lastThermalLoopOptionsKey) {
    const startOptions = Array.from({ length: count }, (_, index) => `<option value="${index}">第 ${index + 1} 帧起</option>`).join("");
    const endOptions = Array.from({ length: count }, (_, index) => `<option value="${index}">第 ${index + 1} 帧止</option>`).join("");
    thermalLoopStartSelect.innerHTML = startOptions || `<option value="0">暂无帧</option>`;
    thermalLoopEndSelect.innerHTML = endOptions || `<option value="0">暂无帧</option>`;
    lastThermalLoopOptionsKey = optionsKey;
  }
  const loopStart = thermalLoopRangeInitialized && Number.isFinite(previousStart) ? Math.max(0, Math.min(count - 1, previousStart)) : 0;
  const loopEnd = thermalLoopRangeInitialized && Number.isFinite(previousEnd) ? Math.max(0, Math.min(count - 1, previousEnd)) : Math.max(0, count - 1);
  thermalLoopRangeInitialized = count > 0;
  thermalFrameSelect.value = String(manualFrameIndex);
  thermalLoopStartSelect.value = String(loopStart);
  thermalLoopEndSelect.value = String(loopEnd < loopStart ? loopStart : loopEnd);
  thermalFrameSelect.disabled = count <= 0;
  thermalLoopStartSelect.disabled = count <= 0;
  thermalLoopEndSelect.disabled = count <= 0;
}

function updateThermalControls() {
  thermalPeakBtn.classList.toggle("active", thermalMode === "peak");
  thermalPlayBtn.classList.toggle("active", thermalMode === "play");
  thermalPauseBtn.classList.toggle("active", thermalMode === "pause");
  thermalManualBtn.classList.toggle("active", thermalMode === "manual");
  thermalLatestBtn.classList.toggle("active", thermalMode === "latest");
  thermalRoiBtn.classList.toggle("active", showThermalRoi);
}

function setThermalMode(mode) {
  thermalMode = mode;
  if (mode !== "play" && mode !== "pause") animationFrameIndex = 0;
  if (mode === "play" && thermalPlayScope === "triggers") manualFrameIndex = Number(thermalFrameSelect.value) || 0;
  if (mode === "manual") manualFrameIndex = Number(thermalFrameSelect.value) || 0;
  renderHeatmaps();
  updateAnimationTimer();
}

function updateAnimationTimer() {
  if (animationTimer) {
    clearInterval(animationTimer);
    animationTimer = null;
  }
  if (thermalMode !== "play") return;
  const intervalMs = Number(thermalSpeed.value) || 125;
  animationTimer = setInterval(() => {
    animationFrameIndex += 1;
    renderHeatmaps();
  }, intervalMs);
}

function repeatFrameCount(data, sensor) {
  const heatmaps = data?.heatmaps;
  const indexes = heatmaps?.triggerIndexes || [];
  let count = 0;
  indexes.forEach(index => {
    const channels = heatmaps?.byTrigger?.[index]?.channels || {};
    if (sensor === "both" || sensor === "left") count = Math.max(count, thermalFrames(channels.left).length);
    if (sensor === "both" || sensor === "right") count = Math.max(count, thermalFrames(channels.right).length);
  });
  return count;
}

function frameForRepeat(data, sensor, triggerIndex, frameIndex) {
  const channels = data?.heatmaps?.byTrigger?.[triggerIndex]?.channels || {};
  if (sensor === "left") {
    return thermalFrames(channels.left)[frameIndex] || null;
  }
  if (sensor === "right") {
    return thermalFrames(channels.right)[frameIndex] || null;
  }
  return null;
}

function pixelAt(frame, x, y) {
  const width = frame?.width || 32;
  const height = frame?.height || 24;
  if (!frame || !Array.isArray(frame.pixels) || x < 0 || y < 0 || x >= width || y >= height) return NaN;
  return frame.pixels[y * width + x];
}

function hotBoundingBox(frame, threshold = 50) {
  if (!frame || !Array.isArray(frame.pixels)) return null;
  const width = frame.width || 32;
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity, count = 0;
  frame.pixels.forEach((value, index) => {
    if (!Number.isFinite(value) || value < threshold) return;
    const x = index % width;
    const y = Math.floor(index / width);
    xmin = Math.min(xmin, x);
    xmax = Math.max(xmax, x);
    ymin = Math.min(ymin, y);
    ymax = Math.max(ymax, y);
    count += 1;
  });
  if (!count) return null;
  return {
    xmin,
    xmax,
    ymin,
    ymax,
    count,
    cx: (xmin + xmax) / 2,
    cy: (ymin + ymax) / 2,
  };
}

function coreHotBoundingBox(frame, deltaFromMax = 10) {
  const values = (frame?.pixels || []).filter(Number.isFinite);
  if (!values.length) return null;
  const maxValue = Math.max(...values);
  return hotBoundingBox(frame, maxValue - deltaFromMax);
}

function percentile(sortedValues, p) {
  if (!sortedValues.length) return NaN;
  const index = Math.min(sortedValues.length - 1, Math.max(0, Math.ceil((p / 100) * sortedValues.length) - 1));
  return sortedValues[index];
}

function stats(values) {
  const valid = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!valid.length) return { count: 0, p50: NaN, p90: NaN, p95: NaN, mean: NaN, max: NaN };
  return {
    count: valid.length,
    p50: percentile(valid, 50),
    p90: percentile(valid, 90),
    p95: percentile(valid, 95),
    mean: valid.reduce((sum, value) => sum + value, 0) / valid.length,
    max: valid[valid.length - 1],
  };
}

function frameDiffsAgainstReference(data, sensor, frameIndex, referenceIndex) {
  const indexes = data?.heatmaps?.triggerIndexes || [];
  const reference = frameForRepeat(data, sensor, referenceIndex, frameIndex);
  const refBox = coreHotBoundingBox(reference, 10);
  if (!reference || !refBox) return { diffs: [], shifts: [], comparisons: 0 };
  const diffs = [];
  const shifts = [];
  const roi = {
    xmin: refBox.xmin,
    xmax: refBox.xmax,
    ymin: refBox.ymin,
    ymax: refBox.ymax,
  };
  let comparisons = 0;
  indexes.forEach(index => {
    if (index === referenceIndex) return;
    const frame = frameForRepeat(data, sensor, index, frameIndex);
    const box = coreHotBoundingBox(frame, 10);
    if (!frame || !box) return;
    const dx = Math.round(box.cx - refBox.cx);
    const dy = Math.round(box.cy - refBox.cy);
    shifts.push({ dx, dy });
    comparisons += 1;
    for (let y = roi.ymin; y <= roi.ymax; y += 1) {
      for (let x = roi.xmin; x <= roi.xmax; x += 1) {
        const refValue = pixelAt(reference, x, y);
        const alignedValue = pixelAt(frame, x + dx, y + dy);
        if (Number.isFinite(refValue) && Number.isFinite(alignedValue)) {
          diffs.push(Math.abs(alignedValue - refValue));
        }
      }
    }
  });
  return { diffs, shifts, comparisons };
}

function repeatabilityStatsFor(data, sensor, frameIndexes, referenceIndex) {
  const sessionKey = data?.session || "none";
  const frameKey = frameIndexes.join(",");
  const cacheKey = `${sessionKey}|${sensor}|${referenceIndex}|${frameKey}`;
  if (repeatabilityStatsCache.has(cacheKey)) {
    return repeatabilityStatsCache.get(cacheKey);
  }
  const parts = frameIndexes.map(index => frameDiffsAgainstReference(data, sensor, index, referenceIndex));
  const item = stats(parts.flatMap(part => part.diffs));
  const shifts = parts.flatMap(part => part.shifts);
  item.comparisons = parts.reduce((sum, part) => sum + part.comparisons, 0);
  item.dxMin = shifts.length ? Math.min(...shifts.map(shift => shift.dx)) : NaN;
  item.dxMax = shifts.length ? Math.max(...shifts.map(shift => shift.dx)) : NaN;
  item.dyMin = shifts.length ? Math.min(...shifts.map(shift => shift.dy)) : NaN;
  item.dyMax = shifts.length ? Math.max(...shifts.map(shift => shift.dy)) : NaN;
  repeatabilityStatsCache.set(cacheKey, item);
  if (repeatabilityStatsCache.size > 400) {
    repeatabilityStatsCache.delete(repeatabilityStatsCache.keys().next().value);
  }
  return item;
}

function selectedRepeatFrameIndexes() {
  const count = Math.max(
    repeatFrameCount(state.staticData, "left"),
    repeatFrameCount(state.staticData, "right"),
    repeatFrameCount(state.movingData, "left"),
    repeatFrameCount(state.movingData, "right"),
  );
  if (repeatFrameSelect.value === "first4") {
    return Array.from({ length: Math.min(4, count) }, (_, index) => index);
  }
  const index = Number(repeatFrameSelect.value);
  return Number.isFinite(index) && index >= 0 && index < count ? [index] : [];
}

function updateRepeatFrameOptions() {
  const previous = repeatFrameSelect.value || "first4";
  const count = Math.max(
    repeatFrameCount(state.staticData, "left"),
    repeatFrameCount(state.staticData, "right"),
    repeatFrameCount(state.movingData, "left"),
    repeatFrameCount(state.movingData, "right"),
  );
  const optionsKey = String(count);
  if (optionsKey !== lastRepeatFrameOptionsKey) {
    const frameOptions = Array.from({ length: count }, (_, index) => `<option value="${index}">第 ${index + 1} 帧</option>`).join("");
    repeatFrameSelect.innerHTML = `<option value="first4">前 4 帧</option>${frameOptions}`;
    lastRepeatFrameOptionsKey = optionsKey;
  }
  repeatFrameSelect.value = previous === "first4" || Number(previous) < count ? previous : "first4";
}

function renderRepeatability() {
  updateRepeatFrameOptions();
  const frameIndexes = selectedRepeatFrameIndexes();
  const referenceIndex = "1";
  const frameLabel = repeatFrameSelect.value === "first4" ? `前 4 帧（实际 ${frameIndexes.length} 帧）` : `第 ${Number(repeatFrameSelect.value) + 1} 帧`;
  [
    ["staticRepeatabilityTable", state.staticData],
    ["movingRepeatabilityTable", state.movingData],
  ].forEach(([tableId, data]) => {
    const rows = ["left", "right"].map(sensor => {
      const item = repeatabilityStatsFor(data, sensor, frameIndexes, referenceIndex);
    const label = sensor === "left" ? "左侧传感器" : "右侧传感器";
    return `
    <tr>
      <td>${label}</td>
      <td>核心热区对齐，阈值=最高温-10°C，触发 #2-N 对比 #1</td>
      <td>${frameLabel}</td>
      <td>${item.comparisons || 0}</td>
      <td>${item.count}</td>
      <td>${fmt(item.p50)}</td>
      <td>${fmt(item.p90)}</td>
      <td>${fmt(item.p95)}</td>
      <td>${Number.isFinite(item.dxMin) ? `${item.dxMin}..${item.dxMax}` : ""}</td>
      <td>${Number.isFinite(item.dyMin) ? `${item.dyMin}..${item.dyMax}` : ""}</td>
    </tr>
  `;
    }).join("");
    document.getElementById(tableId).innerHTML = `
    <thead><tr><th>传感器</th><th>计算方法</th><th>帧范围</th><th>有效比较次数</th><th>差值数量</th><th>P50</th><th>P90</th><th>P95</th><th>x偏移</th><th>y偏移</th></tr></thead>
    <tbody>${rows}</tbody>`;
  });
}

function renderTable() {
  const data = summaryTableMode === "static" ? state.staticData : state.movingData;
  summaryStaticBtn.classList.toggle("active", summaryTableMode === "static");
  summaryMovingBtn.classList.toggle("active", summaryTableMode === "moving");
  summaryStaticBtn.disabled = !state.staticData;
  summaryMovingBtn.disabled = !state.movingData;
  if (!data) {
    document.getElementById("summaryTable").innerHTML = `
      <tbody><tr><td style="text-align:left">当前没有${summaryTableMode === "static" ? "静止" : "滑动"}数据。</td></tr></tbody>`;
    return;
  }
  const rows = triggerRows(data).map(row => {
    return `<tr>
      <td>#${row.index}</td><td>${row.timestamp}</td>
      <td>${row.leftFrames}</td><td>${fmt(row.left_min)}</td><td>${fmt(row.left_avg)}</td><td>${fmt(row.left_max)}</td>
      <td>${row.rightFrames}</td><td>${fmt(row.right_min)}</td><td>${fmt(row.right_avg)}</td><td>${fmt(row.right_max)}</td>
      <td>${fmt(row.tasi1, 1)}</td><td>${fmt(row.tasi2, 1)}</td><td>${fmt(row.tasi3, 1)}</td><td>${fmt(row.tasi4, 1)}</td>
    </tr>`;
  }).join("");
  document.getElementById("summaryTable").innerHTML = `
    <thead><tr><th>触发</th><th>时间</th><th>左侧帧数</th><th>左侧最低</th><th>左侧平均</th><th>左侧最高</th><th>右侧帧数</th><th>右侧最低</th><th>右侧平均</th><th>右侧最高</th><th>TA612-1</th><th>TA612-2</th><th>TA612-3</th><th>TA612-4</th></tr></thead>
    <tbody>${rows}</tbody>`;
}

document.getElementById("loadBtn").addEventListener("click", () => loadData().catch(showError));
document.getElementById("refreshBtn").addEventListener("click", () => loadData().catch(showError));
summaryStaticBtn.addEventListener("click", () => {
  summaryTableMode = "static";
  renderTable();
});
summaryMovingBtn.addEventListener("click", () => {
  summaryTableMode = "moving";
  renderTable();
});
chartTriggerModeBtn.addEventListener("click", () => {
  chartMode = "trigger";
  renderChart();
});
chartRowModeBtn.addEventListener("click", () => {
  chartMode = "rows";
  renderChart();
});
document.getElementById("saveStaticLabelBtn").addEventListener("click", async () => {
  try {
    await saveSessionLabel(staticSessionInput.value.trim(), staticLabelInput.value.trim());
    await loadSessionList();
    await loadData();
  } catch (error) {
    showError(error);
  }
});
document.getElementById("saveMovingLabelBtn").addEventListener("click", async () => {
  try {
    await saveSessionLabel(movingSessionInput.value.trim(), movingLabelInput.value.trim());
    await loadSessionList();
    await loadData();
  } catch (error) {
    showError(error);
  }
});
autoRefreshToggle.addEventListener("change", updateAutoRefresh);
thermalPeakBtn.addEventListener("click", () => setThermalMode("peak"));
thermalPlayBtn.addEventListener("click", () => setThermalMode("play"));
thermalPauseBtn.addEventListener("click", () => setThermalMode("pause"));
thermalManualBtn.addEventListener("click", () => setThermalMode("manual"));
thermalLatestBtn.addEventListener("click", () => setThermalMode("latest"));
thermalRoiBtn.addEventListener("click", () => {
  showThermalRoi = !showThermalRoi;
  renderHeatmaps();
});
thermalSpeed.addEventListener("change", () => {
  if (thermalMode === "play") updateAnimationTimer();
});
thermalPlayScopeSelect.addEventListener("change", () => {
  thermalPlayScope = thermalPlayScopeSelect.value;
  manualFrameIndex = Number(thermalFrameSelect.value) || 0;
  animationFrameIndex = 0;
  lastHeatmapTriggerIndex = null;
  renderHeatmaps();
  updateAnimationTimer();
});
subpageShiftRowsSelect.addEventListener("change", () => {
  renderHeatmaps();
  renderPanorama();
});
thermalTriggerSelect.addEventListener("change", () => {
  selectedThermalTrigger = thermalTriggerSelect.value;
  animationFrameIndex = 0;
  renderHeatmaps();
});
distortionTriggerSelect.addEventListener("change", () => {
  renderDistortion();
});
distortionFrameSelect.addEventListener("change", () => {
  renderDistortion();
});
distortionSeriesInputs.forEach(input => {
  input.addEventListener("change", () => {
    renderDistortion();
  });
});
addStaticGaussianBtn.addEventListener("click", () => {
  addDistortionDataset("static");
});
addMovingGaussianBtn.addEventListener("click", () => {
  addDistortionDataset("moving");
});
clearGaussianSourcesBtn.addEventListener("click", () => {
  distortionDatasets = [];
  renderDistortion();
});
panoramaTriggerSelect.addEventListener("change", () => {
  selectedPanoramaTrigger = panoramaTriggerSelect.value;
  renderPanorama();
});
panoramaMinGapInput.addEventListener("input", () => {
  renderPanorama();
});
panoramaMinGapInput.addEventListener("change", () => {
  renderPanorama();
});
panoramaAnchorMmInput.addEventListener("input", () => {
  renderPanorama();
});
panoramaAnchorMmInput.addEventListener("change", () => {
  renderPanorama();
});
panoramaStepMmInput.addEventListener("input", () => {
  renderPanorama();
});
panoramaStepMmInput.addEventListener("change", () => {
  renderPanorama();
});
panoramaUseCorrectedToggle.addEventListener("change", () => {
  renderPanorama();
});
Object.values(panoramaSpeedInputs).forEach(input => {
  input.addEventListener("change", () => loadData().catch(showError));
});
thermalFrameSelect.addEventListener("change", () => {
  manualFrameIndex = Number(thermalFrameSelect.value) || 0;
  thermalMode = "manual";
  animationFrameIndex = 0;
  renderHeatmaps();
  updateAnimationTimer();
});
function handleThermalLoopRangeChange() {
  animationFrameIndex = 0;
  renderHeatmaps();
  updateAnimationTimer();
}
thermalLoopStartSelect.addEventListener("change", handleThermalLoopRangeChange);
thermalLoopEndSelect.addEventListener("change", handleThermalLoopRangeChange);
repeatFrameSelect.addEventListener("change", () => {
  requestAnimationFrame(renderRepeatability);
  requestAnimationFrame(renderHeatmaps);
});
staticSessionSelect.addEventListener("change", () => {
  staticSessionInput.value = staticSessionSelect.value;
  loadData().catch(showError);
});
movingSessionSelect.addEventListener("change", () => {
  movingSessionInput.value = movingSessionSelect.value;
  loadData().catch(showError);
});
function showError(error) {
  document.getElementById("status").innerHTML = `<span class="error">${error.message}</span>`;
}
function isInteractingWithThermalControls() {
  return document.activeElement === thermalTriggerSelect ||
    document.activeElement === thermalFrameSelect ||
    document.activeElement === thermalPlayScopeSelect ||
    document.activeElement === subpageShiftRowsSelect ||
    document.activeElement === thermalLoopStartSelect ||
    document.activeElement === thermalLoopEndSelect ||
    document.activeElement === distortionTriggerSelect ||
    document.activeElement === distortionFrameSelect ||
    document.activeElement === panoramaTriggerSelect ||
    document.activeElement === panoramaMinGapInput ||
    document.activeElement === panoramaAnchorMmInput ||
    document.activeElement === panoramaStepMmInput ||
    document.activeElement === panoramaUseCorrectedToggle ||
    document.activeElement === panoramaSpeedInputs["100"] ||
    document.activeElement === panoramaSpeedInputs["200"] ||
    document.activeElement === panoramaSpeedInputs["300"] ||
    document.activeElement === repeatFrameSelect ||
    document.activeElement === staticSessionSelect ||
    document.activeElement === movingSessionSelect ||
    document.activeElement === staticSessionInput ||
    document.activeElement === movingSessionInput ||
    document.activeElement === staticLabelInput ||
    document.activeElement === movingLabelInput;
}

function sameSessionPath(left, right) {
  if (!left || !right) return false;
  return String(left) === String(right) || String(left).endsWith("/" + String(right)) || String(right).endsWith("/" + String(left));
}

async function selectedSessionsChanged() {
  const staticPath = staticSessionInput.value.trim();
  const movingPath = movingSessionInput.value.trim();
  const res = await fetch("/api/sessions", { cache: "no-store" });
  const payload = await res.json();
  if (!res.ok) throw new Error(payload.error || "读取数据目录失败");
  const findSession = path => payload.sessions.find(item => sameSessionPath(item.path, path));
  const staticInfo = staticPath ? findSession(staticPath) : null;
  const movingInfo = movingPath ? findSession(movingPath) : null;
  const staticChanged = staticPath && staticInfo && state.staticData && (
    staticInfo.triggerCount !== state.staticData.counts?.triggers ||
    staticInfo.windowRows !== state.staticData.counts?.triggerWindowRows
  );
  const movingChanged = movingPath && movingInfo && state.movingData && (
    movingInfo.triggerCount !== state.movingData.counts?.triggers ||
    movingInfo.windowRows !== state.movingData.counts?.triggerWindowRows
  );
  return Boolean(staticChanged || movingChanged);
}

function updateAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (autoRefreshToggle.checked) {
    refreshTimer = setInterval(async () => {
      if (autoRefreshInFlight) return;
      if (isInteractingWithThermalControls()) {
        lastAutoRefreshSkip = Date.now();
        return;
      }
      autoRefreshInFlight = true;
      try {
        if (await selectedSessionsChanged()) {
          await loadData({ silent: true });
        }
      } catch (error) {
        showError(error);
      } finally {
        autoRefreshInFlight = false;
      }
    }, 2000);
  }
  if (state.data) render();
}
loadSessionList().then(loadData).then(updateAutoRefresh).catch(showError);
</script>
</body>
</html>
"""


class ReportHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("", "/"):
            self.send_html(HTML)
            return
        if parsed.path == "/api/session":
            self.handle_session(parsed.query)
            return
        if parsed.path == "/api/sessions":
            self.handle_sessions()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/session-label":
            self.handle_session_label()
            return
        self.send_error(404)

    def handle_sessions(self) -> None:
        try:
            sessions = list_low_delay_sessions(self.server.browse_root)  # type: ignore[attr-defined]
            self.send_json({"sessions": sessions}, 200)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_session(self, query: str) -> None:
        try:
            params = parse_qs(query)
            path = unquote(params.get("path", [""])[0]) or None
            session = resolve_session(path, self.server.project_root, self.server.browse_root)  # type: ignore[attr-defined]
            plain, compressed = cached_session_response(session)
            self.send_json_bytes(plain, compressed, 200)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_session_label(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8") if body else "{}")
            session = resolve_session(
                str(payload.get("path") or ""),
                self.server.project_root,  # type: ignore[attr-defined]
                self.server.browse_root,  # type: ignore[attr-defined]
            )
            label_info = write_session_label(session, str(payload.get("label") or ""), str(payload.get("note") or ""))
            self.send_json({"session": str(session), **label_info}, 200)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=json_default, separators=(",", ":")).encode("utf-8")
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        gzip_encoded = accepts_gzip and len(data) > 1024
        if gzip_encoded:
            data = gzip.compress(data, compresslevel=1)
        self.send_json_data(data, status, gzip_encoded=gzip_encoded)

    def send_json_bytes(self, plain: bytes, compressed: bytes, status: int) -> None:
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        if accepts_gzip and len(compressed) < len(plain):
            self.send_json_data(compressed, status, gzip_encoded=True)
        else:
            self.send_json_data(plain, status, gzip_encoded=False)

    def send_json_data(self, data: bytes, status: int, gzip_encoded: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if gzip_encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a low-delay trigger-window infrared report.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--session", default="", help="Low-delay capture session directory. Defaults to latest.")
    parser.add_argument("--browse-root", default=DEFAULT_BROWSE_ROOT)
    args = parser.parse_args()

    project_root = Path.cwd().resolve()
    browse_root = Path(args.browse_root).expanduser()
    if not browse_root.is_absolute():
        browse_root = (project_root / browse_root).resolve()
    session = resolve_session(args.session or None, project_root, browse_root)

    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    server.project_root = project_root  # type: ignore[attr-defined]
    server.browse_root = browse_root  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/?path={quote(str(session))}"
    print(f"Serving low-delay infrared report at {url}")
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
