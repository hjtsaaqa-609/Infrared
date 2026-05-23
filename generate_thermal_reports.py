#!/usr/bin/env python3
"""
Batch-generate pseudocolor and report images for exported MLX90640 heatmaps.

This script is designed for the dataset layout used here:

  session_dir/
    capture_records.json
    frame_dir_000001/
      metadata.json
      left_infrared_thermal.bin
      right_infrared_thermal.bin

The .bin files in this dataset are 768-byte exported heatmaps, not complete
MLX90640 16-bit frameData[834] dumps. The report therefore shows:

  EST:  an estimate derived from exported byte values, default raw - 44
  META: the capture program's own summary from metadata.json

For strict MLX90640 temperature reconstruction, save EEPROM data and full
16-bit frameData from the capture side and use the Melexis driver algorithm.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: Pillow. Install it on Ubuntu 22.04 with:\n"
        "  sudo apt update && sudo apt install -y python3-pil\n"
        "or:\n"
        "  python3 -m pip install Pillow"
    ) from exc


DEFAULT_WIDTH = 32
DEFAULT_HEIGHT = 24
DEFAULT_OFFSET = 44.0

# Pillow on Ubuntu 22.04 may not expose Image.Resampling yet. Keep the script
# compatible with both old apt-installed Pillow and newer pip-installed Pillow.
try:
    RESAMPLE_BICUBIC = Image.Resampling.BICUBIC
except AttributeError:
    RESAMPLE_BICUBIC = Image.BICUBIC

SIDE_NAMES = ("left", "right")
SENSOR_KEYS = ("sensor_temperature", "sensor_temp", "ambient_temperature", "ta")


@dataclass
class SideResult:
    frame_dir: Path
    side: str
    status_name: str
    bin_path: Path
    pseudo_path: Path
    report_path: Path
    width: int
    height: int
    bin_size: int
    expected_size: int
    raw_min: int
    raw_max: int
    raw_avg: float
    est_min: float
    est_max: float
    est_avg: float
    est_center: float
    est_tmax_minus_tavg: float
    est_tmax_minus_tmin: float
    meta_min: Optional[float]
    meta_max: Optional[float]
    meta_avg: Optional[float]
    meta_tmax_minus_tavg: Optional[float]
    meta_tmax_minus_tmin: Optional[float]
    sensor: Optional[float]
    capture_json_match: str
    warning: str = ""


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_capture_records(root: Path) -> Dict[Path, Dict[int, Dict[str, Any]]]:
    """Load every capture_records.json under root, keyed by its parent dir."""
    sessions: Dict[Path, Dict[int, Dict[str, Any]]] = {}
    for path in root.rglob("capture_records.json"):
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
            continue

        if not isinstance(records, list):
            print(f"WARNING: {path} is not a JSON list", file=sys.stderr)
            continue

        by_seq: Dict[int, Dict[str, Any]] = {}
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("sequence"), int):
                by_seq[record["sequence"]] = record
        sessions[path.parent.resolve()] = by_seq
    return sessions


def nearest_capture_record(
    frame_dir: Path,
    sequence: Optional[int],
    sessions: Dict[Path, Dict[int, Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    if sequence is None:
        return None

    resolved = frame_dir.resolve()
    candidates: List[Tuple[int, Path]] = []
    for session_dir in sessions:
        try:
            resolved.relative_to(session_dir)
        except ValueError:
            continue
        candidates.append((len(session_dir.parts), session_dir))

    if not candidates:
        return None

    _, closest = max(candidates)
    return sessions[closest].get(sequence)


def frame_dirs(root: Path) -> Iterable[Path]:
    for metadata_path in sorted(root.rglob("metadata.json")):
        yield metadata_path.parent


def find_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
    ]
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        except Exception:
            pass
    return ImageFont.load_default()


def thermal_rgb(value: float) -> Tuple[int, int, int]:
    """Jet-like thermal palette: blue/cyan -> green/yellow -> red."""
    x = max(0.0, min(1.0, value))
    r = max(0.0, min(1.0, 1.5 - abs(4.0 * x - 3.0)))
    g = max(0.0, min(1.0, 1.5 - abs(4.0 * x - 2.0)))
    b = max(0.0, min(1.0, 1.5 - abs(4.0 * x - 1.0)))
    return int(r * 255), int(g * 255), int(b * 255)


def make_pseudocolor(values: List[int], width: int, height: int) -> Image.Image:
    raw_min = min(values)
    raw_max = max(values)
    denom = raw_max - raw_min

    pixels: List[Tuple[int, int, int]] = []
    for value in values:
        norm = 0.0 if denom == 0 else (value - raw_min) / denom
        pixels.append(thermal_rgb(norm))

    image = Image.new("RGB", (width, height))
    image.putdata(pixels)
    image = image.resize((640, 480), RESAMPLE_BICUBIC)
    return image.filter(ImageFilter.GaussianBlur(radius=0.4))


def draw_square(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: int = 20) -> None:
    half = size / 2
    shadow = [cx - half + 2, cy - half + 2, cx + half + 2, cy + half + 2]
    box = [cx - half, cy - half, cx + half, cy + half]
    draw.rectangle(shadow, outline="black", width=4)
    draw.rectangle(box, outline="white", width=4)


def draw_triangle(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: int = 22) -> None:
    half = size / 2
    points = [(cx, cy - half), (cx - half, cy + half), (cx + half, cy + half)]
    shadow = [(x + 2, y + 2) for x, y in points]
    draw.line(shadow + [shadow[0]], fill="black", width=4, joint="curve")
    draw.line(points + [points[0]], fill="white", width=4, joint="curve")


def sensor_value(metadata: Dict[str, Any]) -> Optional[float]:
    for key in SENSOR_KEYS:
        value = metadata.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def format_meta(metadata: Dict[str, Any]) -> str:
    fields = []
    for label, key in (("MAX", "max_temperature"), ("MIN", "min_temperature"), ("AVG", "avg_temperature")):
        value = metadata.get(key)
        if value is not None:
            try:
                fields.append(f"{label}:{float(value):.0f}C")
            except (TypeError, ValueError):
                fields.append(f"{label}:{value}")
    return "  ".join(fields)


def number_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def delta_text(prefix: str, tmax_minus_tavg: Optional[float], tmax_minus_tmin: Optional[float]) -> str:
    if tmax_minus_tavg is None or tmax_minus_tmin is None:
        return ""
    return f"{prefix} Tmax-Tavg:{tmax_minus_tavg:6.2f}C   Tmax-Tmin:{tmax_minus_tmin:6.2f}C"


def positions(values: List[int], width: int, target: int) -> List[Tuple[int, int]]:
    return [(idx // width, idx % width) for idx, value in enumerate(values) if value == target]


def parse_sequence(metadata: Dict[str, Any], frame_dir: Path) -> Optional[int]:
    sequence = metadata.get("sequence")
    if isinstance(sequence, int):
        return sequence

    suffix = frame_dir.name.rsplit("_", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


def process_side(
    frame_dir: Path,
    metadata: Dict[str, Any],
    side: str,
    record: Optional[Dict[str, Any]],
    offset: float,
    font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> Optional[SideResult]:
    width = int(metadata.get("image_width") or DEFAULT_WIDTH)
    height = int(metadata.get("image_height") or DEFAULT_HEIGHT)
    expected_size = width * height

    bin_name = metadata.get(f"{side}_infrared_file") or f"{side}_infrared_thermal.bin"
    bin_path = frame_dir / str(bin_name)
    if not bin_path.exists():
        return None

    data = list(bin_path.read_bytes())
    warning = ""
    if len(data) != expected_size:
        warning = f"size {len(data)} != expected {expected_size}"
        print(f"WARNING: {bin_path}: {warning}", file=sys.stderr)
        return SideResult(
            frame_dir=frame_dir,
            side=side,
            status_name=str(metadata.get(f"{side}_infrared_status_name", "")),
            bin_path=bin_path,
            pseudo_path=frame_dir / f"{side}_infrared_thermal_pseudocolor.png",
            report_path=frame_dir / f"{side}_infrared_thermal_report.png",
            width=width,
            height=height,
            bin_size=len(data),
            expected_size=expected_size,
            raw_min=0,
            raw_max=0,
            raw_avg=0.0,
            est_min=0.0,
            est_max=0.0,
            est_avg=0.0,
            est_center=0.0,
            est_tmax_minus_tavg=0.0,
            est_tmax_minus_tmin=0.0,
            meta_min=number_value(metadata.get("min_temperature")),
            meta_max=number_value(metadata.get("max_temperature")),
            meta_avg=number_value(metadata.get("avg_temperature")),
            meta_tmax_minus_tavg=None,
            meta_tmax_minus_tmin=None,
            sensor=sensor_value(metadata),
            capture_json_match="NA",
            warning=warning,
        )

    raw_min = min(data)
    raw_max = max(data)
    raw_avg = sum(data) / len(data)
    est_values = [value - offset for value in data]
    est_min = min(est_values)
    est_max = max(est_values)
    est_avg = sum(est_values) / len(est_values)
    est_center = est_values[(height // 2) * width + (width // 2)]
    est_tmax_minus_tavg = est_max - est_avg
    est_tmax_minus_tmin = est_max - est_min

    meta_min = number_value(metadata.get("min_temperature"))
    meta_max = number_value(metadata.get("max_temperature"))
    meta_avg = number_value(metadata.get("avg_temperature"))
    meta_tmax_minus_tavg = None
    meta_tmax_minus_tmin = None
    if meta_max is not None and meta_avg is not None:
        meta_tmax_minus_tavg = meta_max - meta_avg
    if meta_max is not None and meta_min is not None:
        meta_tmax_minus_tmin = meta_max - meta_min

    pseudo = make_pseudocolor(data, width, height)
    pseudo_path = frame_dir / f"{side}_infrared_thermal_pseudocolor.png"
    pseudo.save(pseudo_path)

    sensor = sensor_value(metadata)
    meta_text = format_meta(metadata)
    canvas_h = 724 if sensor is not None else 696
    canvas = Image.new("RGB", (680, canvas_h), "white")
    canvas.paste(pseudo, (20, 20))
    draw = ImageDraw.Draw(canvas)

    scale_x = 640 / width
    scale_y = 480 / height

    def pixel_center(row: int, col: int) -> Tuple[float, float]:
        return 20 + (col + 0.5) * scale_x, 20 + (row + 0.5) * scale_y

    for row, col in positions(data, width, raw_max):
        draw_square(draw, *pixel_center(row, col))
    for row, col in positions(data, width, raw_min):
        draw_triangle(draw, *pixel_center(row, col))

    text_y = 516
    draw.text((20, text_y), f"{side.upper()} EST MAX:{est_max:6.2f}C", fill="black", font=font)
    draw.text((348, text_y), f"EST MIN:{est_min:6.2f}C", fill="black", font=font)
    draw.text((20, text_y + 28), f"EST AVG:{est_avg:6.2f}C", fill="black", font=font)
    draw.text((348, text_y + 28), f"CENTER:{est_center:6.2f}C", fill="black", font=font)
    draw.text(
        (20, text_y + 56),
        delta_text("EST", est_tmax_minus_tavg, est_tmax_minus_tmin),
        fill="black",
        font=font,
    )

    if meta_text:
        draw.text((20, text_y + 84), f"META {meta_text}", fill="black", font=font)

    meta_delta = delta_text("META", meta_tmax_minus_tavg, meta_tmax_minus_tmin)
    if meta_delta:
        draw.text((20, text_y + 112), meta_delta, fill="black", font=font)

    legend_y = text_y + 140
    if sensor is not None:
        draw.text((20, legend_y), f"SENSOR:{sensor:6.2f}C", fill="black", font=font)
        legend_y += 28

    lx = 348
    draw.rectangle([lx, legend_y, lx + 14, legend_y + 14], outline="black", width=2)
    draw.text((lx + 22, legend_y - 4), "MAX", fill="black", font=small_font)
    tx = 448
    draw.line(
        [(tx + 7, legend_y - 1), (tx, legend_y + 14), (tx + 14, legend_y + 14), (tx + 7, legend_y - 1)],
        fill="black",
        width=2,
    )
    draw.text((tx + 22, legend_y - 4), "MIN", fill="black", font=small_font)

    report_path = frame_dir / f"{side}_infrared_thermal_report.png"
    canvas.save(report_path)

    capture_json_match = "NA"
    if record is not None:
        record_values = record.get(f"{side}_infrared_thermal")
        capture_json_match = "YES" if record_values == data else "NO"

    return SideResult(
        frame_dir=frame_dir,
        side=side,
        status_name=str(metadata.get(f"{side}_infrared_status_name", "")),
        bin_path=bin_path,
        pseudo_path=pseudo_path,
        report_path=report_path,
        width=width,
        height=height,
        bin_size=len(data),
        expected_size=expected_size,
        raw_min=raw_min,
        raw_max=raw_max,
        raw_avg=raw_avg,
        est_min=est_min,
        est_max=est_max,
        est_avg=est_avg,
        est_center=est_center,
        est_tmax_minus_tavg=est_tmax_minus_tavg,
        est_tmax_minus_tmin=est_tmax_minus_tmin,
        meta_min=meta_min,
        meta_max=meta_max,
        meta_avg=meta_avg,
        meta_tmax_minus_tavg=meta_tmax_minus_tavg,
        meta_tmax_minus_tmin=meta_tmax_minus_tmin,
        sensor=sensor,
        capture_json_match=capture_json_match,
        warning=warning,
    )


def write_audit_csv(root: Path, results: List[SideResult]) -> Path:
    out = root / "thermal_report_audit.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "frame_dir",
                "side",
                "status_name",
                "bin_file",
                "report_file",
                "pseudocolor_file",
                "width",
                "height",
                "bin_size",
                "expected_size",
                "capture_json_match",
                "raw_min",
                "raw_max",
                "raw_avg",
                "est_min_c",
                "est_max_c",
                "est_avg_c",
                "est_center_c",
                "est_tmax_minus_tavg_c",
                "est_tmax_minus_tmin_c",
                "meta_min_c",
                "meta_max_c",
                "meta_avg_c",
                "meta_tmax_minus_tavg_c",
                "meta_tmax_minus_tmin_c",
                "sensor_c",
                "warning",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.frame_dir,
                    result.side,
                    result.status_name,
                    result.bin_path,
                    result.report_path,
                    result.pseudo_path,
                    result.width,
                    result.height,
                    result.bin_size,
                    result.expected_size,
                    result.capture_json_match,
                    result.raw_min,
                    result.raw_max,
                    f"{result.raw_avg:.6f}",
                    f"{result.est_min:.6f}",
                    f"{result.est_max:.6f}",
                    f"{result.est_avg:.6f}",
                    f"{result.est_center:.6f}",
                    f"{result.est_tmax_minus_tavg:.6f}",
                    f"{result.est_tmax_minus_tmin:.6f}",
                    "" if result.meta_min is None else result.meta_min,
                    "" if result.meta_max is None else result.meta_max,
                    "" if result.meta_avg is None else result.meta_avg,
                    "" if result.meta_tmax_minus_tavg is None else f"{result.meta_tmax_minus_tavg:.6f}",
                    "" if result.meta_tmax_minus_tmin is None else f"{result.meta_tmax_minus_tmin:.6f}",
                    "" if result.sensor is None else f"{result.sensor:.6f}",
                    result.warning,
                ]
            )
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate MLX90640 exported heatmap pseudocolor and report images recursively."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Dataset root directory. The script recursively finds metadata.json files.",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=DEFAULT_OFFSET,
        help="Temperature estimate offset. EST temperature is raw - offset. Default: 44.",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="Do not write thermal_report_audit.csv in the root directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: root directory does not exist: {root}", file=sys.stderr)
        return 2

    sessions = load_capture_records(root)
    font = find_font(19)
    small_font = find_font(15)
    results: List[SideResult] = []

    for frame_dir in frame_dirs(root):
        metadata_path = frame_dir / "metadata.json"
        try:
            metadata = load_json(metadata_path)
        except Exception as exc:
            print(f"WARNING: cannot read {metadata_path}: {exc}", file=sys.stderr)
            continue

        sequence = parse_sequence(metadata, frame_dir)
        record = nearest_capture_record(frame_dir, sequence, sessions)

        for side in SIDE_NAMES:
            result = process_side(frame_dir, metadata, side, record, args.offset, font, small_font)
            if result is not None:
                results.append(result)

    audit_path = None
    if not args.no_audit:
        audit_path = write_audit_csv(root, results)

    frame_count = len({result.frame_dir for result in results})
    print(f"Processed frame directories: {frame_count}")
    print(f"Generated reports: {len([r for r in results if not r.warning])}")
    print(f"Total result rows: {len(results)}")
    if audit_path is not None:
        print(f"Audit CSV: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
