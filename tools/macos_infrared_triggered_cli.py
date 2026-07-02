#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import glob
import json
import math
import os
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_MLX_ADDRESS = 0x33
DEFAULT_I2C_STRETCH = 10000
DEFAULT_EMISSIVITY = 0.95
DEFAULT_BAUD = 921600
DEFAULT_MLX_READ_CHUNK_WORDS = 64
DEFAULT_READ_MODE = "register"
READ_MODES = ("dll-stop", "dll-restart", "register")
DEFAULT_STARTUP_DELAY_SECONDS = 0.15
DEFAULT_TASI_BAUD = 9600
ROBOT_THERMAL_OFFSET_C = 44.0
ROBOT_THERMAL_BIN_NAME = "mlx90640_infrared_thermal.bin"
ROBOT_THERMAL_LATEST_BIN_NAME = "mlx90640_infrared_thermal_latest.bin"
EAST8 = timezone(timedelta(hours=8))

I2C_RATE_1M = 0x0B
I2C_RATE_CODES = {
    "1k": 0x00,
    "5k": 0x01,
    "10k": 0x02,
    "20k": 0x03,
    "50k": 0x04,
    "80k": 0x05,
    "100k": 0x06,
    "200k": 0x07,
    "400k": 0x08,
    "600k": 0x09,
    "800k": 0x0A,
    "1m": 0x0B,
}
EEPROM_START = 0x2400
EEPROM_WORDS = 832
PIXEL_START = 0x0400
PIXEL_WORDS = 768
AUX_START = 0x0700
AUX_WORDS = 64
STATUS_REGISTER = 0x8000
CONTROL_REGISTER = 0x800D
DATA_READY_MASK = 0x0008
INIT_STATUS_VALUE = 0x0030
DEFAULT_REFRESH_RATE_HZ = 8.0
REFRESH_RATE_8HZ = 4
REFRESH_RATE_32HZ = 6
REFRESH_RATE_BITS_BY_HZ = {
    0.5: 0,
    1.0: 1,
    2.0: 2,
    4.0: 3,
    8.0: 4,
    16.0: 5,
    32.0: 6,
    64.0: 7,
}
REFRESH_RATE_HZ_BY_BITS = {bits: hz for hz, bits in REFRESH_RATE_BITS_BY_HZ.items()}
REFRESH_MASK = 0x0380
RESOLUTION_18BIT = 2
RESOLUTION_MASK = 0x0C00
CHESS_MODE_MASK = 0x1000
FRAME_DATA_WORDS = 834
TASI_HOST_HEADER = 0x55AA
TASI_DEVICE_HEADER = 0xAA55
TASI_HOST_HEADER_BYTES = bytes([0xAA, 0x55])
TASI_DEVICE_HEADER_BYTES = bytes([0x55, 0xAA])
TASI_START_REALTIME = bytes([0xAA, 0x55, 0x01, 0x03, 0x03])
TASI_STOP = bytes([0xAA, 0x55, 0x00, 0x03, 0x02])
TA612_MODEL = 612
DEFAULT_CH9326_VID = 0x1A86
DEFAULT_CH9326_PID = 0xE010
DEFAULT_CH9326_TRIGGER_IO = 1
DEFAULT_CH9326_GPIO_REPORT_ID = 0
DEFAULT_CH9326_GPIO_REPORT_LENGTH = 32
DEFAULT_CH9326_GPIO_VALUE_BYTE_INDEX = 1


class CliError(RuntimeError):
    pass


def import_serial():
    try:
        import serial  # type: ignore
        import serial.tools.list_ports  # type: ignore
    except ImportError as exc:
        raise CliError(
            "pyserial is required. Install it with: python3 -m pip install -r requirements-macos.txt"
        ) from exc
    return serial


def serial_open_cli_error(port: str, exc: BaseException) -> CliError:
    hints = ["Run list-ports and pass one of the printed /dev/cu.* device names."]
    if port.endswith(("LEFT", "RIGHT", "XXXXX", "XXXXXX")):
        hints.insert(0, f"{port} looks like an example placeholder, not a real macOS serial port.")
    candidates = sorted(set(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*")))
    if candidates:
        hints.append("Detected likely ports: " + ", ".join(candidates))
    else:
        hints.append("No /dev/cu.usbmodem* or /dev/cu.usbserial* ports are currently visible.")
    return CliError(f"Could not open serial port {port}: {exc}. {' '.join(hints)}")


class SttySerial:
    """Small macOS serial fallback used when pyserial is not installed."""

    EIGHTBITS = None
    PARITY_NONE = None
    STOPBITS_ONE = None

    def __init__(
        self,
        port: str,
        baudrate: int,
        bytesize=None,
        parity=None,
        stopbits=None,
        timeout: float = 2.0,
        write_timeout: float = 2.0,
    ) -> None:
        del bytesize, parity, stopbits, write_timeout
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._configure()

    def _configure(self) -> None:
        command = [
            "stty",
            "-f",
            self.port,
            str(self.baudrate),
            "cs8",
            "-cstopb",
            "-parenb",
            "raw",
            "-echo",
            "-ixon",
            "-ixoff",
            "-crtscts",
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            self.close()
            details = (exc.stderr or exc.stdout or "").strip()
            raise CliError(f"Failed to configure {self.port} with stty at {self.baudrate}: {details}") from exc

    def reset_input_buffer(self) -> None:
        termios.tcflush(self._fd, termios.TCIFLUSH)
        while True:
            readable, _, _ = select.select([self._fd], [], [], 0)
            if not readable:
                break
            try:
                if not os.read(self._fd, 4096):
                    break
            except BlockingIOError:
                break

    def reset_output_buffer(self) -> None:
        termios.tcflush(self._fd, termios.TCOFLUSH)

    def write(self, data: bytes) -> int:
        written = 0
        view = memoryview(data)
        deadline = time.monotonic() + self.timeout
        while written < len(data):
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out writing to {self.port}")
            _, writable, _ = select.select([], [self._fd], [], 0.05)
            if not writable:
                continue
            written += os.write(self._fd, view[written:])
        return written

    def flush(self) -> None:
        termios.tcdrain(self._fd)

    def read(self, byte_count: int) -> bytes:
        readable, _, _ = select.select([self._fd], [], [], self.timeout)
        if not readable:
            return b""
        try:
            return os.read(self._fd, byte_count)
        except BlockingIOError:
            return b""

    def close(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            os.close(fd)
            self._fd = None


def east8_now() -> datetime:
    return datetime.now(EAST8)


def to_east8(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=EAST8)
    return timestamp.astimezone(EAST8)


def east8_iso(timestamp: datetime) -> str:
    return to_east8(timestamp).isoformat()


def utc_now() -> datetime:
    return east8_now()


def hex_bytes(data: bytes | bytearray | Sequence[int], limit: int | None = None) -> str:
    raw = bytes(data)
    if limit is not None and len(raw) > limit:
        return raw[:limit].hex(" ") + f" ... ({len(raw)} bytes)"
    return raw.hex(" ")


def temperature_to_robot_thermal_byte(value: float, offset_c: float = ROBOT_THERMAL_OFFSET_C) -> int:
    if not math.isfinite(value):
        return 0
    raw = math.floor(value + offset_c + 0.5)
    return max(0, min(255, int(raw)))


def temperatures_to_robot_thermal_bytes(
    temperature: Sequence[float],
    offset_c: float = ROBOT_THERMAL_OFFSET_C,
) -> bytes:
    if len(temperature) != PIXEL_WORDS:
        raise CliError(f"Robot thermal bin requires {PIXEL_WORDS} pixels, got {len(temperature)}")
    return bytes(temperature_to_robot_thermal_byte(value, offset_c) for value in temperature)


def safe_channel_name(channel: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", channel.strip().lower())
    cleaned = cleaned.strip("_-")
    if not cleaned:
        raise CliError("MLX channel name cannot be empty")
    return cleaned


def prefix_dict_keys(prefix: str, values: dict[str, str]) -> dict[str, str]:
    return {f"{prefix}{key[:1].upper()}{key[1:]}": value for key, value in values.items()}


def u16be(value: int) -> tuple[int, int]:
    return ((value >> 8) & 0xFF, value & 0xFF)


def make_command(command_type: int, payload: Iterable[int]) -> bytes:
    payload_bytes = bytes(payload)
    length = 3 + len(payload_bytes)
    return bytes([command_type, (length >> 8) & 0xFF, length & 0xFF]) + payload_bytes


def build_config_i2c_command(rate: int = I2C_RATE_1M, stretch: int = DEFAULT_I2C_STRETCH) -> bytes:
    return make_command(
        0x03,
        [
            rate & 0xFF,
            (stretch >> 24) & 0xFF,
            (stretch >> 16) & 0xFF,
            (stretch >> 8) & 0xFF,
            stretch & 0xFF,
        ],
    )


def build_i2c_register_read_command(i2c_address: int, register: int, byte_count: int) -> bytes:
    reg_hi, reg_lo = u16be(register)
    return make_command(
        0x0B,
        [
            0x00,
            0x00,
            i2c_address & 0xFF,
            0x02,
            (byte_count >> 8) & 0xFF,
            byte_count & 0xFF,
            reg_hi,
            reg_lo,
        ],
    )


def build_i2c_send_receive_command(
    start_bit: int,
    stop_bit: int,
    send_payload: bytes,
    receive_len: int,
) -> bytes:
    return make_command(
        0x09,
        [
            start_bit & 0x01,
            stop_bit & 0x01,
            (len(send_payload) >> 8) & 0xFF,
            len(send_payload) & 0xFF,
            (receive_len >> 8) & 0xFF,
            receive_len & 0xFF,
        ]
        + list(send_payload),
    )


def build_i2c_register_read_sequence(
    i2c_address: int,
    register: int,
    byte_count: int,
    repeated_start: bool = False,
) -> tuple[bytes, bytes, bytes]:
    reg_hi, reg_lo = u16be(register)
    write_address = (i2c_address << 1) & 0xFE
    read_address = write_address | 0x01
    register_select_stop = 0 if repeated_start else 1
    return (
        build_i2c_send_receive_command(1, register_select_stop, bytes([write_address, reg_hi, reg_lo]), 0),
        build_i2c_send_receive_command(1, 0, bytes([read_address]), 0),
        build_i2c_send_receive_command(0, 1, b"", byte_count),
    )


def build_i2c_register_write_via_dll_command(i2c_address: int, register: int, payload: bytes) -> bytes:
    reg_hi, reg_lo = u16be(register)
    write_address = (i2c_address << 1) & 0xFE
    return build_i2c_send_receive_command(1, 1, bytes([write_address, reg_hi, reg_lo]) + payload, 0)


def build_i2c_register_write_command(i2c_address: int, register: int, payload: bytes) -> bytes:
    reg_hi, reg_lo = u16be(register)
    return make_command(
        0x0A,
        [
            0x00,
            0x00,
            i2c_address & 0xFF,
            0x02,
            (len(payload) >> 8) & 0xFF,
            len(payload) & 0xFF,
            reg_hi,
            reg_lo,
        ]
        + list(payload),
    )


def words_from_be(data: bytes) -> list[int]:
    if len(data) % 2 != 0:
        raise CliError(f"Expected an even byte count, got {len(data)}")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def word_to_be(value: int) -> bytes:
    return int(value & 0xFFFF).to_bytes(2, "big")


def refresh_rate(control: int) -> int:
    return (control >> 7) & 0x07


def with_refresh_rate(control: int, rate: int) -> int:
    return (control & ~REFRESH_MASK) | ((rate & 0x07) << 7)


def parse_refresh_rate_hz(value: str) -> float:
    try:
        refresh_hz = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("MLX90640 refresh rate must be numeric") from exc
    normalized = normalize_refresh_rate_hz(refresh_hz)
    if normalized is None:
        allowed = ", ".join(format_refresh_rate_hz(rate) for rate in REFRESH_RATE_BITS_BY_HZ)
        raise argparse.ArgumentTypeError(f"MLX90640 refresh rate must be one of: {allowed} Hz")
    return normalized


def normalize_refresh_rate_hz(refresh_hz: float) -> float | None:
    for allowed in REFRESH_RATE_BITS_BY_HZ:
        if abs(allowed - refresh_hz) < 0.001:
            return allowed
    return None


def refresh_rate_bits_from_hz(refresh_hz: float) -> int:
    normalized = normalize_refresh_rate_hz(refresh_hz)
    if normalized is None:
        allowed = ", ".join(format_refresh_rate_hz(rate) for rate in REFRESH_RATE_BITS_BY_HZ)
        raise CliError(f"MLX90640 refresh rate must be one of: {allowed} Hz")
    return REFRESH_RATE_BITS_BY_HZ[normalized]


def format_refresh_rate_hz(refresh_hz: float) -> str:
    return f"{refresh_hz:g}"


def resolution(control: int) -> int:
    return (control >> 10) & 0x03


def with_resolution(control: int, adc_resolution: int) -> int:
    return (control & ~RESOLUTION_MASK) | ((adc_resolution & 0x03) << 10)


def with_chess_mode(control: int) -> int:
    return control | CHESS_MODE_MASK


def is_data_ready(status: int) -> bool:
    return (status & DATA_READY_MASK) != 0


def subpage_from_status(status: int) -> int:
    return status & 0x01


def parse_i2c_rate(value: str) -> int:
    key = value.strip().lower().replace("hz", "")
    if key in I2C_RATE_CODES:
        return I2C_RATE_CODES[key]
    return int(value, 0)


def i2c_rate_name(rate: int) -> str:
    for name, code in I2C_RATE_CODES.items():
        if code == rate:
            return name.upper()
    return f"code_{rate}"


def ensure_mlx_eeprom_looks_valid(eeprom: Sequence[int]) -> None:
    if len(eeprom) != EEPROM_WORDS:
        raise CliError(f"Expected {EEPROM_WORDS} EEPROM words, got {len(eeprom)}")
    if all(word == 0xFFFF for word in eeprom):
        raise CliError(
            "MLX90640 EEPROM read returned all 0xFFFF. "
            "The USB2UART board is reachable, but the MLX90640 is not responding on I2C. "
            "If VCC/GND/SDA/SCL are confirmed, compare --read-mode dll-restart/dll-stop/register with probe-mlx, "
            "then verify I2C ACKs on SDA/SCL or test a known-good I2C device on the same adapter."
        )
    if all(word == 0x0000 for word in eeprom):
        raise CliError(
            "MLX90640 EEPROM read returned all 0x0000. "
            "Check MLX90640 power, I2C wiring, and address."
        )


@dataclass(frozen=True)
class MlxRawSubpage:
    timestamp_utc: datetime
    status: int
    control: int
    polls: int
    frame_data: list[int]

    @property
    def subpage(self) -> int:
        return self.frame_data[833]


@dataclass
class I2cEventStats:
    read_requests: int = 0
    read_responses: int = 0
    read_failures: int = 0
    response_bytes: int = 0

    def record_read_request(self) -> None:
        self.read_requests += 1

    def record_read_response(self, byte_count: int) -> None:
        self.read_responses += 1
        self.response_bytes += byte_count

    def record_read_failure(self) -> None:
        self.read_failures += 1

    @property
    def pending_reads(self) -> int:
        return max(0, self.read_requests - self.read_responses - self.read_failures)

    @property
    def response_ratio(self) -> float:
        if self.read_requests <= 0:
            return 0.0
        return self.read_responses / self.read_requests

    def as_dict(self) -> dict[str, int | float]:
        return {
            "i2cReadRequests": self.read_requests,
            "i2cReadResponses": self.read_responses,
            "i2cReadFailures": self.read_failures,
            "i2cReadPending": self.pending_reads,
            "i2cResponseBytes": self.response_bytes,
            "i2cReadResponseRatio": self.response_ratio,
        }

    def summary_text(self) -> str:
        return (
            f"i2c_reads={self.read_requests} "
            f"i2c_returns={self.read_responses} "
            f"i2c_failures={self.read_failures} "
            f"i2c_pending={self.pending_reads}"
        )


@dataclass(frozen=True)
class MlxFrameSummary:
    timestamp_utc: datetime
    subpage: int
    to_offset_bytes: int
    pixel_count: int
    ta_c: float
    min_c: float
    max_c: float
    avg_c: float
    center_c: float
    robot_thermal_u8_offset_bytes: int | None = None
    robot_thermal_u8_bytes: int = 0
    channel: str = ""


class MlxFullFrameGate:
    """Emit only after both MLX90640 subpages have refreshed."""

    def __init__(self) -> None:
        self._seen_subpage_mask = 0
        self._last_subpage = -1

    def should_emit(self, subpage: int) -> bool:
        subpage &= 1
        self._seen_subpage_mask |= 1 << subpage
        emit = self._seen_subpage_mask == 0b11 and subpage != self._last_subpage
        self._last_subpage = subpage
        return emit


class MlxSubpageTimingStats:
    def __init__(self, expected_period_s: float) -> None:
        self.expected_period_s = expected_period_s
        self.count = 0
        self.first_s: float | None = None
        self.last_s: float | None = None
        self._previous_s: float | None = None
        self.max_interval_s = 0.0
        self.long_gap_count = 0

    def observe(self, timestamp_s: float) -> None:
        if self.first_s is None:
            self.first_s = timestamp_s
        if self._previous_s is not None:
            interval_s = timestamp_s - self._previous_s
            self.max_interval_s = max(self.max_interval_s, interval_s)
            if interval_s > self.expected_period_s * 1.5:
                self.long_gap_count += 1
        self._previous_s = timestamp_s
        self.last_s = timestamp_s
        self.count += 1

    @property
    def active_seconds(self) -> float:
        if self.first_s is None or self.last_s is None:
            return 0.0
        return max(0.0, self.last_s - self.first_s)

    @property
    def observed_hz(self) -> float:
        if self.count < 2:
            return 0.0
        active = self.active_seconds
        if active <= 0:
            return 0.0
        return (self.count - 1) / active

    def summary_text(self) -> str:
        return (
            f"subpage_active={self.active_seconds:.3f}s "
            f"subpage_hz={self.observed_hz:.3f} "
            f"expected_period={self.expected_period_s * 1000.0:.1f}ms "
            f"max_gap={self.max_interval_s * 1000.0:.1f}ms "
            f"long_gaps={self.long_gap_count}"
        )


class Usb2UartSerialI2c:
    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        timeout: float = 2.0,
        debug_wire: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.debug_wire = debug_wire
        self._serial = None
        self.i2c_stats = I2cEventStats()

    def __enter__(self) -> "Usb2UartSerialI2c":
        try:
            serial = import_serial()
        except CliError:
            try:
                self._serial = SttySerial(
                    self.port,
                    self.baud,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            except Exception as exc:
                raise serial_open_cli_error(self.port, exc) from exc
        else:
            try:
                self._serial = serial.Serial(
                    self.port,
                    self.baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            except Exception as exc:
                raise serial_open_cli_error(self.port, exc) from exc
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def _write(self, command: bytes, response_len: int = 0, response_timeout: float | None = None) -> bytes:
        if self._serial is None:
            raise CliError("Serial port is not open")
        self._serial.reset_input_buffer()
        if self.debug_wire:
            print(f">> {hex_bytes(command)}")
        self._serial.write(command)
        self._serial.flush()
        if response_len <= 0:
            return b""
        response = self._read_exact(response_len, response_timeout or self.timeout)
        if self.debug_wire:
            print(f"<< {hex_bytes(response, limit=64)}")
        return response

    def _read_exact(self, byte_count: int, timeout: float) -> bytes:
        if self._serial is None:
            raise CliError("Serial port is not open")
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        remaining = byte_count
        while remaining > 0:
            if time.monotonic() > deadline:
                have = byte_count - remaining
                raise CliError(f"Timed out reading {byte_count} bytes from {self.port}; got {have}")
            chunk = self._serial.read(remaining)
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
            else:
                time.sleep(0.001)
        return b"".join(chunks)

    def read_uid(self) -> bytes:
        return self._write(bytes([0xF1, 0x00, 0x03]), response_len=12)

    def configure_i2c(self, rate: int = I2C_RATE_1M, stretch: int = DEFAULT_I2C_STRETCH) -> None:
        self._write(build_config_i2c_command(rate, stretch), response_len=0)
        time.sleep(0.02)

    def i2c_send_receive(
        self,
        start_bit: int,
        stop_bit: int,
        send_payload: bytes,
        receive_len: int,
    ) -> bytes:
        command = build_i2c_send_receive_command(start_bit, stop_bit, send_payload, receive_len)
        return self._write(command, response_len=receive_len, response_timeout=max(self.timeout, receive_len / 20000))

    def read_register_bytes(
        self,
        i2c_address: int,
        register: int,
        byte_count: int,
        read_mode: str = DEFAULT_READ_MODE,
    ) -> bytes:
        self.i2c_stats.record_read_request()
        try:
            if read_mode == "register":
                command = build_i2c_register_read_command(i2c_address, register, byte_count)
                response = self._write(command, response_len=byte_count, response_timeout=max(self.timeout, byte_count / 20000))
            elif read_mode in ("dll-stop", "dll-restart"):
                repeated_start = read_mode == "dll-restart"
                sequence = build_i2c_register_read_sequence(i2c_address, register, byte_count, repeated_start)
                for command in sequence[:-1]:
                    self._write(command, response_len=0)
                response = self._write(sequence[-1], response_len=byte_count, response_timeout=max(self.timeout, byte_count / 20000))
            else:
                raise CliError(f"Unsupported MLX read mode: {read_mode}")
        except Exception:
            self.i2c_stats.record_read_failure()
            raise
        self.i2c_stats.record_read_response(len(response))
        return response

    def read_register_words(
        self,
        i2c_address: int,
        register: int,
        word_count: int,
        read_mode: str = DEFAULT_READ_MODE,
    ) -> list[int]:
        return words_from_be(self.read_register_bytes(i2c_address, register, word_count * 2, read_mode))

    def read_word(self, i2c_address: int, register: int, read_mode: str = DEFAULT_READ_MODE) -> int:
        return self.read_register_words(i2c_address, register, 1, read_mode)[0]

    def write_register_bytes(
        self,
        i2c_address: int,
        register: int,
        payload: bytes,
        read_mode: str = DEFAULT_READ_MODE,
    ) -> None:
        if read_mode in ("dll-stop", "dll-restart"):
            self._write(build_i2c_register_write_via_dll_command(i2c_address, register, payload), response_len=0)
        else:
            self._write(build_i2c_register_write_command(i2c_address, register, payload), response_len=0)
        time.sleep(0.002)

    def write_word(self, i2c_address: int, register: int, value: int, read_mode: str = DEFAULT_READ_MODE) -> None:
        self.write_register_bytes(i2c_address, register, word_to_be(value), read_mode)


class Mlx90640Device:
    def __init__(
        self,
        bus: Usb2UartSerialI2c,
        address: int = DEFAULT_MLX_ADDRESS,
        read_chunk_words: int = DEFAULT_MLX_READ_CHUNK_WORDS,
        read_mode: str = DEFAULT_READ_MODE,
    ) -> None:
        self.bus = bus
        self.address = address
        self.read_chunk_words = max(1, min(read_chunk_words, 512))
        if read_mode not in READ_MODES:
            raise CliError(f"Unsupported MLX read mode: {read_mode}")
        self.read_mode = read_mode

    def configure_bus(self, rate: int = I2C_RATE_1M, stretch: int = DEFAULT_I2C_STRETCH) -> None:
        self.bus.configure_i2c(rate, stretch)

    def read_eeprom(self) -> list[int]:
        return self.read_words_chunked(EEPROM_START, EEPROM_WORDS)

    def read_words_chunked(self, start_register: int, word_count: int) -> list[int]:
        words: list[int] = []
        offset = 0
        while offset < word_count:
            count = min(self.read_chunk_words, word_count - offset)
            words.extend(self.bus.read_register_words(self.address, start_register + offset, count, self.read_mode))
            offset += count
        return words

    def read_control(self) -> int:
        return self.bus.read_word(self.address, CONTROL_REGISTER, self.read_mode)

    def set_refresh_rate(self, refresh_rate_hz: float) -> tuple[int, int]:
        bits = refresh_rate_bits_from_hz(refresh_rate_hz)
        before = self.read_control()
        after = with_refresh_rate(before, bits)
        if before != after:
            self.bus.write_word(self.address, CONTROL_REGISTER, after, self.read_mode)
        verify = self.read_control()
        if refresh_rate(verify) != bits:
            raise CliError(
                f"Failed to set MLX90640 refresh to {format_refresh_rate_hz(refresh_rate_hz)} Hz; "
                f"control=0x{verify:04X}"
            )
        return before, verify

    def set_resolution_18bit(self) -> tuple[int, int]:
        before = self.read_control()
        after = with_resolution(before, RESOLUTION_18BIT)
        if before != after:
            self.bus.write_word(self.address, CONTROL_REGISTER, after, self.read_mode)
        verify = self.read_control()
        if resolution(verify) != RESOLUTION_18BIT:
            raise CliError(f"Failed to set MLX90640 ADC resolution to 18-bit; control=0x{verify:04X}")
        return before, verify

    def set_chess_mode(self) -> tuple[int, int]:
        before = self.read_control()
        after = with_chess_mode(before)
        if before != after:
            self.bus.write_word(self.address, CONTROL_REGISTER, after, self.read_mode)
        verify = self.read_control()
        if (verify & CHESS_MODE_MASK) == 0:
            raise CliError(f"Failed to set MLX90640 chess mode; control=0x{verify:04X}")
        return before, verify

    def configure_operating_mode(self, refresh_rate_hz: float = DEFAULT_REFRESH_RATE_HZ) -> dict[str, tuple[int, int]]:
        return {
            "chess": self.set_chess_mode(),
            "resolution": self.set_resolution_18bit(),
            "refresh": self.set_refresh_rate(refresh_rate_hz),
        }

    def read_status(self) -> int:
        return self.bus.read_word(self.address, STATUS_REGISTER, self.read_mode)

    def clear_status(self) -> None:
        self.bus.write_word(self.address, STATUS_REGISTER, INIT_STATUS_VALUE, self.read_mode)

    def read_subpage(self, poll_interval: float = 0.002, max_polls: int = 2000) -> MlxRawSubpage:
        status = 0
        polls = 0
        for polls in range(1, max_polls + 1):
            status = self.read_status()
            if is_data_ready(status):
                break
            time.sleep(poll_interval)
        if not is_data_ready(status):
            raise CliError(f"Timed out waiting for MLX data-ready bit after {max_polls} polls")

        self.clear_status()
        pixels = self.read_words_chunked(PIXEL_START, PIXEL_WORDS)
        aux = self.read_words_chunked(AUX_START, AUX_WORDS)
        control = self.read_control()
        frame_data = pixels + aux + [control, subpage_from_status(status)]
        if len(frame_data) != FRAME_DATA_WORDS:
            raise CliError(f"Internal frameData length error: {len(frame_data)}")
        return MlxRawSubpage(east8_now(), status, control, polls, frame_data)


class MlxNativeCalculator:
    def __init__(self, library_path: Path | None = None) -> None:
        self.library_path = library_path or default_native_library_path()
        if self.library_path is None:
            raise CliError("libMlx90640Native.dylib not found. Run: ./build-macos-native.sh")
        self._lib = ctypes.CDLL(str(self.library_path))
        self._configure_symbols()
        self._ctx = self._lib.MlxCreateContext()
        if not self._ctx:
            raise CliError("MlxCreateContext returned null")
        self._initialized = False
        self._to = (ctypes.c_float * PIXEL_WORDS)(*([math.nan] * PIXEL_WORDS))

    def _configure_symbols(self) -> None:
        self._lib.MlxCreateContext.argtypes = []
        self._lib.MlxCreateContext.restype = ctypes.c_void_p
        self._lib.MlxDestroyContext.argtypes = [ctypes.c_void_p]
        self._lib.MlxDestroyContext.restype = None
        self._lib.MlxExtractParameters.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
        ]
        self._lib.MlxExtractParameters.restype = ctypes.c_int
        self._lib.MlxGetTa.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
        ]
        self._lib.MlxGetTa.restype = ctypes.c_float
        self._lib.MlxCalculateTo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.POINTER(ctypes.c_float),
        ]
        self._lib.MlxCalculateTo.restype = ctypes.c_int
        self._lib.MlxBadPixelsCorrection.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
        ]
        self._lib.MlxBadPixelsCorrection.restype = None

    def close(self) -> None:
        if getattr(self, "_ctx", None):
            self._lib.MlxDestroyContext(self._ctx)
            self._ctx = None

    def __enter__(self) -> "MlxNativeCalculator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def extract_parameters(self, eeprom_words: Sequence[int]) -> int:
        arr = (ctypes.c_uint16 * len(eeprom_words))(*eeprom_words)
        rc = self._lib.MlxExtractParameters(self._ctx, arr, len(eeprom_words))
        self._initialized = rc == 0
        return rc

    def calculate(self, frame_data: Sequence[int], emissivity: float = DEFAULT_EMISSIVITY) -> tuple[int, float, list[float]]:
        if not self._initialized:
            raise CliError("MLX90640 parameters have not been extracted")
        frame = (ctypes.c_uint16 * len(frame_data))(*frame_data)
        ta = float(self._lib.MlxGetTa(self._ctx, frame, len(frame_data)))
        tr = ctypes.c_float(ta - 8.0)
        subpage = int(
            self._lib.MlxCalculateTo(
                self._ctx,
                frame,
                len(frame_data),
                ctypes.c_float(emissivity),
                tr,
                self._to,
            )
        )
        self._lib.MlxBadPixelsCorrection(self._ctx, frame, len(frame_data), self._to)
        return subpage, ta, [float(self._to[i]) for i in range(PIXEL_WORDS)]


class MlxCaptureWriter:
    def __init__(
        self,
        capture_root: Path,
        metadata: dict,
        eeprom_words: Sequence[int] | None = None,
        session_prefix: str = "mac_mlx",
        session_dir: Path | None = None,
        channel: str | None = None,
        write_session_json: bool = True,
    ) -> None:
        stamp = datetime.now().strftime(f"{session_prefix}_%Y%m%d_%H%M%S")
        self.session_dir = session_dir or (capture_root / stamp)
        self.raw_dir = self.session_dir / "raw"
        self.temp_dir = self.session_dir / "temp"
        self.channel = safe_channel_name(channel) if channel else ""
        self._file_prefix = f"{self.channel}_" if self.channel else ""
        self._subpages_csv_name = f"{self._file_prefix}mlx_subpages.csv" if self.channel else "mlx_subpages.csv"
        self._frames_csv_name = f"{self._file_prefix}mlx_frames.csv" if self.channel else "mlx_frames.csv"
        self._eeprom_u16_name = f"{self._file_prefix}eeprom.u16le" if self.channel else "eeprom.u16le"
        self._eeprom_csv_name = f"{self._file_prefix}eeprom.csv" if self.channel else "eeprom.csv"
        self._frame_data_name = f"{self._file_prefix}frameData.u16le" if self.channel else "frameData.u16le"
        self._frame_layout_name = f"{self._file_prefix}frameData.layout.json" if self.channel else "frameData.layout.json"
        self._i2c_events_name = f"{self._file_prefix}i2c_events.json" if self.channel else "i2c_events.json"
        self._temperature_name = f"{self._file_prefix}to.f32le" if self.channel else "to.f32le"
        self._temperature_layout_name = f"{self._file_prefix}to.layout.json" if self.channel else "to.layout.json"
        if self.channel:
            self._robot_thermal_name = f"{self.channel}_infrared_thermal.bin"
            self._robot_thermal_latest_name = f"{self.channel}_infrared_thermal_latest.bin"
            self._robot_thermal_layout_name = f"{self.channel}_infrared_thermal.layout.json"
        else:
            self._robot_thermal_name = ROBOT_THERMAL_BIN_NAME
            self._robot_thermal_latest_name = ROBOT_THERMAL_LATEST_BIN_NAME
            self._robot_thermal_layout_name = "mlx90640_infrared_thermal.layout.json"
        self.raw_dir.mkdir(parents=True, exist_ok=session_dir is not None)
        self.temp_dir.mkdir(parents=True, exist_ok=session_dir is not None)

        metadata = dict(metadata)
        extra_raw_files = dict(metadata.pop("rawFiles", {}))
        session = {
            "createdEast8": east8_now().isoformat(),
            "rawFiles": {**self.file_entries(), **extra_raw_files},
            **metadata,
        }
        if self.channel:
            session["mlxChannel"] = self.channel
        if write_session_json:
            (self.session_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")

        self._subpages_csv_file = (self.session_dir / self._subpages_csv_name).open("w", newline="", encoding="utf-8")
        self._frames_csv_file = (self.session_dir / self._frames_csv_name).open("w", newline="", encoding="utf-8")
        self._subpages_csv = csv.writer(self._subpages_csv_file)
        self._frames_csv = csv.writer(self._frames_csv_file)
        self._frame_data_bin = (self.raw_dir / self._frame_data_name).open("wb")
        self._to_bin = (self.temp_dir / self._temperature_name).open("wb")
        self._robot_thermal_bin = (self.temp_dir / self._robot_thermal_name).open("wb")
        self._robot_thermal_latest_path = self.temp_dir / self._robot_thermal_latest_name
        self.write_layout_files()
        if eeprom_words is not None:
            self.write_eeprom(eeprom_words)

        self._subpages_csv.writerow(
            [
                "timestamp_east8",
                "subpage",
                "status_register_hex",
                "control_register_hex",
                "polls",
                "frameData_offset_bytes",
                "frameData_words",
            ]
        )
        self._frames_csv.writerow(
            [
                "timestamp_east8",
                "subpage",
                "to_offset_bytes",
                "robot_thermal_u8_offset_bytes",
                "robot_thermal_u8_bytes",
                "pixel_count",
                "ta_c",
                "min_c",
                "max_c",
                "avg_c",
                "center_c",
            ]
        )

    def file_entries(self) -> dict[str, str]:
        return {
            "eepromU16Le": f"raw/{self._eeprom_u16_name}",
            "eepromCsv": f"raw/{self._eeprom_csv_name}",
            "frameDataU16Le": f"raw/{self._frame_data_name}",
            "frameDataLayoutJson": f"raw/{self._frame_layout_name}",
            "i2cEventsJson": f"raw/{self._i2c_events_name}",
            "temperatureF32Le": f"temp/{self._temperature_name}",
            "temperatureLayoutJson": f"temp/{self._temperature_layout_name}",
            "robotThermalU8Bin": f"temp/{self._robot_thermal_name}",
            "robotThermalLatestU8Bin": f"temp/{self._robot_thermal_latest_name}",
            "robotThermalLayoutJson": f"temp/{self._robot_thermal_layout_name}",
            "subpagesCsv": self._subpages_csv_name,
            "framesCsv": self._frames_csv_name,
        }

    def write_eeprom(self, eeprom_words: Sequence[int]) -> None:
        (self.raw_dir / self._eeprom_u16_name).write_bytes(struct.pack("<" + "H" * len(eeprom_words), *eeprom_words))
        with (self.raw_dir / self._eeprom_csv_name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["word_index", "address_hex", "word_hex", "word_dec"])
            for index, word in enumerate(eeprom_words):
                writer.writerow([index, f"0x{EEPROM_START + index:04X}", f"0x{word:04X}", word])

    def write_layout_files(self) -> None:
        frame_layout = {
            "format": "u16le-stream",
            "recordWords": FRAME_DATA_WORDS,
            "recordBytes": FRAME_DATA_WORDS * 2,
            "endianness": "little",
            "reference": {
                "archive": "docs/infrared.rar",
                "type": "infrared::FrameData",
                "definition": "std::array<uint16_t, kFrameWordCount>",
                "wordCount": FRAME_DATA_WORDS,
            },
            "layout": [
                {
                    "name": "pixelData",
                    "startWord": 0,
                    "wordCount": PIXEL_WORDS,
                    "sourceRegisterStartHex": f"0x{PIXEL_START:04X}",
                },
                {
                    "name": "auxData",
                    "startWord": PIXEL_WORDS,
                    "wordCount": AUX_WORDS,
                    "sourceRegisterStartHex": f"0x{AUX_START:04X}",
                },
                {
                    "name": "controlRegister1",
                    "wordIndex": 832,
                    "sourceRegisterHex": f"0x{CONTROL_REGISTER:04X}",
                },
                {
                    "name": "subpage",
                    "wordIndex": 833,
                    "source": "statusRegister & 0x0001",
                },
            ],
            "index": {
                "csv": self._subpages_csv_name,
                "offsetColumn": "frameData_offset_bytes",
                "wordsColumn": "frameData_words",
            },
        }
        temperature_layout = {
            "format": "f32le-stream",
            "recordValues": PIXEL_WORDS,
            "recordBytes": PIXEL_WORDS * 4,
            "endianness": "little",
            "reference": {
                "archive": "docs/infrared.rar",
                "type": "infrared::TemperatureArray",
                "definition": "std::array<float, kPixelCount>",
                "valueCount": PIXEL_WORDS,
            },
            "geometry": {"width": 32, "height": 24, "order": "row-major"},
            "index": {
                "csv": self._frames_csv_name,
                "offsetColumn": "to_offset_bytes",
                "valuesColumn": "pixel_count",
            },
        }
        robot_thermal_layout = {
            "format": "u8-stream",
            "recordValues": PIXEL_WORDS,
            "recordBytes": PIXEL_WORDS,
            "geometry": {"width": 32, "height": 24, "order": "row-major"},
            "source": "temp/to.f32le",
            "conversion": {
                "formula": "uint8(clamp(floor(temp_c + offset_c + 0.5), 0, 255))",
                "offsetC": ROBOT_THERMAL_OFFSET_C,
                "inverseEstimate": "temp_c ~= raw_byte - offset_c",
            },
            "compatibility": {
                "robotFilePattern": "*_infrared_thermal.bin",
                "singleFrameBytes": PIXEL_WORDS,
                "latestFramePath": f"temp/{self._robot_thermal_latest_name}",
            },
            "index": {
                "csv": self._frames_csv_name,
                "offsetColumn": "robot_thermal_u8_offset_bytes",
                "bytesColumn": "robot_thermal_u8_bytes",
            },
        }
        if self.channel:
            frame_layout["channel"] = self.channel
            temperature_layout["channel"] = self.channel
            robot_thermal_layout["channel"] = self.channel
        (self.raw_dir / self._frame_layout_name).write_text(json.dumps(frame_layout, indent=2), encoding="utf-8")
        (self.temp_dir / self._temperature_layout_name).write_text(json.dumps(temperature_layout, indent=2), encoding="utf-8")
        (self.temp_dir / self._robot_thermal_layout_name).write_text(
            json.dumps(robot_thermal_layout, indent=2),
            encoding="utf-8",
        )

    def close(self) -> None:
        for handle in [
            self._subpages_csv_file,
            self._frames_csv_file,
            self._frame_data_bin,
            self._to_bin,
            self._robot_thermal_bin,
        ]:
            handle.close()

    def __enter__(self) -> "MlxCaptureWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def write_subpage(self, raw: MlxRawSubpage) -> int:
        offset = self._frame_data_bin.tell()
        self._frame_data_bin.write(struct.pack("<" + "H" * len(raw.frame_data), *raw.frame_data))
        self._frame_data_bin.flush()
        self._subpages_csv.writerow(
            [
                east8_iso(raw.timestamp_utc),
                raw.subpage,
                f"0x{raw.status:04X}",
                f"0x{raw.control:04X}",
                raw.polls,
                offset,
                len(raw.frame_data),
            ]
        )
        self._subpages_csv_file.flush()
        return offset

    def write_i2c_stats(self, stats: I2cEventStats) -> None:
        payload = {
            "createdEast8": east8_now().isoformat(),
            "channel": self.channel or None,
            **stats.as_dict(),
        }
        (self.raw_dir / self._i2c_events_name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def write_frame(self, timestamp_utc: datetime, subpage: int, ta: float, temperature: Sequence[float]) -> MlxFrameSummary:
        robot_bytes = temperatures_to_robot_thermal_bytes(temperature)
        offset = self._to_bin.tell()
        self._to_bin.write(struct.pack("<" + "f" * len(temperature), *temperature))
        self._to_bin.flush()
        robot_offset = self._robot_thermal_bin.tell()
        self._robot_thermal_bin.write(robot_bytes)
        self._robot_thermal_bin.flush()
        self._robot_thermal_latest_path.write_bytes(robot_bytes)
        summary = summarize_frame(to_east8(timestamp_utc), subpage, offset, ta, temperature, robot_offset, len(robot_bytes), self.channel)
        self._frames_csv.writerow(
            [
                east8_iso(summary.timestamp_utc),
                summary.subpage,
                summary.to_offset_bytes,
                summary.robot_thermal_u8_offset_bytes,
                summary.robot_thermal_u8_bytes,
                summary.pixel_count,
                f"{summary.ta_c:.6f}",
                f"{summary.min_c:.6f}",
                f"{summary.max_c:.6f}",
                f"{summary.avg_c:.6f}",
                f"{summary.center_c:.6f}",
            ]
        )
        self._frames_csv_file.flush()
        return summary


def summarize_frame(
    timestamp_utc: datetime,
    subpage: int,
    offset: int,
    ta: float,
    temperature: Sequence[float],
    robot_thermal_u8_offset_bytes: int | None = None,
    robot_thermal_u8_bytes: int = 0,
    channel: str = "",
) -> MlxFrameSummary:
    valid = [v for v in temperature if math.isfinite(v)]
    min_c = min(valid) if valid else math.nan
    max_c = max(valid) if valid else math.nan
    avg_c = sum(valid) / len(valid) if valid else math.nan
    center_c = float(temperature[12 * 32 + 16]) if len(temperature) > 12 * 32 + 16 else math.nan
    return MlxFrameSummary(
        timestamp_utc,
        subpage,
        offset,
        len(temperature),
        ta,
        min_c,
        max_c,
        avg_c,
        center_c,
        robot_thermal_u8_offset_bytes,
        robot_thermal_u8_bytes,
        channel,
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_native_library_path() -> Path | None:
    root = repo_root()
    candidates = [
        root / "native" / "Mlx90640Native" / "build" / "macos" / "libMlx90640Native.dylib",
        root / "native" / "Mlx90640Native" / "bin" / "macos" / "libMlx90640Native.dylib",
        root / "libMlx90640Native.dylib",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def list_serial_ports() -> list[dict]:
    try:
        serial = import_serial()
    except CliError:
        fallback_ports = []
        for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*"):
            fallback_ports.extend(glob.glob(pattern))
        return [
            {"device": p, "description": "glob fallback", "vid": None, "pid": None, "serial_number": None}
            for p in sorted(set(fallback_ports))
        ]

    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append(
            {
                "device": port.device,
                "description": port.description,
                "manufacturer": getattr(port, "manufacturer", None),
                "product": getattr(port, "product", None),
                "vid": getattr(port, "vid", None),
                "pid": getattr(port, "pid", None),
                "serial_number": getattr(port, "serial_number", None),
            }
        )
    if not ports:
        fallback_patterns = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*")
        fallback_ports = []
        for pattern in fallback_patterns:
            fallback_ports.extend(glob.glob(pattern))
        ports = [
            {"device": p, "description": "glob fallback", "vid": None, "pid": None, "serial_number": None}
            for p in sorted(set(fallback_ports))
        ]
    return ports


def select_default_port() -> str:
    ports = list_serial_ports()
    if not ports:
        raise CliError("No /dev/cu.usbmodem* serial ports found")
    for port in ports:
        if port.get("vid") == 0x0483 and port.get("pid") == 0x5740:
            return str(port["device"])
    for port in ports:
        device = str(port.get("device") or "")
        if device.startswith("/dev/cu.usbmodem"):
            return device
    return str(ports[0]["device"])


def list_mlx_serial_ports() -> list[str]:
    ports = list_serial_ports()
    preferred = [
        str(port["device"])
        for port in ports
        if port.get("vid") == 0x0483 and port.get("pid") == 0x5740 and port.get("device")
    ]
    fallback = [
        str(port["device"])
        for port in ports
        if str(port.get("device") or "").startswith("/dev/cu.usbmodem") and str(port.get("device")) not in preferred
    ]
    return preferred + fallback


def select_mlx_serial_ports(count: int) -> list[str]:
    ports = list_mlx_serial_ports()
    if len(ports) < count:
        raise CliError(f"Need {count} USB2UART MLX serial ports, found {len(ports)}: {ports}")
    return ports[:count]


def resolve_dual_mlx_ports(left_port: str | None, right_port: str | None) -> tuple[str, str]:
    ports = list_mlx_serial_ports()
    if left_port and right_port:
        if left_port == right_port:
            raise CliError("Left and right MLX ports must be different")
        return left_port, right_port
    if left_port:
        candidates = [port for port in ports if port != left_port]
        if not candidates:
            raise CliError(f"Could not auto-select right MLX port; detected ports: {ports}")
        return left_port, candidates[0]
    if right_port:
        candidates = [port for port in ports if port != right_port]
        if not candidates:
            raise CliError(f"Could not auto-select left MLX port; detected ports: {ports}")
        return candidates[0], right_port
    selected = select_mlx_serial_ports(2)
    return selected[0], selected[1]


def select_default_tasi_port() -> str:
    ports = list_serial_ports()
    if not ports:
        raise CliError("No serial ports found. Connect TA612C or pass --port explicitly.")
    for port in ports:
        if port.get("vid") == 0x1A86 and port.get("pid") == 0x7523:
            return str(port["device"])
    for port in ports:
        device = str(port.get("device") or "")
        if device.startswith("/dev/cu.usbserial"):
            return device
    for port in ports:
        device = str(port.get("device") or "")
        if not device.startswith("/dev/cu.usbmodem"):
            return device
    return str(ports[0]["device"])


def cmd_list_ports(_: argparse.Namespace) -> int:
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


def add_common_mlx_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", default=None, help="Serial port, default: first /dev/cu.usbmodem*")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Serial baud rate, default: {DEFAULT_BAUD}")
    parser.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_MLX_ADDRESS, help="MLX90640 I2C address")
    parser.add_argument("--i2c-rate", type=parse_i2c_rate, default=I2C_RATE_1M, help="I2C rate: 400k, 600k, 800k, 1m, or numeric code")
    parser.add_argument("--stretch", type=int, default=DEFAULT_I2C_STRETCH, help="I2C clock stretch cycles")
    parser.add_argument("--read-chunk-words", type=int, default=DEFAULT_MLX_READ_CHUNK_WORDS, help="MLX register read chunk size in words")
    parser.add_argument("--read-mode", choices=READ_MODES, default=DEFAULT_READ_MODE, help="MLX register read path")
    parser.add_argument(
        "--refresh-rate-hz",
        type=parse_refresh_rate_hz,
        default=DEFAULT_REFRESH_RATE_HZ,
        help="MLX90640 refresh rate Hz: 0.5, 1, 2, 4, 8, 16, 32, or 64; default: 8",
    )
    parser.add_argument("--startup-delay", type=float, default=DEFAULT_STARTUP_DELAY_SECONDS, help="Delay after I2C setup before first MLX access")
    parser.add_argument("--timeout", type=float, default=2.0, help="Serial read/write timeout seconds")
    parser.add_argument("--debug-wire", action="store_true", help="Print raw serial protocol commands and responses")


def cmd_check_mlx(args: argparse.Namespace) -> int:
    port = args.port or select_default_port()
    print(f"Opening {port} @ {args.baud}")
    with Usb2UartSerialI2c(port, args.baud, args.timeout, args.debug_wire) as bus:
        uid = bus.read_uid()
        print(f"USB2UART UID: {uid.hex()}")
        mlx = Mlx90640Device(bus, args.address, args.read_chunk_words, args.read_mode)
        print(
            f"Configuring I2C: {i2c_rate_name(args.i2c_rate)}, stretch={args.stretch}, "
            f"read_mode={args.read_mode}, startup_delay={args.startup_delay}s"
        )
        mlx.configure_bus(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        uid_after = bus.read_uid()
        print(f"UID after I2C config: {uid_after.hex()}")

        print("Reading MLX90640 EEPROM...")
        eeprom = mlx.read_eeprom()
        print(f"EEPROM words: {len(eeprom)}  first8: {' '.join(f'{w:04X}' for w in eeprom[:8])}")
        ensure_mlx_eeprom_looks_valid(eeprom)

        control_before = mlx.read_control()
        config_steps = mlx.configure_operating_mode(args.refresh_rate_hz)
        status = mlx.read_status()
        print(f"Control before: 0x{control_before:04X}")
        for name, (before, verify) in config_steps.items():
            print(f"{name.capitalize():<12}: 0x{before:04X} -> 0x{verify:04X}")
        print(
            f"Control final:  0x{config_steps['refresh'][1]:04X}; "
            f"refresh={format_refresh_rate_hz(args.refresh_rate_hz)}Hz bits={refresh_rate(config_steps['refresh'][1])}; "
            f"resolution={resolution(config_steps['refresh'][1])}; "
            f"chess={(config_steps['refresh'][1] & CHESS_MODE_MASK) != 0}"
        )
        print(f"Status:         0x{status:04X}; data_ready={is_data_ready(status)}; subpage={subpage_from_status(status)}")
    return 0


def cmd_capture_mlx(args: argparse.Namespace) -> int:
    port = args.port or select_default_port()
    capture_root = Path(args.capture_root)
    native_path = Path(args.native_library) if args.native_library else None
    metadata = {
        "kind": "mac_mlx",
        "port": port,
        "baud": args.baud,
        "mlxAddress": args.address,
        "i2cRate": i2c_rate_name(args.i2c_rate),
        "i2cRateCode": args.i2c_rate,
        "i2cClockStretch": args.stretch,
        "mlxReadChunkWords": args.read_chunk_words,
        "mlxReadMode": args.read_mode,
        "mlxStartupDelaySeconds": args.startup_delay,
        "refreshRateHz": args.refresh_rate_hz,
        "mlxRefreshRateUnit": "subpages_per_second",
        "mlxFramePolicy": "strict_full_frame_after_both_subpages",
        "adcResolution": "18-bit",
        "mode": "chess",
        "emissivity": args.emissivity,
        "nativeLibrary": str(native_path or default_native_library_path() or ""),
    }
    with Usb2UartSerialI2c(port, args.baud, args.timeout, args.debug_wire) as bus:
        mlx = Mlx90640Device(bus, args.address, args.read_chunk_words, args.read_mode)
        uid = bus.read_uid()
        mlx.configure_bus(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        eeprom = mlx.read_eeprom()
        ensure_mlx_eeprom_looks_valid(eeprom)
        control_verify = mlx.configure_operating_mode(args.refresh_rate_hz)["refresh"][1]

        with MlxNativeCalculator(native_path) as calc:
            rc = calc.extract_parameters(eeprom)
            if rc != 0:
                raise CliError(f"MlxExtractParameters failed with code {rc}")
            metadata["usb2uartUid"] = uid.hex()
            metadata["controlRegister"] = f"0x{control_verify:04X}"

            with MlxCaptureWriter(capture_root, metadata, eeprom) as writer:
                print(f"Capture directory: {writer.session_dir}")
                start = time.monotonic()
                deadline = start + args.duration if args.duration and args.duration > 0 else None
                frames_written = 0
                subpages = 0
                last_rate_print = start
                full_frame_gate = MlxFullFrameGate()
                timing_stats = MlxSubpageTimingStats(1.0 / args.refresh_rate_hz)

                try:
                    while True:
                        if deadline is not None and time.monotonic() >= deadline:
                            break
                        if args.frames and frames_written >= args.frames:
                            break

                        raw = mlx.read_subpage(args.poll_interval, args.max_polls)
                        timing_stats.observe(time.monotonic())
                        writer.write_subpage(raw)
                        subpages += 1
                        subpage, ta, temperature = calc.calculate(raw.frame_data, args.emissivity)
                        if not full_frame_gate.should_emit(subpage):
                            continue
                        summary = writer.write_frame(raw.timestamp_utc, subpage, ta, temperature)
                        frames_written += 1

                        now = time.monotonic()
                        if args.print_every <= 1 or frames_written % args.print_every == 0 or now - last_rate_print >= 1.0:
                            elapsed = max(now - start, 1e-6)
                            print(
                                f"full_frame={frames_written} subpage={summary.subpage} "
                                f"full_fps={frames_written / elapsed:.2f} subpages={subpages} polls={raw.polls} "
                                f"{bus.i2c_stats.summary_text()} "
                                f"Ta={summary.ta_c:.2f}C min={summary.min_c:.2f}C "
                                f"avg={summary.avg_c:.2f}C max={summary.max_c:.2f}C center={summary.center_c:.2f}C"
                            )
                            last_rate_print = now
                finally:
                    writer.write_i2c_stats(bus.i2c_stats)
                print(
                    f"Stopped. full_frames={frames_written}, subpages={subpages}, "
                    f"{timing_stats.summary_text()}, {bus.i2c_stats.summary_text()}, output={writer.session_dir}"
                )
    return 0


def cmd_scan_i2c(args: argparse.Namespace) -> int:
    port = args.port or select_default_port()
    print(
        f"Scanning I2C on {port} @ {args.baud}, rate={i2c_rate_name(args.i2c_rate)}, "
        f"register=0x{args.register:04X}, bytes={args.read_bytes}, read_mode={args.read_mode}"
    )
    found = 0
    with Usb2UartSerialI2c(port, args.baud, args.timeout, args.debug_wire) as bus:
        uid = bus.read_uid()
        print(f"USB2UART UID: {uid.hex()}")
        bus.configure_i2c(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        for address in range(args.start, args.end + 1):
            try:
                raw = bus.read_register_bytes(address, args.register, args.read_bytes, args.read_mode)
            except CliError as exc:
                if args.verbose:
                    print(f"0x{address:02X}: error: {exc}")
                continue
            all_ff = all(value == 0xFF for value in raw)
            all_00 = all(value == 0x00 for value in raw)
            if args.show_all or not (all_ff or all_00):
                found += 1
                print(f"0x{address:02X}: {hex_bytes(raw)}")
    if found == 0:
        print("No likely I2C responders found.")
    return 0 if found else 1


def cmd_probe_mlx(args: argparse.Namespace) -> int:
    port = args.port or select_default_port()
    modes = args.modes or list(READ_MODES)
    print(
        f"Probing MLX90640 on {port} @ {args.baud}, address=0x{args.address:02X}, "
        f"rate={i2c_rate_name(args.i2c_rate)}, stretch={args.stretch}"
    )
    with Usb2UartSerialI2c(port, args.baud, args.timeout, args.debug_wire) as bus:
        uid = bus.read_uid()
        print(f"USB2UART UID: {uid.hex()}")
        bus.configure_i2c(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        uid_after = bus.read_uid()
        print(f"UID after I2C config: {uid_after.hex()}")

        for mode in modes:
            print(f"\n[{mode}]")
            try:
                status = bus.read_register_bytes(args.address, STATUS_REGISTER, 2, mode)
                control = bus.read_register_bytes(args.address, CONTROL_REGISTER, 2, mode)
                eeprom_head = bus.read_register_words(args.address, EEPROM_START, args.eeprom_words, mode)
            except CliError as exc:
                print(f"error: {exc}")
                continue
            status_word = int.from_bytes(status, "big")
            control_word = int.from_bytes(control, "big")
            eeprom_text = " ".join(f"{word:04X}" for word in eeprom_head)
            all_ff = all(word == 0xFFFF for word in eeprom_head) and status == b"\xff\xff" and control == b"\xff\xff"
            all_00 = all(word == 0x0000 for word in eeprom_head) and status == b"\x00\x00" and control == b"\x00\x00"
            print(f"status=0x{status_word:04X} control=0x{control_word:04X} eeprom[{args.eeprom_words}]={eeprom_text}")
            print(f"flags: all_ff={all_ff} all_00={all_00}")
    return 0


def cmd_clock_scl(args: argparse.Namespace) -> int:
    port = args.port or select_default_port()
    print(
        f"Clocking SCL on {port} @ {args.baud}, address=0x{args.address:02X}, "
        f"rate={i2c_rate_name(args.i2c_rate)}, mode={args.clock_mode}"
    )
    with Usb2UartSerialI2c(port, args.baud, args.timeout, args.debug_wire) as bus:
        bus.configure_i2c(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        start = time.monotonic()
        last = start
        count = 0
        while True:
            if args.duration > 0 and time.monotonic() - start >= args.duration:
                break
            if args.clock_mode == "address-stop":
                bus.i2c_send_receive(1, 1, bytes([(args.address << 1) & 0xFE]), 0)
            else:
                bus.read_register_bytes(args.address, args.register, args.read_bytes, "register")
            count += 1
            now = time.monotonic()
            if now - last >= args.print_every:
                elapsed = max(now - start, 1e-6)
                print(f"transactions={count} rate={count / elapsed:.1f}/s")
                last = now
            if args.interval > 0:
                time.sleep(args.interval)
    print(f"Stopped. transactions={count}")
    return 0


def build_tasi_host_frame(command: int, payload: bytes = b"") -> bytes:
    length = 3 + len(payload)
    frame_without_sum = TASI_HOST_HEADER_BYTES + bytes([command & 0xFF, length & 0xFF]) + payload
    return frame_without_sum + bytes([sum(frame_without_sum) & 0xFF])


def tasi_checksum_ok(raw: bytes) -> bool:
    return len(raw) >= 5 and ((sum(raw[:-1]) & 0xFF) == raw[-1])


def parse_tasi_frame(raw: bytes) -> dict[str, object]:
    if len(raw) < 5:
        raise CliError(f"TA612 frame too short: {hex_bytes(raw)}")
    length = raw[3]
    length_includes_sum = length == len(raw) - 2
    length_excludes_sum = length == len(raw) - 3
    if not (length_includes_sum or length_excludes_sum):
        raise CliError(
            f"TA612 frame length mismatch: field={length}, actual={len(raw) - 2} or {len(raw) - 3}, "
            f"raw={hex_bytes(raw)}"
        )
    command = raw[2]
    data = raw[4:-1]
    parsed: dict[str, object] = {
        "header": raw[0] | (raw[1] << 8),
        "command": command,
        "length": length,
        "length_includes_checksum": length_includes_sum,
        "checksum_ok": tasi_checksum_ok(raw),
        "data_hex": data.hex(),
    }
    if command in (0x01, 0x02) and len(data) >= 8:
        values = struct.unpack_from("<hhhh", data, 0)
        parsed["channels_c"] = [value / 10.0 for value in values]
    if command == 0x00 and len(data) >= 4:
        model, version = struct.unpack_from("<HH", data, 0)
        parsed["model"] = model
        parsed["version"] = version / 100.0
    return parsed


def find_tasi_frame(buffer: bytearray, accept_alt_header: bool = False) -> bytes | None:
    headers = [TASI_DEVICE_HEADER_BYTES]
    if accept_alt_header:
        headers.append(TASI_HOST_HEADER_BYTES)
    while True:
        candidates = [(idx, header) for header in headers if (idx := buffer.find(header)) >= 0]
        if not candidates:
            keep = max(len(TASI_DEVICE_HEADER_BYTES) - 1, 0)
            if len(buffer) > keep:
                del buffer[:-keep]
            return None
        start, _ = min(candidates, key=lambda item: item[0])
        if start > 0:
            del buffer[:start]
        if len(buffer) < 5:
            return None
        length = buffer[3]
        if length < 3 or length > 62:
            del buffer[0]
            continue
        candidate_totals = [2 + length, 3 + length]
        if len(buffer) < candidate_totals[0]:
            return None
        for total in candidate_totals:
            if len(buffer) >= total:
                raw = bytes(buffer[:total])
                if tasi_checksum_ok(raw):
                    del buffer[:total]
                    return raw
        if len(buffer) < candidate_totals[-1]:
            return None
        total = candidate_totals[-1]
        raw = bytes(buffer[:total])
        del buffer[:total]
        return raw


def open_tasi_serial(port: str, baud: int, timeout: float):
    try:
        serial = import_serial()
    except CliError:
        try:
            return SttySerial(port, baud, timeout=timeout, write_timeout=timeout)
        except Exception as exc:
            raise serial_open_cli_error(port, exc) from exc
    try:
        return serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=timeout,
        )
    except Exception as exc:
        raise serial_open_cli_error(port, exc) from exc


def write_serial_frame(serial_port, frame: bytes, debug: bool = False) -> None:
    if debug:
        print(f"TX {hex_bytes(frame)}")
    serial_port.write(frame)
    serial_port.flush()


@dataclass
class TasiSerialSample:
    timestamp_utc: datetime
    raw_offset_bytes: int
    frame_length: int
    command: int
    checksum_ok: bool
    channels_c: list[float] | None
    model: int | None
    version: float | None
    raw: bytes


class TasiSerialCaptureWriter:
    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.raw_dir = session_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._csv_file = (session_dir / "tasi_serial_frames.csv").open("w", newline="", encoding="utf-8")
        self._bin_file = (self.raw_dir / "tasi_serial_frames.bin").open("wb")
        self._csv = csv.writer(self._csv_file)
        self._lock = threading.Lock()
        self._csv.writerow(
            [
                "timestamp_east8",
                "raw_offset_bytes",
                "frame_length",
                "command",
                "checksum_ok",
                "channel1_c",
                "channel2_c",
                "channel3_c",
                "channel4_c",
                "model",
                "version",
                "raw_hex",
            ]
        )

    def close(self) -> None:
        self._csv_file.close()
        self._bin_file.close()

    def __enter__(self) -> "TasiSerialCaptureWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def write_frame(self, timestamp_utc: datetime, raw: bytes, parsed: dict[str, object]) -> TasiSerialSample:
        timestamp_east8 = to_east8(timestamp_utc)
        channels = parsed.get("channels_c")
        channel_values = channels if isinstance(channels, list) else ["", "", "", ""]
        model_value = parsed.get("model")
        version_value = parsed.get("version")
        model = int(model_value) if isinstance(model_value, int) else None
        version = float(version_value) if isinstance(version_value, float) else None
        sample = TasiSerialSample(
            timestamp_utc=timestamp_east8,
            raw_offset_bytes=0,
            frame_length=len(raw),
            command=int(parsed["command"]),
            checksum_ok=bool(parsed["checksum_ok"]),
            channels_c=[float(v) for v in channels] if isinstance(channels, list) else None,
            model=model,
            version=version,
            raw=raw,
        )
        with self._lock:
            offset = self._bin_file.tell()
            self._bin_file.write(struct.pack("<I", len(raw)))
            self._bin_file.write(raw)
            self._bin_file.flush()
            sample.raw_offset_bytes = offset
            self._csv.writerow(
                [
                    east8_iso(timestamp_east8),
                    offset,
                    len(raw),
                    f"0x{sample.command:02X}",
                    sample.checksum_ok,
                    *channel_values,
                    sample.model if sample.model is not None else "",
                    sample.version if sample.version is not None else "",
                    raw.hex(),
                ]
            )
            self._csv_file.flush()
        return sample


class TasiSerialPoller:
    def __init__(
        self,
        port: str,
        baud: int,
        timeout: float,
        poll_interval: float,
        writer: TasiSerialCaptureWriter,
        read_size: int = 64,
        stop_first: bool = False,
        stop_on_exit: bool = True,
        command_delay: float = 0.1,
        accept_alt_header: bool = False,
        debug_wire: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.writer = writer
        self.read_size = read_size
        self.stop_first = stop_first
        self.stop_on_exit = stop_on_exit
        self.command_delay = command_delay
        self.accept_alt_header = accept_alt_header
        self.debug_wire = debug_wire
        self._serial = None
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._latest: TasiSerialSample | None = None
        self._frames = 0
        self._error: BaseException | None = None

    @property
    def frames(self) -> int:
        return self._frames

    def latest(self) -> TasiSerialSample | None:
        with self._lock:
            return self._latest

    def check_error(self) -> None:
        if self._error is not None:
            raise CliError(f"TA612 serial poller failed: {self._error}") from self._error

    def start(self) -> None:
        self._serial = open_tasi_serial(self.port, self.baud, self.timeout)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        if self.stop_first:
            write_serial_frame(self._serial, build_tasi_host_frame(0x00), self.debug_wire)
            time.sleep(self.command_delay)
            self._serial.reset_input_buffer()
        write_serial_frame(self._serial, build_tasi_host_frame(0x01), self.debug_wire)
        self._last_start = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="tasi-serial-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.timeout + 0.5))
        if self._serial is not None:
            if self.stop_on_exit:
                try:
                    write_serial_frame(self._serial, build_tasi_host_frame(0x00), self.debug_wire)
                except Exception:
                    pass
            self._serial.close()
            self._serial = None

    def __enter__(self) -> "TasiSerialPoller":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - self._last_start >= self.poll_interval:
                    write_serial_frame(self._serial, build_tasi_host_frame(0x01), self.debug_wire)
                    self._last_start = now
                chunk = self._serial.read(self.read_size)
                if chunk:
                    if self.debug_wire:
                        print(f"TA612 RX chunk {hex_bytes(chunk)}")
                    self._buffer.extend(chunk)
                while True:
                    raw = find_tasi_frame(self._buffer, self.accept_alt_header)
                    if raw is None:
                        break
                    parsed = parse_tasi_frame(raw)
                    sample = self.writer.write_frame(east8_now(), raw, parsed)
                    self._frames += 1
                    with self._lock:
                        self._latest = sample
        except BaseException as exc:
            self._error = exc


class JoinedSummaryWriter:
    def __init__(self, session_dir: Path, include_mlx_channel: bool = False) -> None:
        self.include_mlx_channel = include_mlx_channel
        self._lock = threading.Lock()
        self._csv_file = (session_dir / "joined_summary.csv").open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        header = [
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
        ]
        if self.include_mlx_channel:
            header.insert(0, "mlx_channel")
        self._csv.writerow(header)

    def close(self) -> None:
        self._csv_file.close()

    def __enter__(self) -> "JoinedSummaryWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def write(self, mlx: MlxFrameSummary, tasi: TasiSerialSample | None) -> None:
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
        row = [
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
        ]
        if self.include_mlx_channel:
            row.insert(0, mlx.channel)
        with self._lock:
            self._csv.writerow(row)
            self._csv_file.flush()


class MlxChannelCaptureWorker:
    def __init__(
        self,
        channel: str,
        mlx: Mlx90640Device,
        calc: MlxNativeCalculator,
        writer: MlxCaptureWriter,
        tasi_poller: TasiSerialPoller,
        joined_writer: JoinedSummaryWriter,
        stop_event: threading.Event,
        poll_interval: float,
        max_polls: int,
        emissivity: float,
        frame_limit: int = 0,
        print_every: int = 32,
        print_lock: threading.Lock | None = None,
        refresh_rate_hz: float = DEFAULT_REFRESH_RATE_HZ,
    ) -> None:
        self.channel = safe_channel_name(channel)
        self.mlx = mlx
        self.calc = calc
        self.writer = writer
        self.tasi_poller = tasi_poller
        self.joined_writer = joined_writer
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.emissivity = emissivity
        self.frame_limit = frame_limit
        self.print_every = max(1, print_every)
        self.print_lock = print_lock or threading.Lock()
        self.full_frame_gate = MlxFullFrameGate()
        self.timing_stats = MlxSubpageTimingStats(1.0 / refresh_rate_hz)
        self.frames = 0
        self.subpages = 0
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"mlx-{self.channel}-capture", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def check_error(self) -> None:
        if self._error is not None:
            raise CliError(f"MLX channel {self.channel} failed: {self._error}") from self._error

    @property
    def stopped(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    def _run(self) -> None:
        start = time.monotonic()
        last_rate_print = start
        try:
            while not self.stop_event.is_set():
                if self.frame_limit and self.frames >= self.frame_limit:
                    break
                self.tasi_poller.check_error()

                raw = self.mlx.read_subpage(self.poll_interval, self.max_polls)
                self.timing_stats.observe(time.monotonic())
                self.writer.write_subpage(raw)
                self.subpages += 1
                subpage, ta, temperature = self.calc.calculate(raw.frame_data, self.emissivity)
                if not self.full_frame_gate.should_emit(subpage):
                    continue
                summary = self.writer.write_frame(raw.timestamp_utc, subpage, ta, temperature)
                self.frames += 1
                latest_tasi = self.tasi_poller.latest()
                self.joined_writer.write(summary, latest_tasi)

                now = time.monotonic()
                if self.frames % self.print_every == 0 or now - last_rate_print >= 1.0:
                    elapsed = max(now - start, 1e-6)
                    tasi_text = "TA612=none"
                    if latest_tasi and latest_tasi.channels_c:
                        age_ms = (summary.timestamp_utc - latest_tasi.timestamp_utc).total_seconds() * 1000.0
                        channels = " ".join(f"{value:.1f}C" for value in latest_tasi.channels_c)
                        tasi_text = f"TA612=[{channels}] age={age_ms:.0f}ms"
                    with self.print_lock:
                        print(
                            f"{self.channel} full_frame={self.frames} subpage={summary.subpage} "
                            f"full_fps={self.frames / elapsed:.2f} subpages={self.subpages} polls={raw.polls} "
                            f"{self.mlx.bus.i2c_stats.summary_text()} "
                            f"Ta={summary.ta_c:.2f}C avg={summary.avg_c:.2f}C "
                            f"center={summary.center_c:.2f}C {tasi_text}"
                        )
                    last_rate_print = now
        except BaseException as exc:
            self._error = exc


def cmd_capture_tasi_serial(args: argparse.Namespace) -> int:
    port = args.port or select_default_tasi_port()
    capture_root = Path(args.capture_root)
    session_dir = capture_root / datetime.now().strftime("mac_tasi_serial_%Y%m%d_%H%M%S")
    raw_dir = session_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    csv_path = session_dir / "tasi_serial_frames.csv"
    bin_path = raw_dir / "tasi_serial_frames.bin"
    metadata = {
        "createdEast8": east8_now().isoformat(),
        "kind": "mac_tasi_serial",
        "port": port,
        "baud": args.baud,
        "timeout": args.timeout,
        "protocol": "TA series serial, TA612C 9600 8N1",
        "startCommandHex": build_tasi_host_frame(0x01).hex(),
        "stopCommandHex": build_tasi_host_frame(0x00).hex(),
    }
    (session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    serial_port = open_tasi_serial(port, args.baud, args.timeout)
    count = 0
    buffer = bytearray()
    try:
        serial_port.reset_input_buffer()
        serial_port.reset_output_buffer()
        if args.stop_first:
            write_serial_frame(serial_port, build_tasi_host_frame(0x00), args.debug_wire)
            time.sleep(args.command_delay)
            serial_port.reset_input_buffer()
        last_start = 0.0
        if args.send_start:
            write_serial_frame(serial_port, build_tasi_host_frame(0x01), args.debug_wire)
            last_start = time.monotonic()

        print(f"TA612 serial capture directory: {session_dir}")
        print(f"Opening {port} @ {args.baud}; waiting for TA612 frames...")
        start = time.monotonic()
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file, bin_path.open("wb") as bin_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "timestamp_east8",
                    "raw_offset_bytes",
                    "frame_length",
                    "command",
                    "checksum_ok",
                    "channel1_c",
                    "channel2_c",
                    "channel3_c",
                    "channel4_c",
                    "model",
                    "version",
                    "raw_hex",
                ]
            )
            while True:
                now = time.monotonic()
                if args.duration and args.duration > 0 and now - start >= args.duration:
                    break
                if args.send_start and args.repeat_start and now - last_start >= args.poll_interval:
                    write_serial_frame(serial_port, build_tasi_host_frame(0x01), args.debug_wire)
                    last_start = now
                chunk = serial_port.read(args.read_size)
                if chunk:
                    buffer.extend(chunk)
                    if args.debug_wire:
                        print(f"RX chunk {hex_bytes(chunk)}")
                raw = find_tasi_frame(buffer, args.accept_alt_header)
                if raw is None:
                    continue
                parsed = parse_tasi_frame(raw)
                channels = parsed.get("channels_c")
                channel_values = channels if isinstance(channels, list) else ["", "", "", ""]
                model = parsed.get("model", "")
                version = parsed.get("version", "")
                offset = bin_file.tell()
                bin_file.write(struct.pack("<I", len(raw)))
                bin_file.write(raw)
                bin_file.flush()
                writer.writerow(
                    [
                        east8_now().isoformat(),
                        offset,
                        len(raw),
                        f"0x{int(parsed['command']):02X}",
                        parsed["checksum_ok"],
                        *channel_values,
                        model,
                        version,
                        raw.hex(),
                    ]
                )
                csv_file.flush()
                count += 1
                if isinstance(channels, list):
                    print(
                        f"frame={count} ch1={channels[0]:.1f}C ch2={channels[1]:.1f}C "
                        f"ch3={channels[2]:.1f}C ch4={channels[3]:.1f}C "
                        f"checksum={parsed['checksum_ok']} raw={hex_bytes(raw)}"
                    )
                elif model:
                    print(f"frame={count} model={model} version={version} checksum={parsed['checksum_ok']} raw={hex_bytes(raw)}")
                else:
                    print(
                        f"frame={count} cmd=0x{int(parsed['command']):02X} "
                        f"checksum={parsed['checksum_ok']} raw={hex_bytes(raw)}"
                    )
                if args.reports and count >= args.reports:
                    break
    finally:
        if args.stop_on_exit:
            try:
                write_serial_frame(serial_port, build_tasi_host_frame(0x00), args.debug_wire)
            except Exception:
                pass
        serial_port.close()
    print(f"Stopped. frames={count}, output={session_dir}")
    return 0


def cmd_capture_combined(args: argparse.Namespace) -> int:
    mlx_port = args.mlx_port or select_default_port()
    tasi_port = args.tasi_port or select_default_tasi_port()
    capture_root = Path(args.capture_root)
    native_path = Path(args.native_library) if args.native_library else None
    metadata = {
        "kind": "mac_mlx_tasi",
        "mlxPort": mlx_port,
        "mlxBaud": args.mlx_baud,
        "mlxAddress": args.address,
        "i2cRate": i2c_rate_name(args.i2c_rate),
        "i2cRateCode": args.i2c_rate,
        "i2cClockStretch": args.stretch,
        "mlxReadChunkWords": args.read_chunk_words,
        "mlxReadMode": args.read_mode,
        "mlxStartupDelaySeconds": args.startup_delay,
        "refreshRateHz": args.refresh_rate_hz,
        "mlxRefreshRateUnit": "subpages_per_second",
        "mlxFramePolicy": "strict_full_frame_after_both_subpages",
        "adcResolution": "18-bit",
        "mode": "chess",
        "emissivity": args.emissivity,
        "nativeLibrary": str(native_path or default_native_library_path() or ""),
        "tasiPort": tasi_port,
        "tasiBaud": args.tasi_baud,
        "tasiTimeoutSeconds": args.tasi_timeout,
        "tasiPollIntervalSeconds": args.tasi_poll_interval,
        "joinPolicy": "Each MLX temperature frame is joined with the most recent TA612 serial sample by timestamp.",
        "rawFiles": {
            "tasiSerialFramesBin": "raw/tasi_serial_frames.bin",
            "tasiSerialFramesCsv": "tasi_serial_frames.csv",
            "joinedSummaryCsv": "joined_summary.csv",
        },
    }
    with Usb2UartSerialI2c(mlx_port, args.mlx_baud, args.mlx_timeout, args.debug_mlx_wire) as bus:
        mlx = Mlx90640Device(bus, args.address, args.read_chunk_words, args.read_mode)
        uid = bus.read_uid()
        mlx.configure_bus(args.i2c_rate, args.stretch)
        time.sleep(max(0.0, args.startup_delay))
        eeprom = mlx.read_eeprom()
        ensure_mlx_eeprom_looks_valid(eeprom)
        control_verify = mlx.configure_operating_mode(args.refresh_rate_hz)["refresh"][1]

        with MlxNativeCalculator(native_path) as calc:
            rc = calc.extract_parameters(eeprom)
            if rc != 0:
                raise CliError(f"MlxExtractParameters failed with code {rc}")
            metadata["usb2uartUid"] = uid.hex()
            metadata["controlRegister"] = f"0x{control_verify:04X}"

            with MlxCaptureWriter(capture_root, metadata, eeprom, session_prefix="mac_mlx_tasi") as mlx_writer:
                with TasiSerialCaptureWriter(mlx_writer.session_dir) as tasi_writer:
                    with JoinedSummaryWriter(mlx_writer.session_dir) as joined_writer:
                        with TasiSerialPoller(
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
                        ) as tasi_poller:
                            print(f"Combined capture directory: {mlx_writer.session_dir}")
                            print(f"MLX: {mlx_port} @ {args.mlx_baud}; TA612: {tasi_port} @ {args.tasi_baud}")
                            start = time.monotonic()
                            deadline = start + args.duration if args.duration and args.duration > 0 else None
                            frames_written = 0
                            subpages = 0
                            last_rate_print = start
                            full_frame_gate = MlxFullFrameGate()
                            timing_stats = MlxSubpageTimingStats(1.0 / args.refresh_rate_hz)

                            try:
                                while True:
                                    if deadline is not None and time.monotonic() >= deadline:
                                        break
                                    if args.frames and frames_written >= args.frames:
                                        break
                                    tasi_poller.check_error()

                                    raw = mlx.read_subpage(args.poll_interval, args.max_polls)
                                    timing_stats.observe(time.monotonic())
                                    mlx_writer.write_subpage(raw)
                                    subpages += 1
                                    subpage, ta, temperature = calc.calculate(raw.frame_data, args.emissivity)
                                    if not full_frame_gate.should_emit(subpage):
                                        continue
                                    summary = mlx_writer.write_frame(raw.timestamp_utc, subpage, ta, temperature)
                                    frames_written += 1
                                    latest_tasi = tasi_poller.latest()
                                    joined_writer.write(summary, latest_tasi)

                                    now = time.monotonic()
                                    if args.print_every <= 1 or frames_written % args.print_every == 0 or now - last_rate_print >= 1.0:
                                        elapsed = max(now - start, 1e-6)
                                        tasi_text = "TA612=none"
                                        if latest_tasi and latest_tasi.channels_c:
                                            age_ms = (summary.timestamp_utc - latest_tasi.timestamp_utc).total_seconds() * 1000.0
                                            channels = " ".join(f"{value:.1f}C" for value in latest_tasi.channels_c)
                                            tasi_text = f"TA612=[{channels}] age={age_ms:.0f}ms"
                                        print(
                                            f"full_frame={frames_written} subpage={summary.subpage} "
                                            f"full_fps={frames_written / elapsed:.2f} subpages={subpages} polls={raw.polls} "
                                            f"{bus.i2c_stats.summary_text()} "
                                            f"MLX Ta={summary.ta_c:.2f}C avg={summary.avg_c:.2f}C "
                                            f"center={summary.center_c:.2f}C {tasi_text}"
                                        )
                                        last_rate_print = now
                            finally:
                                mlx_writer.write_i2c_stats(bus.i2c_stats)

                            tasi_poller.check_error()
                            print(
                                f"Stopped. mlx_full_frames={frames_written}, mlx_subpages={subpages}, "
                                f"{timing_stats.summary_text()}, {bus.i2c_stats.summary_text()}, tasi_frames={tasi_poller.frames}, "
                                f"output={mlx_writer.session_dir}"
                            )
    return 0


def cmd_capture_dual_combined(args: argparse.Namespace) -> int:
    left_port, right_port = resolve_dual_mlx_ports(args.left_mlx_port, args.right_mlx_port)
    tasi_port = args.tasi_port or select_default_tasi_port()
    capture_root = Path(args.capture_root)
    session_dir = capture_root / datetime.now().strftime("mac_dual_mlx_tasi_%Y%m%d_%H%M%S")
    native_path = Path(args.native_library) if args.native_library else None
    channel_specs = [
        (safe_channel_name(args.left_channel), left_port),
        (safe_channel_name(args.right_channel), right_port),
    ]
    if channel_specs[0][0] == channel_specs[1][0]:
        raise CliError("Left and right MLX channel names must be different")
    if left_port == right_port:
        raise CliError("Left and right MLX ports must be different")

    stop_event = threading.Event()
    print_lock = threading.Lock()

    with contextlib.ExitStack() as stack:
        prepared = []
        for channel, port in channel_specs:
            bus = stack.enter_context(Usb2UartSerialI2c(port, args.mlx_baud, args.mlx_timeout, args.debug_mlx_wire))
            uid = bus.read_uid()
            mlx = Mlx90640Device(bus, args.address, args.read_chunk_words, args.read_mode)
            mlx.configure_bus(args.i2c_rate, args.stretch)
            time.sleep(max(0.0, args.startup_delay))
            eeprom = mlx.read_eeprom()
            ensure_mlx_eeprom_looks_valid(eeprom)
            control_verify = mlx.configure_operating_mode(args.refresh_rate_hz)["refresh"][1]
            calc = stack.enter_context(MlxNativeCalculator(native_path))
            rc = calc.extract_parameters(eeprom)
            if rc != 0:
                raise CliError(f"{channel}: MlxExtractParameters failed with code {rc}")
            writer = stack.enter_context(
                MlxCaptureWriter(
                    capture_root,
                    {"kind": "mac_dual_mlx_tasi_channel", "channel": channel},
                    eeprom,
                    session_prefix="mac_dual_mlx_tasi",
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
                    "bus": bus,
                    "mlx": mlx,
                    "calc": calc,
                    "writer": writer,
                    "files": writer.file_entries(),
                }
            )

        raw_files = {
            "tasiSerialFramesBin": "raw/tasi_serial_frames.bin",
            "tasiSerialFramesCsv": "tasi_serial_frames.csv",
            "joinedSummaryCsv": "joined_summary.csv",
        }
        for item in prepared:
            raw_files.update(prefix_dict_keys(str(item["channel"]), item["files"]))

        metadata = {
            "createdEast8": east8_now().isoformat(),
            "kind": "mac_dual_mlx_tasi",
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
            "mlxRefreshRateUnit": "subpages_per_second",
            "mlxFramePolicy": "strict_full_frame_after_both_subpages",
            "adcResolution": "18-bit",
            "mode": "chess",
            "emissivity": args.emissivity,
            "nativeLibrary": str(native_path or default_native_library_path() or ""),
            "tasiPort": tasi_port,
            "tasiBaud": args.tasi_baud,
            "tasiTimeoutSeconds": args.tasi_timeout,
            "tasiPollIntervalSeconds": args.tasi_poll_interval,
            "joinPolicy": "Each left/right MLX temperature frame is joined with the most recent TA612 serial sample by timestamp.",
            "mlxChannels": [
                {
                    "channel": str(item["channel"]),
                    "port": str(item["port"]),
                    "usb2uartUid": str(item["uid"]),
                    "controlRegister": f"0x{int(item['control']):04X}",
                    "files": item["files"],
                }
                for item in prepared
            ],
            "rawFiles": raw_files,
        }
        (session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        with TasiSerialCaptureWriter(session_dir) as tasi_writer:
            with JoinedSummaryWriter(session_dir, include_mlx_channel=True) as joined_writer:
                with TasiSerialPoller(
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
                ) as tasi_poller:
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
                            frame_limit=args.frames,
                            print_every=args.print_every,
                            print_lock=print_lock,
                            refresh_rate_hz=args.refresh_rate_hz,
                        )
                        for item in prepared
                    ]
                    print(f"Dual combined capture directory: {session_dir}")
                    print(f"MLX left:  {left_port} @ {args.mlx_baud}")
                    print(f"MLX right: {right_port} @ {args.mlx_baud}")
                    print(f"TA612:     {tasi_port} @ {args.tasi_baud}")
                    start = time.monotonic()
                    deadline = start + args.duration if args.duration and args.duration > 0 else None
                    try:
                        for worker in workers:
                            worker.start()
                        while True:
                            time.sleep(0.05)
                            tasi_poller.check_error()
                            for worker in workers:
                                worker.check_error()
                            if deadline is not None and time.monotonic() >= deadline:
                                break
                            if args.frames and all(worker.frames >= args.frames for worker in workers):
                                break
                            if all(worker.stopped for worker in workers):
                                break
                    finally:
                        stop_event.set()
                        join_timeout = max(2.0, args.mlx_timeout + args.max_polls * args.poll_interval + 1.0)
                        for worker in workers:
                            worker.join(timeout=join_timeout)
                        for item in prepared:
                            item["writer"].write_i2c_stats(item["bus"].i2c_stats)
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
                    print(f"Stopped. {summary_text}, tasi_frames={tasi_poller.frames}, output={session_dir}")
    return 0


def cmd_capture_triggered_dual_combined(args: argparse.Namespace) -> int:
    hid = import_hid()
    device = hid_open_by_args(hid, args.trigger_hid_path, args.trigger_vid, args.trigger_pid)
    condition = ch9326_poll_condition_from_args(args)
    waiter = Ch9326GpioPollTriggerWaiter(
        device,
        args.gpio_report_id,
        args.gpio_report_length,
        condition,
        args.gpio_poll_interval,
        debug=args.debug_trigger_wire,
    )
    trigger_count = 0
    print(
        "Triggered dual capture armed. "
        f"CH9326 IO{condition.io} byte={condition.byte_index} mask=0x{condition.mask:02X} "
        f"active=0x{condition.active_value:02X} edge={condition.edge}; "
        f"capture_seconds={args.capture_seconds}; trigger_source=poll"
    )
    try:
        while True:
            if args.trigger_count and trigger_count >= args.trigger_count:
                break
            print("Waiting for platform trigger...")
            report = waiter.wait_for_trigger()
            trigger_count += 1
            print(f"Trigger {trigger_count} at {east8_now().isoformat()} report={hex_bytes(report, limit=32)}")

            capture_args = argparse.Namespace(**vars(args))
            capture_args.duration = args.capture_seconds
            capture_args.frames = args.frames
            cmd_capture_dual_combined(capture_args)

            if args.trigger_cooldown > 0:
                time.sleep(args.trigger_cooldown)
            if args.rearm_wait_inactive and condition.edge != "any-report":
                if hasattr(waiter, "wait_for_rearm_ready"):
                    ok = waiter.wait_for_rearm_ready(args.rearm_timeout)
                else:
                    ok = waiter.wait_for_inactive(args.rearm_timeout)
                if ok:
                    print("Trigger input returned to re-arm state; re-armed.")
                else:
                    print("Trigger input did not return to re-arm state before timeout; re-arming anyway.")
    finally:
        device.close()
    print(f"Stopped triggered capture loop. triggers={trigger_count}")
    return 0


def import_hid():
    try:
        import hid  # type: ignore
    except ImportError as exc:
        raise CliError(
            "hidapi Python package is required for TA612 raw capture. "
            "Install it with: python3 -m pip install -r requirements-macos.txt"
        ) from exc
    return hid


def hid_open_by_args(hid, path: str | None, vid: int | None, pid: int | None):
    device = hid.device()
    if path:
        for info in hid.enumerate():
            candidate = info.get("path", b"")
            candidate_text = candidate.decode("utf-8", errors="replace") if isinstance(candidate, bytes) else str(candidate)
            if path == candidate_text or path == str(candidate):
                device.open_path(candidate)
                return device
        device.open_path(path.encode("utf-8"))
        return device
    if vid is None or pid is None:
        raise CliError("Specify --trigger-hid-path or both --trigger-vid and --trigger-pid")
    device.open(vid, pid)
    return device


def read_hid_report(device, report_length: int, timeout_ms: int) -> bytes:
    try:
        data = device.read(report_length, timeout_ms)
    except TypeError:
        data = device.read(report_length)
    return bytes(data or b"")


def read_ch9326_gpio_report(device, report_id: int = DEFAULT_CH9326_GPIO_REPORT_ID, report_length: int = DEFAULT_CH9326_GPIO_REPORT_LENGTH) -> bytes:
    return bytes(device.get_input_report(report_id, report_length) or b"")


def ch9326_io_active_from_report(report: bytes, trigger_io: int, value_byte_index: int = DEFAULT_CH9326_GPIO_VALUE_BYTE_INDEX) -> bool:
    if trigger_io < 1 or trigger_io > 4:
        raise CliError("Only CH9326 IO1..IO4 are supported")
    if value_byte_index < 0 or value_byte_index >= len(report):
        raise CliError(f"GPIO value byte index {value_byte_index} is outside report length {len(report)}")
    return bool(report[value_byte_index] & (1 << (trigger_io - 1)))


def ch9326_poll_condition_from_args(args: argparse.Namespace) -> HidTriggerCondition:
    trigger_io = int(args.trigger_io)
    byte_index = args.trigger_byte_index
    if byte_index is None:
        byte_index = args.gpio_value_byte_index
    mask = args.trigger_mask
    if mask is None:
        mask = 1 << (trigger_io - 1)
    active_value = args.trigger_active_value
    if active_value is None:
        active_value = mask
    return HidTriggerCondition(
        io=trigger_io,
        byte_index=byte_index,
        mask=mask,
        active_value=active_value,
        edge=args.trigger_edge,
    )


@dataclass
class HidTriggerCondition:
    io: int
    byte_index: int
    mask: int
    active_value: int
    edge: str

    def is_active(self, report: bytes) -> bool:
        if self.byte_index < 0 or self.byte_index >= len(report):
            return False
        return (report[self.byte_index] & self.mask) == self.active_value


def resolve_hid_trigger_condition(args: argparse.Namespace) -> HidTriggerCondition:
    trigger_io = int(args.trigger_io)
    if trigger_io < 1 or trigger_io > 4:
        raise CliError("Only CH9326 IO1..IO4 are supported by --trigger-io")
    byte_index = args.trigger_byte_index
    mask = args.trigger_mask
    if byte_index is None:
        byte_index = 0
    if mask is None:
        mask = 1 << (trigger_io - 1)
    active_value = args.trigger_active_value
    if active_value is None:
        active_value = mask
    return HidTriggerCondition(
        io=trigger_io,
        byte_index=byte_index,
        mask=mask,
        active_value=active_value,
        edge=args.trigger_edge,
    )


class HidReportTriggerWaiter:
    def __init__(
        self,
        device,
        report_length: int,
        timeout_ms: int,
        condition: HidTriggerCondition,
        debug: bool = False,
    ) -> None:
        self.device = device
        self.report_length = report_length
        self.timeout_ms = timeout_ms
        self.condition = condition
        self.debug = debug
        self._last_active: bool | None = None

    def read_report(self) -> bytes:
        report = read_hid_report(self.device, self.report_length, self.timeout_ms)
        if report and self.debug:
            print(f"CH9326 RX {hex_bytes(report)} active={self.condition.is_active(report)}")
        return report

    def wait_for_trigger(self) -> bytes:
        while True:
            report = self.read_report()
            if not report:
                continue
            active = self.condition.is_active(report)
            previous = self._last_active
            self._last_active = active
            if self.condition.edge == "any-report":
                return report
            if self.condition.edge == "level" and active:
                return report
            if previous is None:
                continue
            if self.condition.edge == "rising" and (not previous) and active:
                return report
            if self.condition.edge == "falling" and previous and (not active):
                return report
            if self.condition.edge == "change" and previous != active:
                return report

    def wait_for_inactive(self, timeout: float = 30.0) -> bool:
        start = time.monotonic()
        while True:
            if timeout > 0 and time.monotonic() - start >= timeout:
                return False
            report = self.read_report()
            if not report:
                continue
            active = self.condition.is_active(report)
            self._last_active = active
            if not active:
                return True


class Ch9326GpioPollTriggerWaiter:
    def __init__(
        self,
        device,
        report_id: int,
        report_length: int,
        condition: HidTriggerCondition,
        interval: float,
        debug: bool = False,
    ) -> None:
        self.device = device
        self.report_id = report_id
        self.report_length = report_length
        self.condition = condition
        self.interval = max(0.0, interval)
        self.debug = debug
        self._last_active: bool | None = None

    def read_report(self) -> bytes:
        report = read_ch9326_gpio_report(self.device, self.report_id, self.report_length)
        active = self.condition.is_active(report)
        if self.debug:
            print(f"CH9326 GPIO {hex_bytes(report)} active={active}")
        return report

    def wait_for_trigger(self) -> bytes:
        while True:
            report = self.read_report()
            active = self.condition.is_active(report)
            previous = self._last_active
            self._last_active = active
            if self.condition.edge == "any-report":
                return report
            if self.condition.edge == "level" and active:
                return report
            if previous is not None:
                if self.condition.edge == "rising" and (not previous) and active:
                    return report
                if self.condition.edge == "falling" and previous and (not active):
                    return report
                if self.condition.edge == "change" and previous != active:
                    return report
            time.sleep(self.interval)

    def wait_for_inactive(self, timeout: float = 30.0) -> bool:
        start = time.monotonic()
        while True:
            if timeout > 0 and time.monotonic() - start >= timeout:
                return False
            report = self.read_report()
            active = self.condition.is_active(report)
            self._last_active = active
            if not active:
                return True
            time.sleep(self.interval)

    def wait_for_rearm_ready(self, timeout: float = 30.0) -> bool:
        start = time.monotonic()
        while True:
            if timeout > 0 and time.monotonic() - start >= timeout:
                return False
            report = self.read_report()
            active = self.condition.is_active(report)
            self._last_active = active
            if self.condition.edge == "falling":
                if active:
                    return True
            elif self.condition.edge == "rising":
                if not active:
                    return True
            elif self.condition.edge in ("change", "any-report"):
                return True
            elif self.condition.edge == "level":
                if not active:
                    return True
            time.sleep(self.interval)


def cmd_list_hid(_: argparse.Namespace) -> int:
    hid = import_hid()
    devices = hid.enumerate()
    if not devices:
        print("No HID devices found.")
        return 1
    for idx, dev in enumerate(devices):
        path = dev.get("path", b"")
        if isinstance(path, bytes):
            path_text = path.decode("utf-8", errors="replace")
        else:
            path_text = str(path)
        print(
            f"[{idx}] {dev.get('vendor_id', 0):04X}:{dev.get('product_id', 0):04X} "
            f"{dev.get('manufacturer_string') or ''} {dev.get('product_string') or ''} "
            f"serial={dev.get('serial_number') or ''} path={path_text}"
        )
    return 0


def cmd_monitor_ch9326(args: argparse.Namespace) -> int:
    hid = import_hid()
    device = hid_open_by_args(hid, args.trigger_hid_path, args.trigger_vid, args.trigger_pid)
    condition = resolve_hid_trigger_condition(args)
    count = 0
    start = time.monotonic()
    print(
        f"Monitoring CH9326/HID reports for IO{condition.io}; byte={condition.byte_index}, "
        f"mask=0x{condition.mask:02X}, active=0x{condition.active_value:02X}, edge={condition.edge}"
    )
    try:
        while True:
            if args.duration > 0 and time.monotonic() - start >= args.duration:
                break
            report = read_hid_report(device, args.trigger_report_length, args.trigger_timeout_ms)
            if not report:
                continue
            count += 1
            active = condition.is_active(report)
            print(f"report={count} active={active} len={len(report)} {hex_bytes(report)}")
            if args.reports and count >= args.reports:
                break
    finally:
        device.close()
    return 0


def cmd_poll_ch9326_io(args: argparse.Namespace) -> int:
    hid = import_hid()
    device = hid_open_by_args(hid, args.trigger_hid_path, args.trigger_vid, args.trigger_pid)
    condition = ch9326_poll_condition_from_args(args)
    last_active: bool | None = None
    last_raw: bytes | None = None
    start = time.monotonic()
    count = 0
    print(
        f"Polling CH9326 IO{condition.io}; report_id={args.gpio_report_id}, "
        f"report_length={args.gpio_report_length}, byte={condition.byte_index}, "
        f"mask=0x{condition.mask:02X}, active=0x{condition.active_value:02X}"
    )
    try:
        while True:
            now = time.monotonic()
            if args.duration > 0 and now - start >= args.duration:
                break
            report = read_ch9326_gpio_report(device, args.gpio_report_id, args.gpio_report_length)
            active = condition.is_active(report)
            changed = active != last_active or report != last_raw
            count += 1
            if args.print_all or changed:
                print(
                    f"sample={count} io{condition.io}={'HIGH' if active else 'LOW'} "
                    f"changed={changed} raw={hex_bytes(report)}"
                )
            last_active = active
            last_raw = report
            if args.samples and count >= args.samples:
                break
            time.sleep(max(0.0, args.interval))
    finally:
        device.close()
    return 0


def open_hid_device(args: argparse.Namespace):
    hid = import_hid()
    device = hid.device()
    if args.path:
        for info in hid.enumerate():
            path = info.get("path", b"")
            path_text = path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)
            if args.path == path_text or args.path == str(path):
                device.open_path(path)
                return device
        device.open_path(args.path.encode("utf-8"))
        return device
    if args.vid is None or args.pid is None:
        raise CliError("Specify --path or both --vid and --pid for TA612 HID capture")
    device.open(args.vid, args.pid)
    return device


def cmd_capture_tasi_raw(args: argparse.Namespace) -> int:
    capture_root = Path(args.capture_root)
    session_dir = capture_root / datetime.now().strftime("mac_tasi_%Y%m%d_%H%M%S")
    raw_dir = session_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    csv_path = session_dir / "tasi_raw_reports.csv"
    bin_path = raw_dir / "tasi_hid_reports.bin"
    metadata = {
        "createdEast8": east8_now().isoformat(),
        "kind": "mac_tasi_raw",
        "path": args.path,
        "vid": args.vid,
        "pid": args.pid,
        "reportLength": args.report_length,
        "timeoutMs": args.timeout_ms,
    }
    (session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    dev = open_hid_device(args)
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file, bin_path.open("wb") as bin_file:
            writer = csv.writer(csv_file)
            writer.writerow(["timestamp_east8", "raw_offset_bytes", "report_length", "raw_hex", "parse_status"])
            print(f"TA612 raw capture directory: {session_dir}")
            start = time.monotonic()
            count = 0
            while True:
                if args.duration and args.duration > 0 and time.monotonic() - start >= args.duration:
                    break
                data = dev.read(args.report_length, args.timeout_ms)
                if not data:
                    continue
                raw = bytes(data)
                offset = bin_file.tell()
                bin_file.write(struct.pack("<I", len(raw)))
                bin_file.write(raw)
                bin_file.flush()
                writer.writerow([east8_now().isoformat(), offset, len(raw), raw.hex(), "raw_hid_unparsed"])
                csv_file.flush()
                count += 1
                print(f"report={count} len={len(raw)} offset={offset} hex={hex_bytes(raw, limit=32)}")
                if args.reports and count >= args.reports:
                    break
            print(f"Stopped. reports={count}, output={session_dir}")
    finally:
        dev.close()
    return 0


@dataclass
class TasiRawReport:
    timestamp: str
    raw: bytes
    source: str
    direction: str = ""
    endpoint: str = ""


def parse_hex_payload(text: str) -> bytes:
    cleaned = re.sub(r"(?i)0x", "", text)
    cleaned = re.sub(r"[^0-9a-fA-F]", "", cleaned)
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    if not cleaned:
        return b""
    return bytes.fromhex(cleaned)


def csv_first_present(row: dict[str, str], names: Sequence[str]) -> str:
    lowered = {key.strip().lower(): value for key, value in row.items() if key is not None}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value.strip()
    return ""


def direction_from_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        return ""
    try:
        endpoint_value = int(value, 0)
    except ValueError:
        return value
    if endpoint_value & 0x80:
        return "in"
    return "out"


def iter_tasi_reports_from_tshark_csv(path: Path, min_len: int, max_len: int) -> Iterable[TasiRawReport]:
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise CliError(f"{path} has no CSV header")
        for row_index, row in enumerate(reader, start=2):
            payload = csv_first_present(
                row,
                (
                    "usbhid.data",
                    "usb.capdata",
                    "usb.data",
                    "hid.data",
                    "raw_hex",
                    "hex",
                    "data",
                ),
            )
            if not payload:
                continue
            raw = parse_hex_payload(payload)
            if len(raw) < min_len or (max_len and len(raw) > max_len):
                continue
            timestamp_epoch = csv_first_present(row, ("frame.time_epoch", "time_epoch", "timestamp_epoch"))
            timestamp = csv_first_present(row, ("timestamp_east8", "timestamp_utc", "timestamp", "time"))
            if timestamp_epoch:
                try:
                    timestamp = datetime.fromtimestamp(float(timestamp_epoch), timezone.utc).astimezone(EAST8).isoformat()
                except ValueError:
                    timestamp = timestamp_epoch
            if not timestamp:
                timestamp = east8_now().isoformat()
            endpoint = csv_first_present(row, ("usb.endpoint_address", "usb.endpoint_number", "endpoint"))
            direction = csv_first_present(row, ("direction", "usb.endpoint_direction"))
            if not direction:
                direction = direction_from_endpoint(endpoint)
            yield TasiRawReport(timestamp, raw, f"{path.name}:{row_index}", direction, endpoint)


HEX_RUN_RE = re.compile(r"(?i)(?:0x)?[0-9a-f]{2}(?:(?:[\s,:;,_-]+|0x)[0-9a-f]{2}){3,}")


def iter_tasi_reports_from_hex_text(path: Path, min_len: int, max_len: int) -> Iterable[TasiRawReport]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            for match in HEX_RUN_RE.finditer(line):
                raw = parse_hex_payload(match.group(0))
                if len(raw) < min_len or (max_len and len(raw) > max_len):
                    continue
                yield TasiRawReport(east8_now().isoformat(), raw, f"{path.name}:{line_number}")


def iter_tasi_reports_from_input(path: Path, input_format: str, min_len: int, max_len: int) -> Iterable[TasiRawReport]:
    if input_format == "tshark-csv":
        yield from iter_tasi_reports_from_tshark_csv(path, min_len, max_len)
        return
    if input_format == "hex-text":
        yield from iter_tasi_reports_from_hex_text(path, min_len, max_len)
        return
    if path.suffix.lower() == ".csv":
        try:
            reports = list(iter_tasi_reports_from_tshark_csv(path, min_len, max_len))
        except (csv.Error, UnicodeDecodeError, CliError):
            reports = []
        if reports:
            yield from reports
            return
    yield from iter_tasi_reports_from_hex_text(path, min_len, max_len)


def summarize_report_lengths(reports: Sequence[TasiRawReport]) -> str:
    counts: dict[int, int] = {}
    for report in reports:
        counts[len(report.raw)] = counts.get(len(report.raw), 0) + 1
    return ", ".join(f"{length}B={count}" for length, count in sorted(counts.items())) or "none"


def cmd_import_tasi_capture(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.exists():
        raise CliError(f"Input capture does not exist: {input_path}")
    reports = list(iter_tasi_reports_from_input(input_path, args.format, args.min_len, args.max_len))
    if not reports:
        raise CliError(
            "No HID payloads were found. Export USBPcap/Wireshark fields such as "
            "frame.time_epoch, usb.endpoint_address, usbhid.data, usb.capdata to CSV, "
            "or use --format hex-text for plain hex dumps."
        )

    capture_root = Path(args.capture_root)
    session_dir = capture_root / datetime.now().strftime("mac_tasi_import_%Y%m%d_%H%M%S")
    raw_dir = session_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    csv_path = session_dir / "tasi_raw_reports.csv"
    bin_path = raw_dir / "tasi_hid_reports.bin"
    metadata = {
        "createdEast8": east8_now().isoformat(),
        "kind": "mac_tasi_imported_raw",
        "input": str(input_path),
        "inputFormat": args.format,
        "minLen": args.min_len,
        "maxLen": args.max_len,
        "reportCount": len(reports),
        "lengthSummary": summarize_report_lengths(reports),
    }
    (session_dir / "session.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file, bin_path.open("wb") as bin_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "timestamp_east8",
                "raw_offset_bytes",
                "report_length",
                "direction",
                "endpoint",
                "source",
                "raw_hex",
                "parse_status",
            ]
        )
        for report in reports:
            offset = bin_file.tell()
            bin_file.write(struct.pack("<I", len(report.raw)))
            bin_file.write(report.raw)
            writer.writerow(
                [
                    report.timestamp,
                    offset,
                    len(report.raw),
                    report.direction,
                    report.endpoint,
                    report.source,
                    report.raw.hex(),
                    "raw_hid_unparsed",
                ]
            )

    print(f"Imported TA612 raw capture directory: {session_dir}")
    print(f"reports={len(reports)} lengths={metadata['lengthSummary']}")
    for index, report in enumerate(reports[: args.preview], start=1):
        print(
            f"[{index}] len={len(report.raw)} dir={report.direction or '-'} "
            f"ep={report.endpoint or '-'} source={report.source} hex={hex_bytes(report.raw, limit=48)}"
        )
    return 0


def iter_tasi_raw_bin(path: Path):
    with path.open("rb") as handle:
        offset = 0
        while True:
            header = handle.read(4)
            if not header:
                break
            if len(header) != 4:
                raise CliError(f"Truncated length header at offset {offset}")
            length = struct.unpack("<I", header)[0]
            raw = handle.read(length)
            if len(raw) != length:
                raise CliError(f"Truncated report at offset {offset}; expected {length}, got {len(raw)}")
            yield offset, raw
            offset = handle.tell()


def cmd_decode_tasi_sample(args: argparse.Namespace) -> int:
    if args.hex:
        raw = bytes.fromhex(args.hex)
        print(f"report_length={len(raw)} raw_hex={raw.hex()} parse_status=raw_hid_unparsed")
        return 0
    if not args.input:
        raise CliError("Specify --input raw/tasi_hid_reports.bin or --hex")
    for idx, (offset, raw) in enumerate(iter_tasi_raw_bin(Path(args.input)), start=1):
        print(f"[{idx}] offset={offset} len={len(raw)} hex={hex_bytes(raw, limit=args.limit)} parse_status=raw_hid_unparsed")
    return 0


def add_ch9326_trigger_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trigger-hid-path", default=None, help="CH9326 HID path from list-hid; overrides VID/PID")
    parser.add_argument("--trigger-vid", type=lambda s: int(s, 0), default=DEFAULT_CH9326_VID, help="CH9326 HID VID")
    parser.add_argument("--trigger-pid", type=lambda s: int(s, 0), default=DEFAULT_CH9326_PID, help="CH9326 HID PID")
    parser.add_argument("--trigger-report-length", type=int, default=64, help="CH9326 input report length")
    parser.add_argument("--trigger-timeout-ms", type=int, default=500, help="CH9326 read timeout milliseconds")
    parser.add_argument("--trigger-io", type=int, choices=(1, 2, 3, 4), default=DEFAULT_CH9326_TRIGGER_IO, help="CH9326 IO input used as the platform trigger; default: IO1")
    parser.add_argument("--trigger-byte-index", type=int, default=None, help="Override report byte index containing the trigger bit")
    parser.add_argument("--trigger-mask", type=lambda s: int(s, 0), default=None, help="Override bit mask for trigger state; default maps IO1..IO4 to bit0..bit3")
    parser.add_argument("--trigger-active-value", type=lambda s: int(s, 0), default=None, help="Override masked value considered active; default equals trigger mask")
    parser.add_argument(
        "--trigger-edge",
        choices=("rising", "falling", "change", "level", "any-report"),
        default="rising",
        help="Trigger condition; use monitor-ch9326 first to choose byte/mask/edge",
    )
    parser.add_argument("--debug-trigger-wire", action="store_true", help="Print raw CH9326 HID trigger reports")


def add_ch9326_gpio_poll_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gpio-report-id", type=int, default=DEFAULT_CH9326_GPIO_REPORT_ID, help="HID input report ID for CH9326 GPIO read")
    parser.add_argument("--gpio-report-length", type=int, default=DEFAULT_CH9326_GPIO_REPORT_LENGTH, help="HID input report length for CH9326 GPIO read")
    parser.add_argument("--gpio-value-byte-index", type=int, default=DEFAULT_CH9326_GPIO_VALUE_BYTE_INDEX, help="Byte index used as the GPIO value; default skips report ID byte")
    parser.add_argument("--gpio-poll-interval", type=float, default=0.05, help="Polling interval seconds for CH9326 GPIO trigger")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="macOS command-line debug tools for MLX90640 and TA612.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_ports = subparsers.add_parser("list-ports", help="List macOS serial ports")
    list_ports.set_defaults(func=cmd_list_ports)

    check_mlx = subparsers.add_parser("check-mlx", help="Check one USB2UARTPSIIIC + MLX90640 path")
    add_common_mlx_args(check_mlx)
    check_mlx.set_defaults(func=cmd_check_mlx)

    scan_i2c = subparsers.add_parser("scan-i2c", help="Probe I2C addresses by reading a register")
    add_common_mlx_args(scan_i2c)
    scan_i2c.add_argument("--start", type=lambda s: int(s, 0), default=0x03, help="First 7-bit address")
    scan_i2c.add_argument("--end", type=lambda s: int(s, 0), default=0x77, help="Last 7-bit address")
    scan_i2c.add_argument("--register", type=lambda s: int(s, 0), default=STATUS_REGISTER, help="Register to read")
    scan_i2c.add_argument("--read-bytes", type=int, default=2, help="Bytes to read at each address")
    scan_i2c.add_argument("--show-all", action="store_true", help="Print 0xFF/0x00 responses too")
    scan_i2c.add_argument("--verbose", action="store_true", help="Print per-address errors")
    scan_i2c.set_defaults(func=cmd_scan_i2c)

    probe_mlx = subparsers.add_parser("probe-mlx", help="Try all MLX register read modes and print raw values")
    add_common_mlx_args(probe_mlx)
    probe_mlx.add_argument("--modes", nargs="+", choices=READ_MODES, default=None, help="Read modes to probe")
    probe_mlx.add_argument("--eeprom-words", type=int, default=8, help="EEPROM words to read from 0x2400")
    probe_mlx.set_defaults(func=cmd_probe_mlx)

    clock_scl = subparsers.add_parser("clock-scl", help="Continuously issue safe I2C transactions for oscilloscope probing")
    add_common_mlx_args(clock_scl)
    clock_scl.add_argument("--clock-mode", choices=("register", "address-stop"), default="register", help="Transaction type to repeat")
    clock_scl.add_argument("--register", type=lambda s: int(s, 0), default=STATUS_REGISTER, help="Register for register mode")
    clock_scl.add_argument("--read-bytes", type=int, default=2, help="Read byte count for register mode")
    clock_scl.add_argument("--interval", type=float, default=0.002, help="Delay between transactions")
    clock_scl.add_argument("--duration", type=float, default=0.0, help="Duration seconds; 0 means until interrupted")
    clock_scl.add_argument("--print-every", type=float, default=2.0, help="Progress print interval seconds")
    clock_scl.set_defaults(func=cmd_clock_scl)

    capture_mlx = subparsers.add_parser("capture-mlx", help="Capture one MLX90640 stream")
    add_common_mlx_args(capture_mlx)
    capture_mlx.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_mlx.add_argument("--duration", type=float, default=0.0, help="Capture duration seconds; 0 means until stopped")
    capture_mlx.add_argument("--frames", type=int, default=0, help="Strict full-frame limit; 0 means no limit")
    capture_mlx.add_argument("--poll-interval", type=float, default=0.002, help="Data-ready poll interval seconds")
    capture_mlx.add_argument("--max-polls", type=int, default=2000, help="Maximum polls per subpage")
    capture_mlx.add_argument("--emissivity", type=float, default=DEFAULT_EMISSIVITY, help="Object emissivity")
    capture_mlx.add_argument("--native-library", default=None, help="Path to libMlx90640Native.dylib")
    capture_mlx.add_argument("--print-every", type=int, default=8, help="Print every N frames")
    capture_mlx.set_defaults(func=cmd_capture_mlx)

    capture_combined = subparsers.add_parser(
        "capture-combined",
        help="Capture MLX90640 and TA612C into one timestamp-joined session",
    )
    capture_combined.add_argument("--mlx-port", default=None, help="MLX USB2UART serial port, default: first /dev/cu.usbmodem*")
    capture_combined.add_argument("--mlx-baud", type=int, default=DEFAULT_BAUD, help=f"MLX USB2UART baud rate, default: {DEFAULT_BAUD}")
    capture_combined.add_argument("--mlx-timeout", type=float, default=2.0, help="MLX serial read/write timeout seconds")
    capture_combined.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_MLX_ADDRESS, help="MLX90640 I2C address")
    capture_combined.add_argument("--i2c-rate", type=parse_i2c_rate, default=I2C_RATE_1M, help="MLX I2C rate: 400k, 600k, 800k, 1m, or numeric code")
    capture_combined.add_argument("--stretch", type=int, default=DEFAULT_I2C_STRETCH, help="MLX I2C clock stretch cycles")
    capture_combined.add_argument("--read-chunk-words", type=int, default=DEFAULT_MLX_READ_CHUNK_WORDS, help="MLX register read chunk size in words")
    capture_combined.add_argument("--read-mode", choices=READ_MODES, default=DEFAULT_READ_MODE, help="MLX register read path")
    capture_combined.add_argument(
        "--refresh-rate-hz",
        type=parse_refresh_rate_hz,
        default=DEFAULT_REFRESH_RATE_HZ,
        help="MLX90640 refresh rate Hz: 0.5, 1, 2, 4, 8, 16, 32, or 64; default: 8",
    )
    capture_combined.add_argument("--startup-delay", type=float, default=DEFAULT_STARTUP_DELAY_SECONDS, help="Delay after MLX I2C setup")
    capture_combined.add_argument("--poll-interval", type=float, default=0.002, help="MLX data-ready poll interval seconds")
    capture_combined.add_argument("--max-polls", type=int, default=2000, help="Maximum MLX polls per subpage")
    capture_combined.add_argument("--emissivity", type=float, default=DEFAULT_EMISSIVITY, help="Object emissivity")
    capture_combined.add_argument("--native-library", default=None, help="Path to libMlx90640Native.dylib")
    capture_combined.add_argument("--tasi-port", default=None, help="TA612C serial port, default: CH340 /dev/cu.usbserial*")
    capture_combined.add_argument("--tasi-baud", type=int, default=DEFAULT_TASI_BAUD, help="TA612C serial baud rate")
    capture_combined.add_argument("--tasi-timeout", type=float, default=0.2, help="TA612C serial timeout seconds")
    capture_combined.add_argument("--tasi-poll-interval", type=float, default=1.0, help="Seconds between TA612 realtime commands")
    capture_combined.add_argument("--tasi-read-size", type=int, default=64, help="TA612C serial read chunk size")
    capture_combined.add_argument("--tasi-stop-first", action="store_true", help="Send TA612 stop command and flush before starting")
    capture_combined.add_argument("--tasi-stop-on-exit", action=argparse.BooleanOptionalAction, default=True, help="Send TA612 stop command before closing")
    capture_combined.add_argument("--tasi-command-delay", type=float, default=0.1, help="Delay after TA612 stop-first command")
    capture_combined.add_argument("--tasi-accept-alt-header", action="store_true", help="Also accept 0x55AA host-order TA612 header while debugging")
    capture_combined.add_argument("--duration", type=float, default=60.0, help="Capture duration seconds; 0 means until stopped")
    capture_combined.add_argument("--frames", type=int, default=0, help="MLX strict full-frame limit; 0 means no limit")
    capture_combined.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_combined.add_argument("--print-every", type=int, default=32, help="Print every N MLX frames")
    capture_combined.add_argument("--debug-mlx-wire", action="store_true", help="Print raw MLX USB2UART protocol bytes")
    capture_combined.add_argument("--debug-tasi-wire", action="store_true", help="Print raw TA612 serial TX/RX bytes")
    capture_combined.set_defaults(func=cmd_capture_combined)

    capture_dual_combined = subparsers.add_parser(
        "capture-dual-combined",
        help="Capture two MLX90640 streams and TA612C into one timestamp-joined session",
    )
    capture_dual_combined.add_argument("--left-mlx-port", default=None, help="Left MLX USB2UART serial port; default: first detected USB2UART")
    capture_dual_combined.add_argument("--right-mlx-port", default=None, help="Right MLX USB2UART serial port; default: second detected USB2UART")
    capture_dual_combined.add_argument("--left-channel", default="left", help="Left MLX channel name used in output files")
    capture_dual_combined.add_argument("--right-channel", default="right", help="Right MLX channel name used in output files")
    capture_dual_combined.add_argument("--mlx-baud", type=int, default=DEFAULT_BAUD, help=f"MLX USB2UART baud rate, default: {DEFAULT_BAUD}")
    capture_dual_combined.add_argument("--mlx-timeout", type=float, default=2.0, help="MLX serial read/write timeout seconds")
    capture_dual_combined.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_MLX_ADDRESS, help="MLX90640 I2C address on each independent bus")
    capture_dual_combined.add_argument("--i2c-rate", type=parse_i2c_rate, default=I2C_RATE_1M, help="MLX I2C rate: 400k, 600k, 800k, 1m, or numeric code")
    capture_dual_combined.add_argument("--stretch", type=int, default=DEFAULT_I2C_STRETCH, help="MLX I2C clock stretch cycles")
    capture_dual_combined.add_argument("--read-chunk-words", type=int, default=DEFAULT_MLX_READ_CHUNK_WORDS, help="MLX register read chunk size in words")
    capture_dual_combined.add_argument("--read-mode", choices=READ_MODES, default=DEFAULT_READ_MODE, help="MLX register read path")
    capture_dual_combined.add_argument(
        "--refresh-rate-hz",
        type=parse_refresh_rate_hz,
        default=DEFAULT_REFRESH_RATE_HZ,
        help="MLX90640 refresh rate Hz: 0.5, 1, 2, 4, 8, 16, 32, or 64; default: 8",
    )
    capture_dual_combined.add_argument("--startup-delay", type=float, default=DEFAULT_STARTUP_DELAY_SECONDS, help="Delay after MLX I2C setup")
    capture_dual_combined.add_argument("--poll-interval", type=float, default=0.002, help="MLX data-ready poll interval seconds")
    capture_dual_combined.add_argument("--max-polls", type=int, default=2000, help="Maximum MLX polls per subpage")
    capture_dual_combined.add_argument("--emissivity", type=float, default=DEFAULT_EMISSIVITY, help="Object emissivity")
    capture_dual_combined.add_argument("--native-library", default=None, help="Path to libMlx90640Native.dylib")
    capture_dual_combined.add_argument("--tasi-port", default=None, help="TA612C serial port, default: CH340 /dev/cu.usbserial*")
    capture_dual_combined.add_argument("--tasi-baud", type=int, default=DEFAULT_TASI_BAUD, help="TA612C serial baud rate")
    capture_dual_combined.add_argument("--tasi-timeout", type=float, default=0.2, help="TA612C serial timeout seconds")
    capture_dual_combined.add_argument("--tasi-poll-interval", type=float, default=1.0, help="Seconds between TA612 realtime commands")
    capture_dual_combined.add_argument("--tasi-read-size", type=int, default=64, help="TA612C serial read chunk size")
    capture_dual_combined.add_argument("--tasi-stop-first", action="store_true", help="Send TA612 stop command and flush before starting")
    capture_dual_combined.add_argument("--tasi-stop-on-exit", action=argparse.BooleanOptionalAction, default=True, help="Send TA612 stop command before closing")
    capture_dual_combined.add_argument("--tasi-command-delay", type=float, default=0.1, help="Delay after TA612 stop-first command")
    capture_dual_combined.add_argument("--tasi-accept-alt-header", action="store_true", help="Also accept 0x55AA host-order TA612 header while debugging")
    capture_dual_combined.add_argument("--duration", type=float, default=60.0, help="Capture duration seconds; 0 means until stopped")
    capture_dual_combined.add_argument("--frames", type=int, default=0, help="Per-channel MLX strict full-frame limit; 0 means no limit")
    capture_dual_combined.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_dual_combined.add_argument("--print-every", type=int, default=32, help="Print every N MLX frames per channel")
    capture_dual_combined.add_argument("--debug-mlx-wire", action="store_true", help="Print raw MLX USB2UART protocol bytes")
    capture_dual_combined.add_argument("--debug-tasi-wire", action="store_true", help="Print raw TA612 serial TX/RX bytes")
    capture_dual_combined.set_defaults(func=cmd_capture_dual_combined)

    monitor_ch9326 = subparsers.add_parser(
        "monitor-ch9326",
        help="Print CH9326/HID input reports so the platform trigger byte/mask can be calibrated",
    )
    add_ch9326_trigger_args(monitor_ch9326)
    monitor_ch9326.add_argument("--duration", type=float, default=0.0, help="Monitor duration seconds; 0 means until stopped")
    monitor_ch9326.add_argument("--reports", type=int, default=0, help="Report limit; 0 means no limit")
    monitor_ch9326.set_defaults(func=cmd_monitor_ch9326)

    poll_ch9326 = subparsers.add_parser(
        "poll-ch9326-io",
        help="Actively poll CH9326 GPIO state with HID get_input_report; use this when reports are not pushed automatically",
    )
    add_ch9326_trigger_args(poll_ch9326)
    add_ch9326_gpio_poll_args(poll_ch9326)
    poll_ch9326.add_argument("--duration", type=float, default=30.0, help="Polling duration seconds; 0 means until stopped")
    poll_ch9326.add_argument("--samples", type=int, default=0, help="Sample limit; 0 means no limit")
    poll_ch9326.add_argument("--interval", type=float, default=0.05, help="Polling interval seconds")
    poll_ch9326.add_argument("--print-all", action="store_true", help="Print every sample instead of only changes")
    poll_ch9326.set_defaults(func=cmd_poll_ch9326_io)

    capture_triggered_dual = subparsers.add_parser(
        "capture-triggered-dual-combined",
        help="Wait for a CH9326/HID trigger, capture two MLX90640 streams and TA612C for N seconds, then re-arm",
    )
    add_ch9326_trigger_args(capture_triggered_dual)
    add_ch9326_gpio_poll_args(capture_triggered_dual)
    capture_triggered_dual.add_argument("--trigger-count", type=int, default=0, help="Number of triggers to capture; 0 means until stopped")
    capture_triggered_dual.add_argument("--capture-seconds", type=float, default=3.0, help="Capture duration after each trigger")
    capture_triggered_dual.add_argument("--trigger-cooldown", type=float, default=0.2, help="Delay after each capture before re-arming")
    capture_triggered_dual.add_argument(
        "--rearm-wait-inactive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before re-arming, wait until the trigger bit is inactive",
    )
    capture_triggered_dual.add_argument("--rearm-timeout", type=float, default=30.0, help="Maximum seconds to wait for inactive trigger state")
    capture_triggered_dual.add_argument("--left-mlx-port", default=None, help="Left MLX USB2UART serial port; default: first detected USB2UART")
    capture_triggered_dual.add_argument("--right-mlx-port", default=None, help="Right MLX USB2UART serial port; default: second detected USB2UART")
    capture_triggered_dual.add_argument("--left-channel", default="left", help="Left MLX channel name used in output files")
    capture_triggered_dual.add_argument("--right-channel", default="right", help="Right MLX channel name used in output files")
    capture_triggered_dual.add_argument("--mlx-baud", type=int, default=DEFAULT_BAUD, help=f"MLX USB2UART baud rate, default: {DEFAULT_BAUD}")
    capture_triggered_dual.add_argument("--mlx-timeout", type=float, default=2.0, help="MLX serial read/write timeout seconds")
    capture_triggered_dual.add_argument("--address", type=lambda s: int(s, 0), default=DEFAULT_MLX_ADDRESS, help="MLX90640 I2C address on each independent bus")
    capture_triggered_dual.add_argument("--i2c-rate", type=parse_i2c_rate, default=I2C_RATE_1M, help="MLX I2C rate: 400k, 600k, 800k, 1m, or numeric code")
    capture_triggered_dual.add_argument("--stretch", type=int, default=DEFAULT_I2C_STRETCH, help="MLX I2C clock stretch cycles")
    capture_triggered_dual.add_argument("--read-chunk-words", type=int, default=DEFAULT_MLX_READ_CHUNK_WORDS, help="MLX register read chunk size in words")
    capture_triggered_dual.add_argument("--read-mode", choices=READ_MODES, default=DEFAULT_READ_MODE, help="MLX register read path")
    capture_triggered_dual.add_argument(
        "--refresh-rate-hz",
        type=parse_refresh_rate_hz,
        default=DEFAULT_REFRESH_RATE_HZ,
        help="MLX90640 refresh rate Hz: 0.5, 1, 2, 4, 8, 16, 32, or 64; default: 8",
    )
    capture_triggered_dual.add_argument("--startup-delay", type=float, default=DEFAULT_STARTUP_DELAY_SECONDS, help="Delay after MLX I2C setup")
    capture_triggered_dual.add_argument("--poll-interval", type=float, default=0.002, help="MLX data-ready poll interval seconds")
    capture_triggered_dual.add_argument("--max-polls", type=int, default=2000, help="Maximum MLX polls per subpage")
    capture_triggered_dual.add_argument("--emissivity", type=float, default=DEFAULT_EMISSIVITY, help="Object emissivity")
    capture_triggered_dual.add_argument("--native-library", default=None, help="Path to libMlx90640Native.dylib")
    capture_triggered_dual.add_argument("--tasi-port", default=None, help="TA612C serial port, default: CH340 /dev/cu.usbserial*")
    capture_triggered_dual.add_argument("--tasi-baud", type=int, default=DEFAULT_TASI_BAUD, help="TA612C serial baud rate")
    capture_triggered_dual.add_argument("--tasi-timeout", type=float, default=0.2, help="TA612C serial timeout seconds")
    capture_triggered_dual.add_argument("--tasi-poll-interval", type=float, default=1.0, help="Seconds between TA612 realtime commands")
    capture_triggered_dual.add_argument("--tasi-read-size", type=int, default=64, help="TA612C serial read chunk size")
    capture_triggered_dual.add_argument("--tasi-stop-first", action="store_true", help="Send TA612 stop command and flush before starting")
    capture_triggered_dual.add_argument("--tasi-stop-on-exit", action=argparse.BooleanOptionalAction, default=True, help="Send TA612 stop command before closing")
    capture_triggered_dual.add_argument("--tasi-command-delay", type=float, default=0.1, help="Delay after TA612 stop-first command")
    capture_triggered_dual.add_argument("--tasi-accept-alt-header", action="store_true", help="Also accept 0x55AA host-order TA612 header while debugging")
    capture_triggered_dual.add_argument("--frames", type=int, default=0, help="Per-channel MLX strict full-frame limit during each capture; 0 means duration-based")
    capture_triggered_dual.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_triggered_dual.add_argument("--print-every", type=int, default=32, help="Print every N MLX frames per channel")
    capture_triggered_dual.add_argument("--debug-mlx-wire", action="store_true", help="Print raw MLX USB2UART protocol bytes")
    capture_triggered_dual.add_argument("--debug-tasi-wire", action="store_true", help="Print raw TA612 serial TX/RX bytes")
    capture_triggered_dual.set_defaults(func=cmd_capture_triggered_dual_combined)

    list_hid = subparsers.add_parser("list-hid", help="List HID devices for later TA612 work")
    list_hid.set_defaults(func=cmd_list_hid)

    capture_tasi = subparsers.add_parser("capture-tasi-raw", help="Capture TA612 raw HID reports without decoding")
    capture_tasi.add_argument("--path", default=None, help="HID path from list-hid")
    capture_tasi.add_argument("--vid", type=lambda s: int(s, 0), default=None, help="HID VID, e.g. 0x1234")
    capture_tasi.add_argument("--pid", type=lambda s: int(s, 0), default=None, help="HID PID, e.g. 0x5678")
    capture_tasi.add_argument("--report-length", type=int, default=64, help="Input report length")
    capture_tasi.add_argument("--timeout-ms", type=int, default=1000, help="Read timeout in milliseconds")
    capture_tasi.add_argument("--duration", type=float, default=0.0, help="Capture duration seconds; 0 means until stopped")
    capture_tasi.add_argument("--reports", type=int, default=0, help="Report limit; 0 means no limit")
    capture_tasi.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_tasi.set_defaults(func=cmd_capture_tasi_raw)

    capture_tasi_serial = subparsers.add_parser(
        "capture-tasi-serial",
        help="Capture and decode TA612C four-channel temperatures over its 9600 8N1 USB serial protocol",
    )
    capture_tasi_serial.add_argument("--port", default=None, help="Serial port, default: CH340 /dev/cu.usbserial*")
    capture_tasi_serial.add_argument("--baud", type=int, default=DEFAULT_TASI_BAUD, help="Serial baud rate")
    capture_tasi_serial.add_argument("--timeout", type=float, default=0.5, help="Serial read/write timeout seconds")
    capture_tasi_serial.add_argument("--duration", type=float, default=10.0, help="Capture duration seconds; 0 means until stopped")
    capture_tasi_serial.add_argument("--reports", type=int, default=0, help="Frame limit; 0 means no limit")
    capture_tasi_serial.add_argument("--read-size", type=int, default=64, help="Serial read chunk size")
    capture_tasi_serial.add_argument("--capture-root", default="captures", help="Output root directory")
    capture_tasi_serial.add_argument("--send-start", action=argparse.BooleanOptionalAction, default=True, help="Send start realtime command")
    capture_tasi_serial.add_argument("--repeat-start", action=argparse.BooleanOptionalAction, default=True, help="Poll by repeating the realtime command")
    capture_tasi_serial.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between repeated realtime commands")
    capture_tasi_serial.add_argument("--stop-first", action="store_true", help="Send stop command and flush before starting")
    capture_tasi_serial.add_argument("--stop-on-exit", action=argparse.BooleanOptionalAction, default=True, help="Send stop command before closing")
    capture_tasi_serial.add_argument("--command-delay", type=float, default=0.1, help="Delay after stop-first command")
    capture_tasi_serial.add_argument("--accept-alt-header", action="store_true", help="Also accept 0x55AA host-order header while debugging")
    capture_tasi_serial.add_argument("--debug-wire", action="store_true", help="Print raw serial TX/RX bytes")
    capture_tasi_serial.set_defaults(func=cmd_capture_tasi_serial)

    import_tasi = subparsers.add_parser(
        "import-tasi-capture",
        help="Import Windows USBPcap/Wireshark CSV or hex text into TA612 raw report files",
    )
    import_tasi.add_argument("--input", required=True, help="Capture export path, usually tshark CSV or hex text")
    import_tasi.add_argument(
        "--format",
        choices=("auto", "tshark-csv", "hex-text"),
        default="auto",
        help="Input format. auto tries CSV first for .csv, then plain hex text.",
    )
    import_tasi.add_argument("--min-len", type=int, default=4, help="Minimum payload length to import")
    import_tasi.add_argument("--max-len", type=int, default=0, help="Maximum payload length; 0 means no limit")
    import_tasi.add_argument("--preview", type=int, default=10, help="Preview first N imported reports")
    import_tasi.add_argument("--capture-root", default="captures", help="Output root directory")
    import_tasi.set_defaults(func=cmd_import_tasi_capture)

    decode_tasi = subparsers.add_parser("decode-tasi-sample", help="Print TA612 raw reports; temperature parser is not yet fixed")
    decode_tasi.add_argument("--input", default=None, help="Path to raw/tasi_hid_reports.bin")
    decode_tasi.add_argument("--hex", default=None, help="Single report hex string")
    decode_tasi.add_argument("--limit", type=int, default=64, help="Maximum bytes to print per report")
    decode_tasi.set_defaults(func=cmd_decode_tasi_sample)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
