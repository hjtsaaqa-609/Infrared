#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from macos_infrared_triggered_cl_low_delay import (  # noqa: E402
    AUX_WORDS,
    CliError,
    EEPROM_WORDS,
    FRAME_DATA_WORDS,
    INIT_STATUS_VALUE,
    MlxCaptureWriter,
    MlxNativeCalculator,
    MlxRawSubpage,
    PIXEL_WORDS,
    default_native_library_path,
    east8_iso,
    east8_now,
    ensure_mlx_eeprom_looks_valid,
    import_serial,
    list_serial_ports,
    safe_channel_name,
)


KEY_VALUE_RE = re.compile(r"(\w+)=([^\s]+)")


@dataclass
class FrameAssembly:
    seq: int
    index: int
    target_um: int
    actual_um: int
    elapsed_ms: int
    status: int
    subpage: int
    control: int
    part: int
    words: list[int]


def parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in KEY_VALUE_RE.finditer(line)}


def parse_hex_words(text: str) -> list[int]:
    return [int(part, 16) for part in text.strip().split() if part]


def read_eeprom_file(path: Path) -> list[int]:
    data = path.read_bytes()
    expected_bytes = EEPROM_WORDS * 2
    if len(data) < expected_bytes:
        raise CliError(f"EEPROM file is too small: {path} has {len(data)} bytes, expected {expected_bytes}")
    return [int.from_bytes(data[index * 2 : index * 2 + 2], "little") for index in range(EEPROM_WORDS)]


def parse_target_positions_mm(text: str) -> list[float]:
    values: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        values.append(float(part))
    if not values:
        raise CliError("At least one target position is required")
    return values


def default_stm32_port() -> str:
    ports = list_serial_ports()
    candidates: list[str] = []
    for port in ports:
        device = str(port.get("device") or "")
        text = " ".join(
            str(port.get(key) or "")
            for key in ("device", "description", "manufacturer", "product", "serial_number")
        ).lower()
        if device.startswith("/dev/cu.usbmodem") and ("stlink" in text or "st-link" in text or "stm" in text):
            candidates.append(device)
    if len(candidates) == 1:
        return candidates[0]
    modem_ports = [str(port.get("device")) for port in ports if str(port.get("device") or "").startswith("/dev/cu.usbmodem")]
    if len(modem_ports) == 1:
        return modem_ports[0]
    if candidates or modem_ports:
        raise CliError(
            "Could not safely auto-select the STM32 serial port. "
            f"Candidates: {', '.join(candidates or modem_ports)}. Pass --port explicitly."
        )
    raise CliError("No /dev/cu.usbmodem* STM32 serial port found. Pass --port explicitly.")


class TargetCaptureSession:
    def __init__(
        self,
        capture_root: Path,
        channel: str,
        native_library: Path | None,
        target_width_mm: float,
        capture_mode: str = "target",
        target_positions_mm: Sequence[float] = (),
        single_read: bool = False,
    ) -> None:
        self.capture_root = capture_root
        self.channel = safe_channel_name(channel)
        self.native_library = native_library
        self.target_width_mm = target_width_mm
        self.capture_mode = capture_mode
        self.target_positions_mm = list(target_positions_mm)
        self.single_read = single_read
        self.session_dir = capture_root / datetime.now().strftime("mac_dual_mlx_tasi_low_delay_%Y%m%d_%H%M%S")
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.writer: MlxCaptureWriter | None = None
        self.calc: MlxNativeCalculator | None = None
        self.events_file = None
        self.summary_file = None
        self.events_csv = None
        self.summary_csv = None
        self.frame_count = 0
        self.pending_subpages: dict[int, set[int]] = {}
        self.pending_target_meta: dict[int, FrameAssembly] = {}
        self.completed_seqs: set[int] = set()
        self.frame_records: list[dict[str, float | int]] = []

    def reset_inflight_frames(self) -> None:
        self.pending_subpages.clear()
        self.pending_target_meta.clear()
        self.completed_seqs.clear()

    def start(self, eeprom_words: Sequence[int], serial_port: str, baud: int) -> None:
        ensure_mlx_eeprom_looks_valid(eeprom_words)
        self.calc = MlxNativeCalculator(self.native_library)
        rc = self.calc.extract_parameters(eeprom_words)
        if rc != 0:
            raise CliError(f"MlxExtractParameters failed with code {rc}")
        metadata = {
            "createdEast8": east8_now().isoformat(),
            "kind": "stm32_position_target_mlx_low_delay" if self.capture_mode == "target" else "stm32_continuous_position_mlx_low_delay",
            "lowDelayMode": "stm32_encoder_crosses_target_x_then_reads_both_mlx_subpages"
            if self.capture_mode == "target"
            else "stm32_continuously_reads_mlx_frames_with_encoder_position; select nearest target positions on Mac",
            "stm32SerialPort": serial_port,
            "stm32SerialBaud": baud,
            "mlxChannel": self.channel,
            "mlxFramePolicy": "two_subpages_per_target_position; save temperature image after both subpages arrive",
            "mlxSingleReadMode": self.single_read,
            "positionSegmentation": {
                "selectionPolicy": "stm32_captures_when_encoder_crosses_target_center"
                if self.capture_mode == "target"
                else "mac_selects_nearest_streamed_frame_by_actual_position",
                "segmentWidthMm": self.target_width_mm,
            },
            "nativeLibrary": str(self.native_library or default_native_library_path() or ""),
            "tasiEnabled": False,
            "rawFiles": {
                "triggerEventsCsv": "trigger_events.csv",
                "triggerWindowSummaryCsv": "trigger_window_summary.csv",
                "joinedSummaryCsv": "joined_summary.csv",
                "encoderPositionCsv": "encoder_position.csv",
                "tasiSerialFramesCsv": "tasi_serial_frames.csv",
            },
        }
        self.writer = MlxCaptureWriter(
            self.capture_root,
            {"kind": "stm32_position_target_channel", "channel": self.channel},
            eeprom_words,
            session_prefix="mac_dual_mlx_tasi_low_delay",
            session_dir=self.session_dir,
            channel=self.channel,
            write_session_json=False,
        )
        metadata["rawFiles"].update({f"{self.channel}_{key}": value for key, value in self.writer.file_entries().items()})
        (self.session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        (self.session_dir / "encoder_position.csv").write_text(
            "timestamp_east8,x_mm,line\n",
            encoding="utf-8",
        )
        (self.session_dir / "joined_summary.csv").write_text("", encoding="utf-8")
        (self.session_dir / "tasi_serial_frames.csv").write_text("", encoding="utf-8")
        self.events_file = (self.session_dir / "trigger_events.csv").open("w", newline="", encoding="utf-8")
        self.summary_file = (self.session_dir / "trigger_window_summary.csv").open("w", newline="", encoding="utf-8")
        self.events_csv = csv.writer(self.events_file)
        self.summary_csv = csv.writer(self.summary_file)
        self.events_csv.writerow(
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
        self.summary_csv.writerow(
            [
                "trigger_index",
                "trigger_offset_ms",
                "mlx_channel",
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
        )
        print(f"Capture directory: {self.session_dir}")

    def close(self) -> None:
        self.write_nearest_target_summary()
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.calc is not None:
            self.calc.close()
            self.calc = None
        for handle_name in ("events_file", "summary_file"):
            handle = getattr(self, handle_name)
            if handle is not None:
                handle.close()
                setattr(self, handle_name, None)

    def write_nearest_target_summary(self) -> None:
        if self.capture_mode != "stream" or not self.frame_records or not self.target_positions_mm:
            return
        path = self.session_dir / "nearest_target_summary.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "target_mm",
                    "nearest_frame_seq",
                    "nearest_trigger_index",
                    "actual_mm",
                    "error_mm",
                    "abs_error_mm",
                    "max_c",
                    "avg_c",
                    "center_c",
                ]
            )
            used: set[int] = set()
            for target_mm in self.target_positions_mm:
                candidates = [record for record in self.frame_records if int(record["seq"]) not in used]
                if not candidates:
                    candidates = self.frame_records
                nearest = min(candidates, key=lambda record: abs(float(record["actual_mm"]) - target_mm))
                used.add(int(nearest["seq"]))
                error_mm = float(nearest["actual_mm"]) - target_mm
                writer.writerow(
                    [
                        f"{target_mm:.6f}",
                        int(nearest["seq"]),
                        int(nearest["trigger_index"]),
                        f"{float(nearest['actual_mm']):.6f}",
                        f"{error_mm:.6f}",
                        f"{abs(error_mm):.6f}",
                        f"{float(nearest['max_c']):.6f}",
                        f"{float(nearest['avg_c']):.6f}",
                        f"{float(nearest['center_c']):.6f}",
                    ]
                )
        print(f"Nearest target summary: {path}")

    def write_frame(self, frame: FrameAssembly) -> bool:
        if self.writer is None or self.calc is None or self.events_csv is None or self.summary_csv is None:
            raise CliError("EEPROM has not been received yet. Start this script, then press the black NUCLEO reset button.")
        if len(frame.words) != FRAME_DATA_WORDS:
            raise CliError(f"Frame seq={frame.seq} has {len(frame.words)} words, expected {FRAME_DATA_WORDS}")
        reported_subpage = int(frame.subpage) & 1
        if frame.seq in self.completed_seqs:
            print(f"ignored seq={frame.seq} part={frame.part} subpage={reported_subpage}; seq already completed")
            return False
        timestamp = east8_now()
        raw = MlxRawSubpage(timestamp, frame.status, frame.control, 0, frame.words)
        self.writer.write_subpage(raw)
        calculated_subpage, ta_c, temperatures = self.calc.calculate(frame.words)
        reported_subpage = int(calculated_subpage) & 1
        seen = self.pending_subpages.setdefault(frame.seq, set())
        if reported_subpage in seen:
            print(
                f"ignored seq={frame.seq} part={frame.part} "
                f"stm32_subpage={frame.subpage} calculated_subpage={reported_subpage}; duplicate subpage"
            )
            return False
        if self.single_read:
            summary = self.writer.write_frame(timestamp, reported_subpage, ta_c, temperatures)
            self.completed_seqs.add(frame.seq)
            target_mm = frame.target_um / 1000.0
            actual_mm = frame.actual_um / 1000.0
            half_width = self.target_width_mm / 2.0
            start_mm = max(0.0, target_mm - half_width)
            end_mm = min(600.0, target_mm + half_width)
            event_ts = east8_iso(timestamp)
            self.events_csv.writerow(
                [
                    frame.index,
                    event_ts,
                    event_ts,
                    event_ts,
                    "0.000000",
                    "stm32",
                    "position-target-single-read",
                    f"seq={frame.seq}",
                    f"{start_mm:.6f}",
                    f"{end_mm:.6f}",
                    f"{target_mm:.6f}",
                    1,
                ]
            )
            self.summary_csv.writerow(
                [
                    frame.index,
                    "0.000",
                    self.channel,
                    event_ts,
                    summary.subpage,
                    summary.to_offset_bytes,
                    "" if summary.robot_thermal_u8_offset_bytes is None else summary.robot_thermal_u8_offset_bytes,
                    f"{summary.ta_c:.6f}",
                    f"{summary.min_c:.6f}",
                    f"{summary.max_c:.6f}",
                    f"{summary.avg_c:.6f}",
                    f"{summary.center_c:.6f}",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    f"{actual_mm:.6f}",
                    "",
                    "",
                    f"{start_mm:.6f}",
                    f"{end_mm:.6f}",
                    f"{target_mm:.6f}",
                ]
            )
            self.events_file.flush()
            self.summary_file.flush()
            self.frame_count += 1
            self.frame_records.append(
                {
                    "seq": frame.seq,
                    "trigger_index": frame.index,
                    "actual_mm": actual_mm,
                    "target_mm": target_mm,
                    "max_c": summary.max_c,
                    "avg_c": summary.avg_c,
                    "center_c": summary.center_c,
                }
            )
            print(
                f"saved single-read seq={frame.seq} target={target_mm:.1f}mm actual={actual_mm:.1f}mm "
                f"subpage={summary.subpage} max={summary.max_c:.2f}C avg={summary.avg_c:.2f}C"
            )
            return True
        seen.add(reported_subpage)
        self.pending_target_meta[frame.seq] = frame
        if seen != {0, 1}:
            print(
                f"received seq={frame.seq} part={frame.part} subpage={reported_subpage}; "
                "waiting for the other subpage"
            )
            return False
        if (int(calculated_subpage) & 1) != reported_subpage:
            print(
                f"warning seq={frame.seq}: STM32 subpage={reported_subpage}, "
                f"native subpage={calculated_subpage}"
            )
        summary = self.writer.write_frame(timestamp, reported_subpage, ta_c, temperatures)
        frame = self.pending_target_meta.pop(frame.seq, frame)
        self.pending_subpages.pop(frame.seq, None)
        self.completed_seqs.add(frame.seq)
        target_mm = frame.target_um / 1000.0
        actual_mm = frame.actual_um / 1000.0
        half_width = self.target_width_mm / 2.0
        start_mm = max(0.0, target_mm - half_width)
        end_mm = min(600.0, target_mm + half_width)
        event_ts = east8_iso(timestamp)
        self.events_csv.writerow(
            [
                frame.index,
                event_ts,
                event_ts,
                event_ts,
                "0.000000",
                "stm32",
                "position-target",
                f"seq={frame.seq}",
                f"{start_mm:.6f}",
                f"{end_mm:.6f}",
                f"{target_mm:.6f}",
                1,
            ]
        )
        self.summary_csv.writerow(
            [
                frame.index,
                "0.000",
                self.channel,
                event_ts,
                summary.subpage,
                summary.to_offset_bytes,
                "" if summary.robot_thermal_u8_offset_bytes is None else summary.robot_thermal_u8_offset_bytes,
                f"{summary.ta_c:.6f}",
                f"{summary.min_c:.6f}",
                f"{summary.max_c:.6f}",
                f"{summary.avg_c:.6f}",
                f"{summary.center_c:.6f}",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                f"{actual_mm:.6f}",
                "",
                "",
                f"{start_mm:.6f}",
                f"{end_mm:.6f}",
                f"{target_mm:.6f}",
            ]
        )
        self.events_file.flush()
        self.summary_file.flush()
        self.frame_count += 1
        self.frame_records.append(
            {
                "seq": frame.seq,
                "trigger_index": frame.index,
                "actual_mm": actual_mm,
                "target_mm": target_mm,
                "max_c": summary.max_c,
                "avg_c": summary.avg_c,
                "center_c": summary.center_c,
            }
        )
        print(
            f"saved seq={frame.seq} target={target_mm:.1f}mm actual={actual_mm:.1f}mm "
            f"max={summary.max_c:.2f}C avg={summary.avg_c:.2f}C"
        )
        return True


def cmd_capture(args: argparse.Namespace) -> int:
    serial = import_serial()
    port = args.port or default_stm32_port()
    native = Path(args.native_library) if args.native_library else None
    target_positions_mm = parse_target_positions_mm(args.select_targets_mm)
    session = TargetCaptureSession(
        Path(args.capture_root),
        args.channel,
        native,
        args.target_width_mm,
        args.capture_mode,
        target_positions_mm,
        args.single_read,
    )
    eeprom_words: list[int | None] = []
    frame: FrameAssembly | None = None
    serial_errors = (getattr(serial, "SerialException", OSError), OSError)
    if args.eeprom_file:
        eeprom_path = Path(args.eeprom_file)
        session.start(read_eeprom_file(eeprom_path), port, args.baud)
        print(f"Using EEPROM file: {eeprom_path}")
    runtime_config = (
        f"TCAP_SET refresh_hz={args.mlx_refresh_hz} "
        f"i2c_hz={args.i2c_hz} "
        f"subpage_attempts={args.subpage_attempts} "
        f"start_after_mm={args.start_after_mm} "
        f"single_read={1 if args.single_read else 0} "
        f"skip_eeprom={1 if args.eeprom_file else 0} "
        f"mode={args.capture_mode}\n"
    ).encode("ascii")
    print(f"Listening on {port} at {args.baud}. Press the black NUCLEO reset button if EEPROM does not appear.")
    done = False
    try:
        while not done:
            try:
                ser = serial.Serial(port, args.baud, timeout=0.5)
            except serial_errors as exc:
                print(f"Serial port unavailable: {exc}. Retrying in {args.reconnect_delay:.1f}s...")
                time.sleep(args.reconnect_delay)
                continue
            with ser:
                try:
                    ser.reset_input_buffer()
                except serial_errors:
                    pass
                try:
                    ser.write(runtime_config)
                    ser.flush()
                    print(
                        "Sent STM32 config: "
                        f"mlx_refresh_hz={args.mlx_refresh_hz}, "
                        f"i2c_hz={args.i2c_hz}, "
                        f"subpage_attempts={args.subpage_attempts}, "
                        f"start_after_mm={args.start_after_mm}, "
                        f"single_read={1 if args.single_read else 0}, "
                        f"skip_eeprom={1 if args.eeprom_file else 0}, "
                        f"mode={args.capture_mode}"
                    )
                except serial_errors as exc:
                    print(f"Could not send STM32 config: {exc}")
                print(f"Serial connected: {port}")
                disconnected = False
                config_acknowledged = False
                last_config_send_at = 0.0
                capture_started_at: float | None = None
                while not done:
                    if (
                        args.capture_mode == "stream"
                        and args.duration_seconds > 0
                        and capture_started_at is not None
                        and (time.monotonic() - capture_started_at) >= args.duration_seconds
                    ):
                        print(f"Stream duration reached: {args.duration_seconds:.3f}s")
                        done = True
                        break
                    if not config_acknowledged and (time.monotonic() - last_config_send_at) >= args.config_resend_interval:
                        try:
                            ser.write(runtime_config)
                            ser.flush()
                            last_config_send_at = time.monotonic()
                        except serial_errors as exc:
                            print(f"Could not resend STM32 config: {exc}")
                            frame = None
                            disconnected = True
                            break
                    try:
                        raw = ser.readline()
                    except serial_errors as exc:
                        print(f"Serial disconnected: {exc}. Waiting for {port} to return...")
                        frame = None
                        disconnected = True
                        break
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if args.echo:
                        print(line)
                    if line.startswith("encoder_count_cube ready"):
                        config_acknowledged = False
                        last_config_send_at = 0.0
                        frame = None
                        session.reset_inflight_frames()
                        print("STM32 restarted; resending runtime config.")
                        continue
                    if line.startswith("TCAP_SET_OK"):
                        config_acknowledged = True
                        print(f"STM32 reported: {line}")
                        continue
                    if line.startswith("TCAP_SET_WARNING"):
                        print(f"STM32 reported: {line}")
                        continue
                    if line.startswith("TCAP_STREAM_ARMED"):
                        capture_started_at = time.monotonic()
                        print(f"STM32 reported: {line}")
                        continue
                    if line.startswith("TCAP_EEPROM_BEGIN"):
                        eeprom_words = [None] * EEPROM_WORDS
                        continue
                    if line.startswith("TCAP_EEPROM "):
                        kv = parse_kv(line)
                        offset = int(kv.get("offset", "0"))
                        data = line.split("data=", 1)[1] if "data=" in line else ""
                        words = parse_hex_words(data)
                        eeprom_words[offset : offset + len(words)] = words
                        continue
                    if line.startswith("TCAP_EEPROM_END"):
                        if len(eeprom_words) != EEPROM_WORDS or any(word is None for word in eeprom_words):
                            raise CliError("Incomplete EEPROM received from STM32")
                        if session.writer is None:
                            session.start([int(word) for word in eeprom_words], port, args.baud)
                            if args.capture_mode != "stream" or args.start_after_mm <= 0:
                                capture_started_at = time.monotonic()
                        continue
                    if (
                        line.startswith("TCAP_READY")
                        or line.startswith("TCAP_TIMING")
                        or line.startswith("TCAP_CONFIG ")
                        or line.startswith("TCAP_EEPROM_READ_OK")
                        or line.startswith("TCAP_EEPROM_RETRY")
                    ):
                        print(f"STM32 reported: {line}")
                        continue
                    if line.startswith("TCAP_EEPROM_ERROR") or line.startswith("TCAP_CONFIG_ERROR"):
                        print(f"STM32 reported: {line}")
                        if session.writer is None:
                            print("No EEPROM loaded yet. Press the black reset button, or pass --eeprom-file with a known-good EEPROM.")
                        continue
                    if (
                        line.startswith("TCAP_TARGET")
                        or line.startswith("TCAP_FRAME_PAIR_READY")
                        or line.startswith("TCAP_STREAM_FRAME_READY")
                        or line.startswith("TCAP_SINGLE_FRAME_READY")
                    ):
                        print(f"STM32 reported: {line}")
                        continue
                    if line.startswith("TCAP_FRAME_ERROR") or line.startswith("TCAP_FRAME_INCOMPLETE"):
                        print(f"STM32 reported: {line}")
                        frame = None
                        continue
                    if line.startswith("TCAP_SCAN_DONE"):
                        print(f"STM32 reported: {line}")
                        done = True
                        break
                    if line.startswith("TCAP_FRAME_BEGIN"):
                        if session.writer is None:
                            print("Frame arrived before EEPROM was loaded; ignoring it. Press reset or pass --eeprom-file.")
                            continue
                        kv = parse_kv(line)
                        word_count = int(kv.get("words", str(FRAME_DATA_WORDS)))
                        frame = FrameAssembly(
                            seq=int(kv["seq"]),
                            index=int(kv["index"]),
                            target_um=int(kv["target_um"]),
                            actual_um=int(kv["actual_um"]),
                            elapsed_ms=int(kv.get("elapsed_ms", "0")),
                            status=int(kv.get("status", "0"), 0),
                            subpage=int(kv.get("subpage", "0")),
                            control=int(kv.get("control", "0"), 0),
                            part=int(kv.get("part", "1")),
                            words=[0] * word_count,
                        )
                        continue
                    if line.startswith("TCAP_FRAME_DATA"):
                        if frame is None:
                            continue
                        kv = parse_kv(line)
                        if int(kv.get("seq", "-1")) != frame.seq:
                            continue
                        offset = int(kv.get("offset", "0"))
                        data = line.split("data=", 1)[1] if "data=" in line else ""
                        words = parse_hex_words(data)
                        frame.words[offset : offset + len(words)] = words
                        continue
                    if line.startswith("TCAP_FRAME_BINARY_BEGIN"):
                        if frame is None:
                            continue
                        kv = parse_kv(line)
                        if int(kv.get("seq", "-1")) != frame.seq:
                            continue
                        byte_count = int(kv.get("bytes", "0"))
                        expected_bytes = len(frame.words) * 2
                        if byte_count != expected_bytes:
                            raise CliError(
                                f"Binary frame seq={frame.seq} has {byte_count} bytes, expected {expected_bytes}"
                            )
                        payload = ser.read(byte_count)
                        if len(payload) != byte_count:
                            print(
                                f"Serial disconnected: short binary frame seq={frame.seq} "
                                f"got {len(payload)} of {byte_count} bytes"
                            )
                            frame = None
                            disconnected = True
                            break
                        encoding = kv.get("encoding", "u16be")
                        endian = "<" if encoding == "u16le" else ">"
                        frame.words[:] = list(struct.unpack(f"{endian}{len(frame.words)}H", payload))
                        continue
                    if line.startswith("TCAP_FRAME_END"):
                        if frame is not None:
                            saved = session.write_frame(frame)
                            if saved and args.expected_frames > 0 and session.frame_count >= args.expected_frames:
                                done = True
                                break
                            frame = None
                        continue
                if disconnected and not done:
                    time.sleep(args.reconnect_delay)
    except KeyboardInterrupt:
        print()
    finally:
        session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive STM32 position-target MLX90640 frames and write report sessions.")
    parser.add_argument("--port", default=None, help="STM32 VCP port, e.g. /dev/cu.usbmodem2124403")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--capture-root", default="captures")
    parser.add_argument("--channel", default="left")
    parser.add_argument("--target-width-mm", type=float, default=110.0)
    parser.add_argument("--capture-mode", choices=["target", "stream"], default="target", help="target: capture at configured STM32 target positions; stream: continuously capture frames with actual x")
    parser.add_argument("--duration-seconds", type=float, default=0.0, help="For --capture-mode stream, stop after this many seconds once EEPROM/session starts; 0 means use --expected-frames or Ctrl-C")
    parser.add_argument("--start-after-mm", type=int, default=0, help="For --capture-mode stream, start MLX capture only after x reaches this distance")
    parser.add_argument("--select-targets-mm", default="25,80,135,190,245,300,355,410,465,520,575", help="Comma-separated target x positions for nearest-frame summary in stream mode")
    parser.add_argument("--expected-frames", type=int, default=0, help="Stop after N frames; 0 means Ctrl-C")
    parser.add_argument("--eeprom-file", default=None, help="Use a known-good EEPROM .u16le file instead of waiting for TCAP_EEPROM lines")
    parser.add_argument("--reconnect-delay", type=float, default=1.0, help="Seconds to wait before reopening the STM32 serial port after USB disconnect")
    parser.add_argument("--mlx-refresh-hz", type=int, default=32, choices=[1, 2, 4, 8, 16, 32, 64], help="MLX90640 refresh rate to request from STM32")
    parser.add_argument("--i2c-hz", type=int, default=400000, choices=[100000, 400000, 800000, 1000000], help="STM32 I2C bus speed to request")
    parser.add_argument("--subpage-attempts", type=int, default=8, help="How many subpage read attempts STM32 may make at each target point")
    parser.add_argument("--single-read", action="store_true", help="At each target, read and save exactly one MLX RAM frame; do not wait for both subpages")
    parser.add_argument("--config-resend-interval", type=float, default=0.2, help="Seconds between STM32 config resends until TCAP_SET_OK is seen")
    parser.add_argument("--native-library", default=None)
    parser.add_argument("--echo", action="store_true", help="Also print raw STM32 lines")
    parser.set_defaults(func=cmd_capture)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
