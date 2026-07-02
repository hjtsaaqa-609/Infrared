#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_HOST = "mqtt.i-pv.cn"
DEFAULT_PORT = 1884
DEFAULT_TOPIC = "testup/+"
DEFAULT_USERNAME = "bf076f019e5a44aabf266dfea52f8e8a"
DEFAULT_PASSWORD = "9da5984a0cb475c71d979f57cb9aa022"
CHANNELS = [f"ch{i}_c" for i in range(1, 9)]
EAST8 = timezone(timedelta(hours=8))


def now_pair() -> tuple[datetime, datetime]:
    utc = datetime.now(timezone.utc)
    return utc.astimezone(EAST8), utc


def device_id_from_topic(topic: str) -> str:
    parts = [part for part in topic.strip("/").split("/") if part]
    return parts[-1] if parts else "unknown"


def zs8k_temps(obj: Any | None) -> list[Any] | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "zs8k" or obj.get("ok") != 1:
        return None
    temps = obj.get("temps_c")
    if not isinstance(temps, list):
        return None
    return temps


class CsvHandle:
    def __init__(self, path: Path, header: list[str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        if self.file.tell() == 0:
            self.writer.writerow(header)
            self.file.flush()

    def writerow(self, row: list[Any]) -> None:
        self.writer.writerow(row)
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def import_mqtt():
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit(
            "paho-mqtt is required. Run:\n"
            "  python3 -m pip install paho-mqtt"
        ) from exc
    return mqtt


def decode_payload(payload: bytes) -> tuple[str, Any | None]:
    text = payload.decode("utf-8", errors="replace").strip()
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


class Sink:
    def __init__(
        self,
        jsonl_path: str | None,
        csv_path: str | None,
        sampled_csv_dir: str | None,
        sample_interval: float,
    ):
        self.jsonl_file = Path(jsonl_path).open("a", encoding="utf-8") if jsonl_path else None
        self.csv_handle = (
            CsvHandle(Path(csv_path), ["received_utc", "topic", *CHANNELS])
            if csv_path
            else None
        )
        self.sampled_csv_dir = Path(sampled_csv_dir) if sampled_csv_dir else None
        self.sample_interval = max(0.0, sample_interval)
        self.sampled_handles: dict[str, CsvHandle] = {}
        self.last_sample_at: dict[str, float] = {}

    def write(self, topic: str, text: str, obj: Any | None) -> None:
        received_east8_dt, received_utc_dt = now_pair()
        received_east8 = received_east8_dt.isoformat()
        received_utc = received_utc_dt.isoformat()
        device_id = device_id_from_topic(topic)

        if self.jsonl_file:
            self.jsonl_file.write(
                json.dumps(
                    {
                        "received_east8": received_east8,
                        "received_utc": received_utc,
                        "device_id": device_id,
                        "topic": topic,
                        "payload": obj if obj is not None else text,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self.jsonl_file.flush()

        temps = zs8k_temps(obj)
        if temps is None:
            return

        values = [temps[i] if i < len(temps) and temps[i] is not None else "" for i in range(8)]
        if self.csv_handle:
            self.csv_handle.writerow([received_utc, topic, *values])

        if self.sampled_csv_dir:
            now_monotonic = time.monotonic()
            last = self.last_sample_at.get(device_id)
            if last is None or now_monotonic - last >= self.sample_interval:
                handle = self.sampled_handles.get(device_id)
                if handle is None:
                    handle = CsvHandle(
                        self.sampled_csv_dir / f"{device_id}.csv",
                        ["received_east8", "received_utc", "device_id", "topic", *CHANNELS],
                    )
                    self.sampled_handles[device_id] = handle
                handle.writerow([received_east8, received_utc, device_id, topic, *values])
                self.last_sample_at[device_id] = now_monotonic

    def close(self) -> None:
        if self.jsonl_file:
            self.jsonl_file.close()
        if self.csv_handle:
            self.csv_handle.close()
        for handle in self.sampled_handles.values():
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Subscribe to ZS-8K MQTT temperature data.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--client-id", default="")
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--tls-insecure", action="store_true")
    parser.add_argument("--jsonl", default="zs8k_mqtt.jsonl")
    parser.add_argument("--csv", default="zs8k_mqtt.csv")
    parser.add_argument("--csv-dir", default="csv", help="Directory for per-DTU sampled CSV files")
    parser.add_argument("--sample-interval", type=float, default=10.0, help="Seconds between per-DTU CSV rows")
    parser.add_argument("--no-sampled-csv", action="store_true", help="Disable csv/<device_id>.csv output")
    args = parser.parse_args()

    mqtt = import_mqtt()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id, protocol=mqtt.MQTTv311)
    except AttributeError:
        client = mqtt.Client(client_id=args.client_id, protocol=mqtt.MQTTv311)

    if args.username:
        client.username_pw_set(args.username, args.password)

    if args.tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set(args.tls_insecure)

    sampled_csv_dir = None if args.no_sampled_csv else args.csv_dir
    sink = Sink(args.jsonl, args.csv, sampled_csv_dir, args.sample_interval)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        code = int(reason_code) if isinstance(reason_code, int) else getattr(reason_code, "value", reason_code)
        if code == 0:
            print(f"connected: {args.host}:{args.port}, subscribing {args.topic}")
            client.subscribe(args.topic, qos=0)
        else:
            print(f"connect failed: {reason_code}")

    def on_message(client, userdata, msg):
        text, obj = decode_payload(msg.payload)
        print(f"{datetime.now().isoformat(timespec='seconds')} topic={msg.topic} payload={text}")
        sink.write(msg.topic, text, obj)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("stopped.")
        return 130
    finally:
        sink.close()
        client.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
