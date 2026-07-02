#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from macos_infrared_triggered_cl_low_delay import (  # noqa: E402
    CliError,
    DEFAULT_BAUD,
    DEFAULT_EMISSIVITY,
    DEFAULT_I2C_STRETCH,
    DEFAULT_MLX_ADDRESS,
    DEFAULT_MLX_READ_CHUNK_WORDS,
    DEFAULT_READ_MODE,
    DEFAULT_REFRESH_RATE_HZ,
    DEFAULT_STARTUP_DELAY_SECONDS,
    DEFAULT_TASI_BAUD,
    JoinedSummaryWriter,
    Mlx90640Device,
    MlxCaptureWriter,
    MlxChannelCaptureWorker,
    MlxFrameSummary,
    MlxNativeCalculator,
    READ_MODES,
    TasiSerialCaptureWriter,
    TasiSerialPoller,
    TasiSerialSample,
    Usb2UartSerialI2c,
    default_native_library_path,
    east8_iso,
    east8_now,
    ensure_mlx_eeprom_looks_valid,
    format_trigger_temperature_summary,
    i2c_rate_name,
    import_serial,
    list_serial_ports,
    parse_i2c_rate,
    parse_refresh_rate_hz,
    prefix_dict_keys,
    resolve_dual_mlx_ports,
    safe_channel_name,
    select_default_tasi_port,
    serial_open_cli_error,
    to_east8,
)


@dataclass(frozen=True)
class EncoderPositionSample:
    timestamp: datetime
    line: str
    a: int | None
    b: int | None
    raw: int | None
    delta: int | None
    position_counts: int | None
    x_mm: float
    travel_pct: float | None
    velocity_mm_s: float | None


@dataclass(frozen=True)
class PositionSegment:
    index: int
    start_mm: float
    end_mm: float
    center_mm: float

    def contains(self, x_mm: float) -> bool:
        return self.start_mm <= x_mm <= self.end_mm


@dataclass(frozen=True)
class PositionFrameSnapshot:
    mlx: MlxFrameSummary
    tasi: TasiSerialSample | None
    timestamp: datetime
    sample: EncoderPositionSample


def parse_optional_int(line: str, name: str) -> int | None:
    match = re.search(rf"\b{name}\s*=\s*([-+]?\d+)", line)
    return int(match.group(1)) if match else None


def parse_optional_float(line: str, name: str, suffix: str = "") -> float | None:
    match = re.search(rf"\b{name}\s*=\s*([-+]?\d+(?:\.\d+)?)\s*{re.escape(suffix)}", line)
    return float(match.group(1)) if match else None


def parse_encoder_line(
    line: str,
    timestamp: datetime,
    previous: EncoderPositionSample | None,
) -> EncoderPositionSample | None:
    x_mm = parse_optional_float(line, "x", "mm")
    if x_mm is None:
        return None
    velocity_mm_s = None
    if previous is not None:
        dt = (timestamp - previous.timestamp).total_seconds()
        if dt > 1e-6:
            velocity_mm_s = (x_mm - previous.x_mm) / dt
    return EncoderPositionSample(
        timestamp=timestamp,
        line=line.strip(),
        a=parse_optional_int(line, "A"),
        b=parse_optional_int(line, "B"),
        raw=parse_optional_int(line, "raw"),
        delta=parse_optional_int(line, "delta"),
        position_counts=parse_optional_int(line, "position"),
        x_mm=x_mm,
        travel_pct=parse_optional_float(line, "travel", "%"),
        velocity_mm_s=velocity_mm_s,
    )


class EncoderPositionPoller:
    def __init__(
        self,
        port: str,
        baud: int,
        timeout: float,
        csv_path: Path,
        debug: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.csv_path = csv_path
        self.debug = debug
        self.frames = 0
        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: EncoderPositionSample | None = None
        self._error: BaseException | None = None
        self._csv_file = None
        self._csv = None

    def start(self) -> None:
        serial = import_serial()
        try:
            self._serial = serial.Serial(
                self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
        except Exception as exc:
            raise serial_open_cli_error(self.port, exc) from exc
        self._serial.reset_input_buffer()
        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(
            [
                "timestamp_east8",
                "a",
                "b",
                "raw",
                "delta",
                "position_counts",
                "x_mm",
                "travel_pct",
                "velocity_mm_s",
                "line",
            ]
        )
        self._thread = threading.Thread(target=self._run, name="encoder-position-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.timeout + 0.5))
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None

    def __enter__(self) -> "EncoderPositionPoller":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def check_error(self) -> None:
        if self._error is not None:
            raise CliError(f"Encoder position poller failed: {self._error}") from self._error

    def latest(self) -> EncoderPositionSample | None:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        previous: EncoderPositionSample | None = None
        try:
            while not self._stop.is_set():
                raw_line = self._serial.readline() if self._serial is not None else b""
                if not raw_line:
                    continue
                timestamp = east8_now()
                line = raw_line.decode("utf-8", errors="replace").strip()
                sample = parse_encoder_line(line, timestamp, previous)
                if sample is None:
                    if self.debug:
                        print(f"ENCODER ignored: {line}")
                    continue
                previous = sample
                self.frames += 1
                with self._lock:
                    self._latest = sample
                if self._csv is not None:
                    self._csv.writerow(
                        [
                            east8_iso(sample.timestamp),
                            "" if sample.a is None else sample.a,
                            "" if sample.b is None else sample.b,
                            "" if sample.raw is None else sample.raw,
                            "" if sample.delta is None else sample.delta,
                            "" if sample.position_counts is None else sample.position_counts,
                            f"{sample.x_mm:.6f}",
                            "" if sample.travel_pct is None else f"{sample.travel_pct:.6f}",
                            "" if sample.velocity_mm_s is None else f"{sample.velocity_mm_s:.6f}",
                            sample.line,
                        ]
                    )
                    self._csv_file.flush()
                if self.debug:
                    velocity = "?" if sample.velocity_mm_s is None else f"{sample.velocity_mm_s:.1f}"
                    print(f"ENCODER x={sample.x_mm:.3f}mm v={velocity}mm/s")
        except BaseException as exc:
            self._error = exc


class NullTasiPoller:
    frames = 0

    def __enter__(self) -> "NullTasiPoller":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def check_error(self) -> None:
        return None

    def latest(self) -> None:
        return None


class PositionSegmentSummaryWriter:
    def __init__(
        self,
        session_dir: Path,
        segments: Sequence[PositionSegment],
        encoder_poller: EncoderPositionPoller,
        include_mlx_channel: bool = False,
        direction: str = "increasing",
        min_abs_speed_mm_s: float = 1.0,
        target_tolerance_mm: float = 25.0,
        channel_names: Sequence[str] | None = None,
    ) -> None:
        self.include_mlx_channel = include_mlx_channel
        self.segments = list(segments)
        self.encoder_poller = encoder_poller
        self.direction = direction
        self.min_abs_speed_mm_s = max(0.0, min_abs_speed_mm_s)
        self.target_tolerance_mm = max(0.0, target_tolerance_mm)
        self.channel_names = [str(name) for name in (channel_names or [])]
        self._lock = threading.Lock()
        self._enabled = False
        self._segment_first_ts: dict[int, datetime] = {}
        self._segment_last_ts: dict[int, datetime] = {}
        self._segment_rows: dict[int, int] = {segment.index: 0 for segment in self.segments}
        self._last_snapshot_by_channel: dict[str, PositionFrameSnapshot] = {}
        self._written_segment_channels: set[tuple[int, str]] = set()
        self._events_file = (session_dir / "trigger_events.csv").open("w", newline="", encoding="utf-8")
        self._summary_file = (session_dir / "trigger_window_summary.csv").open("w", newline="", encoding="utf-8")
        self._events_csv = csv.writer(self._events_file)
        self._summary_csv = csv.writer(self._summary_file)
        self._events_csv.writerow(
            [
                "trigger_index",
                "trigger_timestamp_east8",
                "window_start_east8",
                "window_end_east8",
                "window_seconds",
                "trigger_io",
                "trigger_edge",
                "trigger_report_hex",
                "position_start_mm",
                "position_end_mm",
                "position_center_mm",
                "segment_frame_rows",
            ]
        )
        header = [
            "trigger_index",
            "trigger_offset_ms",
            "mlx_timestamp_east8",
            "mlx_subpage",
            "mlx_to_offset_bytes",
            "mlx_robot_thermal_u8_offset_bytes",
            "mlx_ta_c",
            "mlx_min_c",
            "mlx_max_c",
            "mlx_avg_c",
            "mlx_center_c",
            "tasi_timestamp_east8",
            "tasi_age_ms",
            "tasi_channel1_c",
            "tasi_channel2_c",
            "tasi_channel3_c",
            "tasi_channel4_c",
            "tasi_raw_offset_bytes",
            "tasi_checksum_ok",
            "position_x_mm",
            "position_travel_pct",
            "position_velocity_mm_s",
            "segment_start_mm",
            "segment_end_mm",
            "segment_center_mm",
        ]
        if self.include_mlx_channel:
            header.insert(2, "mlx_channel")
        self._summary_csv.writerow(header)

    def enable(self) -> None:
        with self._lock:
            self._enabled = True

    def close(self) -> None:
        with self._lock:
            for segment in self.segments:
                first = self._segment_first_ts.get(segment.index)
                last = self._segment_last_ts.get(segment.index)
                seconds = (last - first).total_seconds() if first is not None and last is not None else 0.0
                self._events_csv.writerow(
                    [
                        segment.index,
                        east8_iso(first) if first is not None else "",
                        east8_iso(first) if first is not None else "",
                        east8_iso(last) if last is not None else "",
                        f"{seconds:.6f}",
                        "encoder",
                        "position-segment",
                        "",
                        f"{segment.start_mm:.6f}",
                        f"{segment.end_mm:.6f}",
                        f"{segment.center_mm:.6f}",
                        self._segment_rows.get(segment.index, 0),
                    ]
                )
            self._events_file.flush()
        self._events_file.close()
        self._summary_file.close()

    def __enter__(self) -> "PositionSegmentSummaryWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def write(self, mlx: MlxFrameSummary, tasi: TasiSerialSample | None) -> None:
        sample = self.encoder_poller.latest()
        if sample is None:
            return
        with self._lock:
            if not self._enabled:
                return
            timestamp = to_east8(mlx.timestamp_utc)
            channel = mlx.channel or ""
            current = PositionFrameSnapshot(mlx=mlx, tasi=tasi, timestamp=timestamp, sample=sample)
            previous = self._last_snapshot_by_channel.get(channel)
            motion_ok = self._accept_motion(previous.sample, sample) if previous is not None else self._accept_sample(sample)
            for segment in self.segments:
                key = (segment.index, channel)
                if key in self._written_segment_channels:
                    continue
                snapshot = self._snapshot_for_target(segment, previous, current, motion_ok)
                if snapshot is None:
                    continue
                if segment.index not in self._segment_first_ts:
                    self._segment_first_ts[segment.index] = snapshot.timestamp
                self._segment_last_ts[segment.index] = snapshot.timestamp
                self._segment_rows[segment.index] = self._segment_rows.get(segment.index, 0) + 1
                self._written_segment_channels.add(key)
                self._summary_csv.writerow(
                    self._build_row(segment, snapshot.mlx, snapshot.tasi, snapshot.timestamp, snapshot.sample)
                )
            self._last_snapshot_by_channel[channel] = current
            self._summary_file.flush()

    def _accept_sample(self, sample: EncoderPositionSample) -> bool:
        velocity = sample.velocity_mm_s
        if velocity is None:
            return False
        if abs(velocity) < self.min_abs_speed_mm_s:
            return False
        if self.direction == "increasing":
            return velocity > 0
        if self.direction == "decreasing":
            return velocity < 0
        return True

    def _accept_motion(self, previous: EncoderPositionSample, current: EncoderPositionSample) -> bool:
        dt = (current.timestamp - previous.timestamp).total_seconds()
        if dt <= 1e-6:
            return False
        velocity = (current.x_mm - previous.x_mm) / dt
        if abs(velocity) < self.min_abs_speed_mm_s:
            return False
        if self.direction == "increasing":
            return velocity > 0
        if self.direction == "decreasing":
            return velocity < 0
        return True

    def _snapshot_for_target(
        self,
        segment: PositionSegment,
        previous: PositionFrameSnapshot | None,
        current: PositionFrameSnapshot,
        motion_ok: bool,
    ) -> PositionFrameSnapshot | None:
        target = segment.center_mm
        current_error = abs(current.sample.x_mm - target)
        if previous is None:
            return current if motion_ok and current_error <= self.target_tolerance_mm else None
        prev_x = previous.sample.x_mm
        current_x = current.sample.x_mm
        crossed_target = min(prev_x, current_x) <= target <= max(prev_x, current_x)
        previous_error = abs(prev_x - target)
        if not crossed_target:
            if not motion_ok:
                return None
            if self.direction == "increasing" and max(prev_x, current_x) < target:
                return None
            if self.direction == "decreasing" and min(prev_x, current_x) > target:
                return None
            best = previous if previous_error <= current_error else current
            best_error = min(previous_error, current_error)
            return best if best_error <= self.target_tolerance_mm else None
        best = previous if previous_error <= current_error else current
        best_error = min(previous_error, current_error)
        if motion_ok and best_error <= self.target_tolerance_mm:
            return best
        return None

    def is_complete(self) -> bool:
        if not self.channel_names:
            return False
        return len(self._written_segment_channels) >= len(self.segments) * len(self.channel_names)

    def _build_row(
        self,
        segment: PositionSegment,
        mlx: MlxFrameSummary,
        tasi: TasiSerialSample | None,
        timestamp: datetime,
        sample: EncoderPositionSample,
    ) -> list[object]:
        start = self._segment_first_ts.get(segment.index, timestamp)
        offset_ms = (timestamp - start).total_seconds() * 1000.0
        if tasi is not None and tasi.channels_c is not None:
            age_ms = (mlx.timestamp_utc - tasi.timestamp_utc).total_seconds() * 1000.0
            tasi_timestamp = east8_iso(tasi.timestamp_utc)
            channels = [f"{value:.6f}" for value in tasi.channels_c]
            tasi_offset = tasi.raw_offset_bytes
            checksum_ok = tasi.checksum_ok
        else:
            age_ms = ""
            tasi_timestamp = ""
            channels = ["", "", "", ""]
            tasi_offset = ""
            checksum_ok = ""
        row: list[object] = [
            segment.index,
            f"{offset_ms:.3f}",
            east8_iso(mlx.timestamp_utc),
            mlx.subpage,
            mlx.to_offset_bytes,
            "" if mlx.robot_thermal_u8_offset_bytes is None else mlx.robot_thermal_u8_offset_bytes,
            f"{mlx.ta_c:.6f}",
            f"{mlx.min_c:.6f}",
            f"{mlx.max_c:.6f}",
            f"{mlx.avg_c:.6f}",
            f"{mlx.center_c:.6f}",
            tasi_timestamp,
            f"{age_ms:.3f}" if isinstance(age_ms, float) else "",
            *channels,
            tasi_offset,
            checksum_ok,
            f"{sample.x_mm:.6f}",
            "" if sample.travel_pct is None else f"{sample.travel_pct:.6f}",
            "" if sample.velocity_mm_s is None else f"{sample.velocity_mm_s:.6f}",
            f"{segment.start_mm:.6f}",
            f"{segment.end_mm:.6f}",
            f"{segment.center_mm:.6f}",
        ]
        if self.include_mlx_channel:
            row.insert(2, mlx.channel)
        return row


def build_position_segments(
    start_mm: float,
    end_mm: float,
    width_mm: float,
    step_mm: float,
    anchor_mm: float,
) -> list[PositionSegment]:
    if width_mm <= 0:
        raise CliError("--segment-width-mm must be > 0")
    if step_mm <= 0:
        raise CliError("--segment-step-mm must be > 0")
    if end_mm <= start_mm:
        raise CliError("--travel-end-mm must be greater than --travel-start-mm")
    if not (start_mm <= anchor_mm <= end_mm):
        raise CliError("--anchor-mm must be inside --travel-start-mm and --travel-end-mm")
    centers: list[float] = []
    current = anchor_mm
    while current >= start_mm:
        centers.append(current)
        current -= step_mm
    current = anchor_mm + step_mm
    while current <= end_mm:
        centers.append(current)
        current += step_mm
    centers = sorted(set(round(center, 6) for center in centers))
    half_width = width_mm / 2.0
    segments = [
        PositionSegment(
            index=index,
            start_mm=max(start_mm, center - half_width),
            end_mm=min(end_mm, center + half_width),
            center_mm=center,
        )
        for index, center in enumerate(centers, start=1)
    ]
    return segments


def resolve_encoder_port(explicit_port: str | None, excluded_ports: Sequence[str]) -> str:
    if explicit_port:
        return explicit_port
    excluded = set(excluded_ports)
    ports = [port for port in list_serial_ports() if str(port.get("device") or "") not in excluded]
    st_candidates = []
    for port in ports:
        text = " ".join(
            str(port.get(key) or "")
            for key in ("device", "description", "manufacturer", "product", "serial_number")
        ).lower()
        if "stlink" in text or "st-link" in text or "nucleo" in text:
            st_candidates.append(str(port["device"]))
    if len(st_candidates) == 1:
        return st_candidates[0]
    modem_candidates = [
        str(port["device"])
        for port in ports
        if str(port.get("device") or "").startswith("/dev/cu.usbmodem")
    ]
    if len(modem_candidates) == 1:
        return modem_candidates[0]
    candidates = st_candidates or modem_candidates
    if candidates:
        raise CliError(
            "Could not safely auto-select the NUCLEO encoder port. "
            f"Candidates: {', '.join(candidates)}. Pass --encoder-port explicitly."
        )
    raise CliError("Could not find a NUCLEO encoder serial port. Run list-ports and pass --encoder-port.")


def cmd_capture_position_segmented(args: argparse.Namespace) -> int:
    left_port, right_port = resolve_dual_mlx_ports(args.left_mlx_port, args.right_mlx_port)
    encoder_port = resolve_encoder_port(args.encoder_port, (left_port, right_port))
    tasi_port = "" if args.no_tasi else (args.tasi_port or select_default_tasi_port())
    capture_root = Path(args.capture_root)
    session_dir = capture_root / datetime.now().strftime("mac_dual_mlx_tasi_low_delay_%Y%m%d_%H%M%S")
    native_path = Path(args.native_library) if args.native_library else None
    left_refresh_rate_hz = args.left_refresh_rate_hz if args.left_refresh_rate_hz is not None else args.refresh_rate_hz
    right_refresh_rate_hz = args.right_refresh_rate_hz if args.right_refresh_rate_hz is not None else args.refresh_rate_hz
    channel_specs = [
        (safe_channel_name(args.left_channel), left_port, left_refresh_rate_hz),
        (safe_channel_name(args.right_channel), right_port, right_refresh_rate_hz),
    ]
    if channel_specs[0][0] == channel_specs[1][0]:
        raise CliError("Left and right MLX channel names must be different")
    if left_port == right_port:
        raise CliError("Left and right MLX ports must be different")
    if encoder_port in (left_port, right_port) or (tasi_port and encoder_port == tasi_port):
        raise CliError("Encoder, MLX, and TA612 serial ports must be different")

    segments = build_position_segments(
        args.travel_start_mm,
        args.travel_end_mm,
        args.segment_width_mm,
        args.segment_step_mm,
        args.anchor_mm,
    )
    stop_event = threading.Event()
    print_lock = threading.Lock()

    with contextlib.ExitStack() as stack:
        prepared = []
        for channel, port, refresh_rate_hz in channel_specs:
            bus = stack.enter_context(Usb2UartSerialI2c(port, args.mlx_baud, args.mlx_timeout, args.debug_mlx_wire))
            uid = bus.read_uid()
            mlx = Mlx90640Device(bus, args.address, args.read_chunk_words, args.read_mode)
            mlx.configure_bus(args.i2c_rate, args.stretch)
            time.sleep(max(0.0, args.startup_delay))
            eeprom = mlx.read_eeprom()
            ensure_mlx_eeprom_looks_valid(eeprom)
            control_verify = mlx.configure_operating_mode(refresh_rate_hz)["refresh"][1]
            calc = stack.enter_context(MlxNativeCalculator(native_path))
            rc = calc.extract_parameters(eeprom)
            if rc != 0:
                raise CliError(f"{channel}: MlxExtractParameters failed with code {rc}")
            writer = stack.enter_context(
                MlxCaptureWriter(
                    capture_root,
                    {"kind": "mac_dual_mlx_tasi_position_segmented_channel", "channel": channel},
                    eeprom,
                    session_prefix="mac_dual_mlx_tasi_low_delay",
                    session_dir=session_dir,
                    channel=channel,
                    write_session_json=False,
                )
            )
            prepared.append(
                {
                    "channel": channel,
                    "port": port,
                    "uid": uid.hex(),
                    "control": control_verify,
                    "refresh_rate_hz": refresh_rate_hz,
                    "bus": bus,
                    "mlx": mlx,
                    "calc": calc,
                    "writer": writer,
                    "files": writer.file_entries(),
                }
            )

        raw_files = {
            "encoderPositionCsv": "encoder_position.csv",
            "tasiSerialFramesBin": "raw/tasi_serial_frames.bin",
            "tasiSerialFramesCsv": "tasi_serial_frames.csv",
            "joinedSummaryCsv": "joined_summary.csv",
            "triggerEventsCsv": "trigger_events.csv",
            "triggerWindowSummaryCsv": "trigger_window_summary.csv",
        }
        for item in prepared:
            raw_files.update(prefix_dict_keys(str(item["channel"]), item["files"]))

        metadata = {
            "createdEast8": east8_now().isoformat(),
            "kind": "mac_dual_mlx_tasi_position_segmented_low_delay",
            "lowDelayMode": "devices_initialized_once_continuous_capture_position_segments",
            "positionSegmentation": {
                "encoderPort": encoder_port,
                "encoderBaud": args.encoder_baud,
                "travelStartMm": args.travel_start_mm,
                "travelEndMm": args.travel_end_mm,
                "segmentWidthMm": args.segment_width_mm,
                "segmentStepMm": args.segment_step_mm,
                "anchorMm": args.anchor_mm,
                "targetToleranceMm": args.target_tolerance_mm,
                "startAfterDeltaMm": args.start_after_delta_mm,
                "direction": args.direction,
                "minAbsSpeedMmS": args.min_abs_speed_mm_s,
                "selectionPolicy": "one_closest_frame_per_channel_when_encoder_crosses_target_center",
                "segments": [
                    {
                        "index": segment.index,
                        "startMm": segment.start_mm,
                        "endMm": segment.end_mm,
                        "centerMm": segment.center_mm,
                    }
                    for segment in segments
                ],
            },
            "durationSeconds": args.duration_seconds,
            "warmupSeconds": args.warmup_seconds,
            "mlxAddress": args.address,
            "mlxBaud": args.mlx_baud,
            "mlxTimeoutSeconds": args.mlx_timeout,
            "i2cRate": i2c_rate_name(args.i2c_rate),
            "i2cRateCode": args.i2c_rate,
            "i2cClockStretch": args.stretch,
            "mlxReadChunkWords": args.read_chunk_words,
            "mlxReadMode": args.read_mode,
            "mlxStartupDelaySeconds": args.startup_delay,
            "refreshRateHz": args.refresh_rate_hz,
            "leftRefreshRateHz": left_refresh_rate_hz,
            "rightRefreshRateHz": right_refresh_rate_hz,
            "mlxRefreshRateUnit": "subpages_per_second",
            "mlxFramePolicy": "strict_full_frame_after_both_subpages",
            "adcResolution": "18-bit",
            "mode": "chess",
            "emissivity": args.emissivity,
            "nativeLibrary": str(native_path or default_native_library_path() or ""),
            "tasiEnabled": not args.no_tasi,
            "tasiPort": tasi_port,
            "tasiBaud": args.tasi_baud,
            "tasiTimeoutSeconds": args.tasi_timeout,
            "tasiPollIntervalSeconds": args.tasi_poll_interval,
            "joinPolicy": "Continuous capture; trigger_window_summary.csv groups MLX frames by encoder x_mm position segments.",
            "mlxChannels": [
                {
                    "channel": str(item["channel"]),
                    "port": str(item["port"]),
                    "usb2uartUid": str(item["uid"]),
                    "refreshRateHz": item["refresh_rate_hz"],
                    "controlRegister": f"0x{int(item['control']):04X}",
                    "files": item["files"],
                }
                for item in prepared
            ],
            "rawFiles": raw_files,
        }
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        with EncoderPositionPoller(
            encoder_port,
            args.encoder_baud,
            args.encoder_timeout,
            session_dir / "encoder_position.csv",
            debug=args.debug_encoder,
        ) as encoder_poller:
            with TasiSerialCaptureWriter(session_dir) as tasi_writer:
                with JoinedSummaryWriter(session_dir, include_mlx_channel=True) as joined_writer:
                    with PositionSegmentSummaryWriter(
                        session_dir,
                        segments,
                        encoder_poller,
                        include_mlx_channel=True,
                        direction=args.direction,
                        min_abs_speed_mm_s=args.min_abs_speed_mm_s,
                        target_tolerance_mm=args.target_tolerance_mm,
                        channel_names=[str(item["channel"]) for item in prepared],
                        ) as segment_writer:
                        tasi_context = (
                            NullTasiPoller()
                            if args.no_tasi
                            else TasiSerialPoller(
                                tasi_port,
                                args.tasi_baud,
                                args.tasi_timeout,
                                args.tasi_poll_interval,
                                tasi_writer,
                                read_size=args.tasi_read_size,
                                stop_first=args.tasi_stop_first,
                                stop_on_exit=args.tasi_stop_on_exit,
                                command_delay=args.tasi_command_delay,
                                accept_alt_header=args.tasi_accept_alt_header,
                                debug_wire=args.debug_tasi_wire,
                            )
                        )
                        with tasi_context as tasi_poller:
                            workers = [
                                MlxChannelCaptureWorker(
                                    str(item["channel"]),
                                    item["mlx"],
                                    item["calc"],
                                    item["writer"],
                                    tasi_poller,
                                    joined_writer,
                                    stop_event,
                                    args.poll_interval,
                                    args.max_polls,
                                    args.emissivity,
                                    frame_limit=0,
                                    print_every=args.print_every,
                                    print_lock=print_lock,
                                    refresh_rate_hz=item["refresh_rate_hz"],
                                    trigger_window_writer=segment_writer,
                                    quiet_live=args.quiet_live,
                                )
                                for item in prepared
                            ]
                            print(f"Position segmented capture directory: {session_dir}")
                            print(f"MLX left:   {left_port} @ {args.mlx_baud}")
                            print(f"MLX right:  {right_port} @ {args.mlx_baud}")
                            print(
                                "TA612:      disabled"
                                if args.no_tasi
                                else f"TA612:      {tasi_port} @ {args.tasi_baud}"
                            )
                            print(f"Encoder:    {encoder_port} @ {args.encoder_baud}")
                            print(
                                f"Targets: {len(segments)} x positions, anchor={args.anchor_mm:g}mm, "
                                f"FOV width={args.segment_width_mm:g}mm, step={args.segment_step_mm:g}mm, "
                                f"tolerance=±{args.target_tolerance_mm:g}mm, direction={args.direction}"
                            )
                            start = time.monotonic()
                            try:
                                for worker in workers:
                                    worker.start()
                                if args.warmup_seconds > 0:
                                    print(f"Warming up for {args.warmup_seconds:.2f}s...")
                                    warmup_deadline = time.monotonic() + args.warmup_seconds
                                    while time.monotonic() < warmup_deadline:
                                        time.sleep(0.05)
                                        encoder_poller.check_error()
                                        tasi_poller.check_error()
                                        for worker in workers:
                                            worker.check_error()
                                start_after_delta_mm = max(0.0, float(args.start_after_delta_mm))
                                baseline_sample = encoder_poller.latest()
                                baseline_x_mm = baseline_sample.x_mm if baseline_sample is not None else None
                                if start_after_delta_mm > 0:
                                    print(
                                        f"Waiting until encoder x changes {start_after_delta_mm:g}mm "
                                        "before starting timed position segmentation..."
                                    )
                                    while True:
                                        time.sleep(0.02)
                                        encoder_poller.check_error()
                                        tasi_poller.check_error()
                                        for worker in workers:
                                            worker.check_error()
                                        latest = encoder_poller.latest()
                                        if latest is None:
                                            continue
                                        if baseline_x_mm is None:
                                            baseline_x_mm = latest.x_mm
                                        moved_mm = latest.x_mm - baseline_x_mm
                                        if args.direction == "increasing":
                                            reached = moved_mm >= start_after_delta_mm
                                        elif args.direction == "decreasing":
                                            reached = -moved_mm >= start_after_delta_mm
                                        else:
                                            reached = abs(moved_mm) >= start_after_delta_mm
                                        if reached:
                                            print(
                                                f"Start threshold reached: baseline={baseline_x_mm:.3f}mm, "
                                                f"current={latest.x_mm:.3f}mm, moved={moved_mm:.3f}mm"
                                            )
                                            break
                                segment_writer.enable()
                                timed_start = time.monotonic()
                                print(
                                    "Position segmentation started. "
                                    "Press Ctrl-C to stop early."
                                )
                                deadline = (
                                    timed_start + args.duration_seconds
                                    if args.duration_seconds > 0
                                    else None
                                )
                                while True:
                                    if deadline is not None and time.monotonic() >= deadline:
                                        break
                                    time.sleep(0.05)
                                    encoder_poller.check_error()
                                    tasi_poller.check_error()
                                    for worker in workers:
                                        worker.check_error()
                                    if args.stop_after_all_targets and segment_writer.is_complete():
                                        print("All target positions captured for all MLX channels.")
                                        break
                                    if args.quiet_live:
                                        latest = encoder_poller.latest()
                                        if latest is not None and int((time.monotonic() - start) * 10) % 10 == 0:
                                            print(
                                                format_trigger_temperature_summary(
                                                    0,
                                                    east8_now(),
                                                    workers,
                                                    tasi_poller.latest(),
                                                )
                                                + f" | x={latest.x_mm:.2f}mm"
                                            )
                            except KeyboardInterrupt:
                                print("Stopping after Ctrl-C...")
                            finally:
                                stop_event.set()
                                join_timeout = max(2.0, args.mlx_timeout + args.max_polls * args.poll_interval + 1.0)
                                for worker in workers:
                                    worker.join(timeout=join_timeout)
                                for item in prepared:
                                    item["writer"].write_i2c_stats(item["bus"].i2c_stats)
                            encoder_poller.check_error()
                            tasi_poller.check_error()
                            for worker in workers:
                                worker.check_error()
                            elapsed = max(time.monotonic() - start, 1e-6)
                            summary_text = ", ".join(
                                f"{worker.channel}: full_frames={worker.frames} subpages={worker.subpages} "
                                f"full_fps={worker.frames / elapsed:.2f} {worker.timing_stats.summary_text()} "
                                f"{worker.mlx.bus.i2c_stats.summary_text()}"
                                for worker in workers
                            )
                            print(
                                f"Stopped position segmented capture. {summary_text}, "
                                f"encoder_samples={encoder_poller.frames}, tasi_frames={tasi_poller.frames}, "
                                f"output={session_dir}"
                            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously capture dual MLX90640 + TA612 and group frames by NUCLEO encoder position."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_ports = subparsers.add_parser("list-ports", help="List macOS serial ports")
    list_ports.set_defaults(func=lambda args: _cmd_list_ports())

    capture = subparsers.add_parser(
        "capture-position-segmented-dual-combined-low-delay",
        help="Continuous dual MLX capture; classify frames into position segments using encoder x_mm.",
    )
    capture.add_argument("--encoder-port", default=None, help="NUCLEO encoder serial port, e.g. /dev/cu.usbmodem2124303")
    capture.add_argument("--encoder-baud", type=int, default=115200, help="NUCLEO encoder serial baud rate")
    capture.add_argument("--encoder-timeout", type=float, default=0.2, help="Encoder serial read timeout seconds")
    capture.add_argument("--travel-start-mm", type=float, default=0.0, help="Physical start position in mm")
    capture.add_argument("--travel-end-mm", type=float, default=600.0, help="Physical end position in mm")
    capture.add_argument("--anchor-mm", type=float, default=300.0, help="Main target x position; default is sensor center at 300mm")
    capture.add_argument("--segment-width-mm", type=float, default=110.0, help="Position segment width in mm; default MLX FOV along motion")
    capture.add_argument("--segment-step-mm", type=float, default=55.0, help="Distance between segment starts in mm")
    capture.add_argument("--target-tolerance-mm", type=float, default=25.0, help="Maximum accepted distance between encoder x and target center")
    capture.add_argument(
        "--direction",
        choices=("increasing", "decreasing", "any"),
        default="increasing",
        help="Which encoder motion direction to accept into position segments",
    )
    capture.add_argument("--min-abs-speed-mm-s", type=float, default=1.0, help="Ignore stationary/near-stationary encoder samples")
    capture.add_argument("--duration-seconds", type=float, default=2.5, help="Capture duration after warmup; 0 means until Ctrl-C")
    capture.add_argument("--warmup-seconds", type=float, default=1.0, help="Seconds to capture before accepting position segments")
    capture.add_argument("--start-after-delta-mm", type=float, default=0.0, help="After warmup, wait until encoder x changes this many mm before starting timed segmentation")
    capture.add_argument("--stop-after-all-targets", action=argparse.BooleanOptionalAction, default=True, help="Stop after every target x position has one frame for each MLX channel")
    capture.add_argument("--left-mlx-port", default=None, help="Left MLX USB2UART serial port")
    capture.add_argument("--right-mlx-port", default=None, help="Right MLX USB2UART serial port")
    capture.add_argument("--left-channel", default="left", help="Left MLX channel name used in output files")
    capture.add_argument("--right-channel", default="right", help="Right MLX channel name used in output files")
    capture.add_argument("--mlx-baud", type=int, default=DEFAULT_BAUD, help=f"MLX USB2UART baud rate, default: {DEFAULT_BAUD}")
    capture.add_argument("--mlx-timeout", type=float, default=2.0, help="MLX serial read/write timeout seconds")
    capture.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_MLX_ADDRESS, help="MLX90640 I2C address on each independent bus")
    capture.add_argument("--i2c-rate", type=parse_i2c_rate, default=parse_i2c_rate("1m"), help="MLX I2C rate: 400k, 600k, 800k, 1m, or numeric code")
    capture.add_argument("--stretch", type=int, default=DEFAULT_I2C_STRETCH, help="MLX I2C clock stretch cycles")
    capture.add_argument("--read-chunk-words", type=int, default=DEFAULT_MLX_READ_CHUNK_WORDS, help="MLX register read chunk size in words")
    capture.add_argument("--read-mode", choices=READ_MODES, default=DEFAULT_READ_MODE, help="MLX register read path")
    capture.add_argument(
        "--refresh-rate-hz",
        type=parse_refresh_rate_hz,
        default=DEFAULT_REFRESH_RATE_HZ,
        help="MLX90640 refresh rate Hz: 0.5, 1, 2, 4, 8, 16, 32, or 64; default: 8",
    )
    capture.add_argument("--left-refresh-rate-hz", type=parse_refresh_rate_hz, default=None, help="Left MLX refresh rate; default: use --refresh-rate-hz")
    capture.add_argument("--right-refresh-rate-hz", type=parse_refresh_rate_hz, default=None, help="Right MLX refresh rate; default: use --refresh-rate-hz")
    capture.add_argument("--startup-delay", type=float, default=DEFAULT_STARTUP_DELAY_SECONDS, help="Delay after MLX I2C setup")
    capture.add_argument("--poll-interval", type=float, default=0.002, help="MLX data-ready poll interval seconds")
    capture.add_argument("--max-polls", type=int, default=2000, help="Maximum MLX polls per subpage")
    capture.add_argument("--emissivity", type=float, default=DEFAULT_EMISSIVITY, help="Object emissivity")
    capture.add_argument("--native-library", default=None, help="Path to libMlx90640Native.dylib")
    capture.add_argument("--no-tasi", action="store_true", help="Disable TA612 polling and write empty TA612 files")
    capture.add_argument("--tasi-port", default=None, help="TA612C serial port, default: CH340 /dev/cu.usbserial*")
    capture.add_argument("--tasi-baud", type=int, default=DEFAULT_TASI_BAUD, help="TA612C serial baud rate")
    capture.add_argument("--tasi-timeout", type=float, default=0.2, help="TA612C serial timeout seconds")
    capture.add_argument("--tasi-poll-interval", type=float, default=0.25, help="Seconds between TA612 realtime commands")
    capture.add_argument("--tasi-read-size", type=int, default=64, help="TA612C serial read chunk size")
    capture.add_argument("--tasi-stop-first", action="store_true", help="Send TA612 stop command and flush before starting")
    capture.add_argument("--tasi-stop-on-exit", action=argparse.BooleanOptionalAction, default=True, help="Send TA612 stop command before closing")
    capture.add_argument("--tasi-command-delay", type=float, default=0.1, help="Delay after TA612 stop-first command")
    capture.add_argument("--tasi-accept-alt-header", action="store_true", help="Also accept 0x55AA host-order TA612 header while debugging")
    capture.add_argument("--capture-root", default="captures", help="Output root directory")
    capture.add_argument("--print-every", type=int, default=32, help="Print every N MLX frames per channel")
    capture.add_argument("--quiet-live", action="store_true", help="Reduce live MLX logging")
    capture.add_argument("--debug-encoder", action="store_true", help="Print parsed encoder samples")
    capture.add_argument("--debug-mlx-wire", action="store_true", help="Print raw MLX USB2UART protocol bytes")
    capture.add_argument("--debug-tasi-wire", action="store_true", help="Print raw TA612 serial TX/RX bytes")
    capture.set_defaults(func=cmd_capture_position_segmented)
    return parser


def _cmd_list_ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return 1
    for idx, port in enumerate(ports):
        vid = port.get("vid")
        pid = port.get("pid")
        vid_pid = f"{vid:04X}:{pid:04X}" if isinstance(vid, int) and isinstance(pid, int) else "----:----"
        print(
            f"[{idx}] {port.get('device')}  {vid_pid}  "
            f"{port.get('description') or ''}  serial={port.get('serial_number') or ''}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
