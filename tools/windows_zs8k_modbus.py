#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


DEFAULT_BAUD = 38400
DEFAULT_ADDRESS = 1
DEFAULT_CHANNELS = 8
DEFAULT_START_REGISTER = 0x0000


class CliError(RuntimeError):
    pass


def import_serial():
    try:
        import serial  # type: ignore
        import serial.tools.list_ports  # type: ignore
    except ImportError as exc:
        raise CliError("pyserial is required. Install it with: py -m pip install -r requirements-windows.txt") from exc
    return serial


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(frame: bytes) -> bytes:
    crc = modbus_crc16(frame)
    return frame + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_read_input_registers(address: int, start: int, count: int) -> bytes:
    if not 1 <= address <= 255:
        raise CliError("--address must be 1..255")
    if not 1 <= count <= 125:
        raise CliError("--channels/register count must be 1..125")
    if not 0 <= start <= 0xFFFF:
        raise CliError("--start-register must be 0..0xffff")
    payload = bytes(
        (
            address,
            0x04,
            (start >> 8) & 0xFF,
            start & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        )
    )
    return append_crc(payload)


def read_exact(serial_port, byte_count: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    have = 0
    while have < byte_count:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            break
        serial_port.timeout = min(max(remaining_time, 0.01), timeout)
        chunk = serial_port.read(byte_count - have)
        if not chunk:
            continue
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)


def parse_read_input_response(response: bytes, address: int, count: int) -> list[int]:
    expected_len = 5 + 2 * count
    if len(response) != expected_len:
        raise CliError(f"Expected {expected_len} response bytes, got {len(response)}: {response.hex(' ')}")
    body, crc_bytes = response[:-2], response[-2:]
    expected_crc = modbus_crc16(body)
    actual_crc = crc_bytes[0] | (crc_bytes[1] << 8)
    if actual_crc != expected_crc:
        raise CliError(
            f"Bad CRC: expected 0x{expected_crc:04X}, got 0x{actual_crc:04X}; response={response.hex(' ')}"
        )
    if body[0] != address:
        raise CliError(f"Unexpected slave address {body[0]}, expected {address}")
    if body[1] & 0x80:
        code = body[2] if len(body) > 2 else None
        raise CliError(f"Modbus exception from slave {address}: function=0x{body[1]:02X} code={code}")
    if body[1] != 0x04:
        raise CliError(f"Unexpected function 0x{body[1]:02X}, expected 0x04")
    if body[2] != 2 * count:
        raise CliError(f"Unexpected byte count {body[2]}, expected {2 * count}")
    data = body[3:]
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]


def decode_zs8k_temperature(raw: int, signed_register_bank: bool) -> float | None:
    if raw == 0xFFFF:
        return None
    if signed_register_bank:
        if raw & 0x8000:
            raw -= 0x10000
        return raw / 10.0
    sign = -1 if raw & 0x8000 else 1
    magnitude = raw & 0x7FFF
    return sign * magnitude / 10.0


def read_temperatures(
    serial_port,
    address: int,
    start_register: int,
    channels: int,
    timeout: float,
    signed_register_bank: bool,
    debug_wire: bool,
) -> list[float | None]:
    request = build_read_input_registers(address, start_register, channels)
    if debug_wire:
        print("TX", request.hex(" "))
    serial_port.reset_input_buffer()
    serial_port.write(request)
    serial_port.flush()
    response = read_exact(serial_port, 5 + 2 * channels, timeout)
    if debug_wire:
        print("RX", response.hex(" "))
    raw_values = parse_read_input_response(response, address, channels)
    return [decode_zs8k_temperature(value, signed_register_bank) for value in raw_values]


def format_temperatures(values: Sequence[float | None]) -> str:
    parts = []
    for index, value in enumerate(values, start=1):
        if value is None:
            parts.append(f"ch{index}=FAULT")
        else:
            parts.append(f"ch{index}={value:.1f}C")
    return " ".join(parts)


def open_serial(port: str, baud: int, timeout: float):
    serial = import_serial()
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
        raise CliError(f"Could not open {port}: {exc}") from exc


def cmd_list_ports(_args: argparse.Namespace) -> int:
    serial = import_serial()
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 0
    for port in ports:
        print(
            f"{port.device}\t{port.description or ''}\t"
            f"vid={getattr(port, 'vid', None)} pid={getattr(port, 'pid', None)} "
            f"serial={getattr(port, 'serial_number', None) or ''}"
        )
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    csv_file = None
    writer = None
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = csv_path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp_utc", *[f"ch{i}_c" for i in range(1, args.channels + 1)]])

    mcu_serial = None
    if args.forward_mcu_port:
        mcu_serial = open_serial(args.forward_mcu_port, args.forward_mcu_baud, args.timeout)

    serial_port = open_serial(args.port, args.baud, args.timeout)
    try:
        printed_header = False
        while True:
            timestamp = datetime.now(timezone.utc).isoformat()
            values = read_temperatures(
                serial_port=serial_port,
                address=args.address,
                start_register=args.start_register,
                channels=args.channels,
                timeout=args.timeout,
                signed_register_bank=args.signed_register_bank,
                debug_wire=args.debug_wire,
            )
            if not printed_header:
                print(f"Polling ZS-8K on {args.port} at {args.baud} 8N1, address={args.address}")
                printed_header = True
            print(timestamp, format_temperatures(values))

            if writer is not None:
                writer.writerow([timestamp, *["" if value is None else f"{value:.1f}" for value in values]])
                csv_file.flush()

            if mcu_serial is not None:
                payload = {
                    "type": "zs8k_temperature",
                    "timestamp_utc": timestamp,
                    "address": args.address,
                    "temps_c": values,
                }
                mcu_serial.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii"))
                mcu_serial.flush()

            if args.once:
                break
            time.sleep(args.interval)
    finally:
        serial_port.close()
        if mcu_serial is not None:
            mcu_serial.close()
        if csv_file is not None:
            csv_file.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read ZS-8K K-type thermocouple module over Modbus RTU/RS485.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_ports = subparsers.add_parser("list-ports", help="List Windows COM ports")
    list_ports.set_defaults(func=cmd_list_ports)

    poll = subparsers.add_parser("poll", help="Poll ZS-8K temperature registers")
    poll.add_argument("--port", required=True, help="USB-RS485 COM port, for example COM5")
    poll.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Modbus baud rate, default: 38400")
    poll.add_argument("--address", type=int, default=DEFAULT_ADDRESS, help="Modbus slave address, default: 1")
    poll.add_argument("--channels", type=int, default=DEFAULT_CHANNELS, help="Temperature channels/registers to read")
    poll.add_argument(
        "--start-register",
        type=lambda value: int(value, 0),
        default=DEFAULT_START_REGISTER,
        help="Input register start address, default: 0x0000",
    )
    poll.add_argument(
        "--signed-register-bank",
        action="store_true",
        help="Use signed int16 decoding. Use this with start register 0x0010.",
    )
    poll.add_argument("--timeout", type=float, default=0.5, help="Serial read/write timeout seconds")
    poll.add_argument("--interval", type=float, default=0.2, help="Poll interval seconds")
    poll.add_argument("--once", action="store_true", help="Read once and exit")
    poll.add_argument("--csv", default=None, help="Optional CSV output path")
    poll.add_argument("--debug-wire", action="store_true", help="Print raw Modbus TX/RX bytes")
    poll.add_argument(
        "--forward-mcu-port",
        default=None,
        help="Optional second COM port. Sends one JSON line per sample; MCU firmware must implement this protocol.",
    )
    poll.add_argument("--forward-mcu-baud", type=int, default=115200, help="Baud rate for --forward-mcu-port")
    poll.set_defaults(func=cmd_poll)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Stopped.")
        return 130
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
