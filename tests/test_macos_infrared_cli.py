import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import macos_infrared_cli as cli


class MacosInfraredCliTests(unittest.TestCase):
    def test_config_i2c_1m_command(self):
        command = cli.build_config_i2c_command(rate=0x0B, stretch=10000)
        self.assertEqual(command, bytes.fromhex("03 00 08 0B 00 00 27 10"))

    def test_i2c_register_read_command(self):
        command = cli.build_i2c_register_read_command(0x33, 0x2400, 1664)
        self.assertEqual(command, bytes.fromhex("0B 00 0B 00 00 33 02 06 80 24 00"))

    def test_i2c_register_write_command(self):
        command = cli.build_i2c_register_write_command(0x33, 0x800D, bytes.fromhex("19 00"))
        self.assertEqual(command, bytes.fromhex("0A 00 0D 00 00 33 02 00 02 80 0D 19 00"))

    def test_i2c_send_receive_command(self):
        command = cli.build_i2c_send_receive_command(1, 1, bytes.fromhex("66 80 00"), 0)
        self.assertEqual(command, bytes.fromhex("09 00 0C 01 01 00 03 00 00 66 80 00"))

    def test_dll_style_register_read_sequence(self):
        commands = cli.build_i2c_register_read_sequence(0x33, 0x8000, 2, repeated_start=False)
        self.assertEqual(commands[0], bytes.fromhex("09 00 0C 01 01 00 03 00 00 66 80 00"))
        self.assertEqual(commands[1], bytes.fromhex("09 00 0A 01 00 00 01 00 00 67"))
        self.assertEqual(commands[2], bytes.fromhex("09 00 09 00 01 00 00 00 02"))

    def test_dll_style_register_write_command(self):
        command = cli.build_i2c_register_write_via_dll_command(0x33, 0x800D, bytes.fromhex("19 00"))
        self.assertEqual(command, bytes.fromhex("09 00 0E 01 01 00 05 00 00 66 80 0D 19 00"))

    def test_refresh_rate_bits(self):
        self.assertEqual(cli.with_refresh_rate(0x0000, 4), 0x0200)
        self.assertEqual(cli.refresh_rate(0x0200), 4)
        self.assertEqual(cli.with_refresh_rate(0xFFFF, 4), 0xFFFF & ~0x0380 | 0x0200)

    def test_refresh_rate_hz_parser(self):
        self.assertEqual(cli.parse_refresh_rate_hz("8"), 8.0)
        self.assertEqual(cli.refresh_rate_bits_from_hz(8.0), cli.REFRESH_RATE_8HZ)
        self.assertEqual(cli.refresh_rate_bits_from_hz(32.0), cli.REFRESH_RATE_32HZ)
        with self.assertRaises(cli.argparse.ArgumentTypeError):
            cli.parse_refresh_rate_hz("12")

    def test_full_frame_gate_emits_only_after_both_subpages(self):
        gate = cli.MlxFullFrameGate()

        decisions = [gate.should_emit(subpage) for subpage in [0, 0, 1, 1, 0, 1]]

        self.assertEqual(decisions, [False, False, True, False, True, True])

    def test_verified_driver_control_bits(self):
        control = 0
        control = cli.with_chess_mode(control)
        control = cli.with_resolution(control, cli.RESOLUTION_18BIT)
        control = cli.with_refresh_rate(control, cli.REFRESH_RATE_8HZ)
        self.assertEqual(control, 0x1A00)
        self.assertEqual(cli.resolution(control), 2)
        self.assertEqual(cli.refresh_rate(control), 4)
        self.assertTrue(control & cli.CHESS_MODE_MASK)

    def test_default_read_mode_uses_atomic_usb2uart_register_command(self):
        self.assertEqual(cli.DEFAULT_READ_MODE, "register")

    def test_robot_thermal_bytes_match_legacy_offset(self):
        pixels = [28.0, 28.4, 28.5, -100.0, 300.0, float("nan")] + [25.0] * (cli.PIXEL_WORDS - 6)

        raw = cli.temperatures_to_robot_thermal_bytes(pixels)

        self.assertEqual(len(raw), cli.PIXEL_WORDS)
        self.assertEqual(list(raw[:6]), [72, 72, 73, 0, 255, 0])

    def test_i2c_rate_parser(self):
        self.assertEqual(cli.parse_i2c_rate("1m"), 0x0B)
        self.assertEqual(cli.parse_i2c_rate("400k"), 0x08)
        self.assertEqual(cli.parse_i2c_rate("0x0B"), 0x0B)

    def test_rejects_all_ff_eeprom(self):
        with self.assertRaises(cli.CliError):
            cli.ensure_mlx_eeprom_looks_valid([0xFFFF] * cli.EEPROM_WORDS)

    def test_chunked_reads_advance_register_address(self):
        class FakeBus:
            def __init__(self):
                self.calls = []

            def read_register_words(self, address, register, count, read_mode=cli.DEFAULT_READ_MODE):
                self.calls.append((address, register, count, read_mode))
                return list(range(register, register + count))

        bus = FakeBus()
        dev = cli.Mlx90640Device(bus, address=0x33, read_chunk_words=4, read_mode="dll-stop")
        words = dev.read_words_chunked(0x2400, 10)
        self.assertEqual(
            bus.calls,
            [
                (0x33, 0x2400, 4, "dll-stop"),
                (0x33, 0x2404, 4, "dll-stop"),
                (0x33, 0x2408, 2, "dll-stop"),
            ],
        )
        self.assertEqual(len(words), 10)

    def test_i2c_read_stats_track_responses_and_failures(self):
        bus = cli.Usb2UartSerialI2c("dummy")

        def successful_write(command, response_len=0, response_timeout=None):
            return b"\x12\x34"

        bus._write = successful_write
        self.assertEqual(bus.read_register_bytes(0x33, cli.STATUS_REGISTER, 2), b"\x12\x34")
        self.assertEqual(bus.i2c_stats.read_requests, 1)
        self.assertEqual(bus.i2c_stats.read_responses, 1)
        self.assertEqual(bus.i2c_stats.read_failures, 0)
        self.assertEqual(bus.i2c_stats.response_bytes, 2)
        self.assertEqual(bus.i2c_stats.pending_reads, 0)

        def failing_write(command, response_len=0, response_timeout=None):
            raise cli.CliError("timeout")

        bus._write = failing_write
        with self.assertRaises(cli.CliError):
            bus.read_register_bytes(0x33, cli.STATUS_REGISTER, 2)
        self.assertEqual(bus.i2c_stats.read_requests, 2)
        self.assertEqual(bus.i2c_stats.read_responses, 1)
        self.assertEqual(bus.i2c_stats.read_failures, 1)
        self.assertEqual(bus.i2c_stats.pending_reads, 0)

    def test_capture_writer_saves_i2c_event_stats(self):
        with TemporaryDirectory() as tmp:
            stats = cli.I2cEventStats()
            stats.record_read_request()
            stats.record_read_response(2)

            with cli.MlxCaptureWriter(Path(tmp), {"kind": "test"}, [0x1234]) as writer:
                session_dir = writer.session_dir
                writer.write_i2c_stats(stats)
                entries = writer.file_entries()

            self.assertEqual(entries["i2cEventsJson"], "raw/i2c_events.json")
            i2c_text = (session_dir / "raw" / "i2c_events.json").read_text(encoding="utf-8")
            self.assertIn('"i2cReadRequests": 1', i2c_text)
            self.assertIn('"i2cReadResponses": 1', i2c_text)

    def test_capture_writer_saves_eeprom_raw_files(self):
        with TemporaryDirectory() as tmp:
            eeprom = [0x1234, 0xABCD, 0x0001]
            with cli.MlxCaptureWriter(Path(tmp), {"kind": "test"}, eeprom) as writer:
                session_dir = writer.session_dir

            self.assertEqual((session_dir / "raw" / "eeprom.u16le").read_bytes(), bytes.fromhex("34 12 cd ab 01 00"))
            csv_text = (session_dir / "raw" / "eeprom.csv").read_text(encoding="utf-8")
            self.assertIn("0x2400,0x1234,4660", csv_text)
            self.assertIn("0x2401,0xABCD,43981", csv_text)
            session_text = (session_dir / "session.json").read_text(encoding="utf-8")
            self.assertIn("raw/eeprom.u16le", session_text)
            self.assertIn("raw/frameData.layout.json", session_text)
            self.assertIn("temp/to.layout.json", session_text)
            self.assertIn("temp/mlx90640_infrared_thermal.bin", session_text)
            self.assertIn("temp/mlx90640_infrared_thermal.layout.json", session_text)
            frame_layout = (session_dir / "raw" / "frameData.layout.json").read_text(encoding="utf-8")
            self.assertIn("infrared::FrameData", frame_layout)
            self.assertIn("\"recordWords\": 834", frame_layout)

    def test_capture_writer_saves_robot_thermal_bin_stream(self):
        with TemporaryDirectory() as tmp:
            pixels = [28.0 + (index / 100.0) for index in range(cli.PIXEL_WORDS)]
            with cli.MlxCaptureWriter(Path(tmp), {"kind": "test"}, [0x1234]) as writer:
                session_dir = writer.session_dir
                writer.write_frame(cli.east8_now(), 1, 24.0, pixels)

            robot_bin = session_dir / "temp" / "mlx90640_infrared_thermal.bin"
            latest_bin = session_dir / "temp" / "mlx90640_infrared_thermal_latest.bin"
            self.assertEqual(robot_bin.read_bytes(), latest_bin.read_bytes())
            self.assertEqual(len(robot_bin.read_bytes()), cli.PIXEL_WORDS)
            self.assertEqual(robot_bin.read_bytes()[0], 72)
            self.assertIn("robot_thermal_u8_offset_bytes", (session_dir / "mlx_frames.csv").read_text(encoding="utf-8"))
            layout_text = (session_dir / "temp" / "mlx90640_infrared_thermal.layout.json").read_text(encoding="utf-8")
            self.assertIn("raw_byte - offset_c", layout_text)

    def test_capture_writer_channel_prefixes_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "session"
            pixels = [25.0] * cli.PIXEL_WORDS
            with cli.MlxCaptureWriter(
                root,
                {"kind": "test"},
                [0x1234],
                session_dir=session_dir,
                channel="left",
                write_session_json=False,
            ) as writer:
                summary = writer.write_frame(cli.east8_now(), 1, 24.0, pixels)
                entries = writer.file_entries()

            self.assertEqual(summary.channel, "left")
            self.assertEqual(entries["frameDataU16Le"], "raw/left_frameData.u16le")
            self.assertTrue((session_dir / "raw" / "left_eeprom.u16le").exists())
            self.assertTrue((session_dir / "temp" / "left_infrared_thermal.bin").exists())
            self.assertIn("left_mlx_frames.csv", entries["framesCsv"])

    def test_parse_hex_payload_accepts_common_dump_formats(self):
        self.assertEqual(cli.parse_hex_payload("01:02:0A ff"), bytes.fromhex("01 02 0a ff"))
        self.assertEqual(cli.parse_hex_payload("0x01, 0x02, 0x03"), bytes.fromhex("01 02 03"))

    def test_tasi_tshark_csv_importer_reads_hid_payloads(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ta612.csv"
            path.write_text(
                "frame.time_epoch,usb.endpoint_address,usbhid.data,usb.capdata\n"
                "1780000000.5,0x81,01:02:03:04,\n"
                "1780000001.5,0x01,,05:06:07:08:09\n",
                encoding="utf-8",
            )

            reports = list(cli.iter_tasi_reports_from_tshark_csv(path, min_len=4, max_len=0))

            self.assertEqual([report.raw for report in reports], [bytes.fromhex("01 02 03 04"), bytes.fromhex("05 06 07 08 09")])
            self.assertEqual(reports[0].direction, "in")
            self.assertEqual(reports[1].direction, "out")

    def test_import_tasi_capture_writes_raw_report_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ta612.txt"
            path.write_text("IN 01 02 03 04\nOUT 05 06 07 08\n", encoding="utf-8")
            args = cli.argparse.Namespace(
                input=str(path),
                format="hex-text",
                min_len=4,
                max_len=0,
                preview=0,
                capture_root=str(root / "captures"),
            )

            cli.cmd_import_tasi_capture(args)
            session_dir = next((root / "captures").glob("mac_tasi_import_*"))
            raw = (session_dir / "raw" / "tasi_hid_reports.bin").read_bytes()

            self.assertEqual(raw, bytes.fromhex("04 00 00 00 01 02 03 04 04 00 00 00 05 06 07 08"))
            csv_text = (session_dir / "tasi_raw_reports.csv").read_text(encoding="utf-8")
            self.assertIn("raw_hid_unparsed", csv_text)

    def test_tasi_host_commands_match_protocol_examples(self):
        self.assertEqual(cli.build_tasi_host_frame(0x01), bytes.fromhex("aa 55 01 03 03"))
        self.assertEqual(cli.build_tasi_host_frame(0x00), bytes.fromhex("aa 55 00 03 02"))
        self.assertEqual(cli.build_tasi_host_frame(0x02), bytes.fromhex("aa 55 02 03 04"))

    def test_parse_ta612_realtime_frame(self):
        frame_without_sum = bytes.fromhex("55 aa 01 0b") + bytes.fromhex("f6 00 00 01 0a 01 14 01")
        frame = frame_without_sum + bytes([sum(frame_without_sum) & 0xFF])

        parsed = cli.parse_tasi_frame(frame)

        self.assertTrue(parsed["checksum_ok"])
        self.assertEqual(parsed["header"], cli.TASI_DEVICE_HEADER)
        self.assertEqual(parsed["command"], 0x01)
        self.assertEqual(parsed["channels_c"], [24.6, 25.6, 26.6, 27.6])

    def test_find_tasi_frame_skips_noise(self):
        frame_without_sum = bytes.fromhex("55 aa 00 06 64 02 64 00")
        frame = frame_without_sum + bytes([sum(frame_without_sum) & 0xFF])
        buffer = bytearray(bytes.fromhex("00 ff 10") + frame)

        self.assertEqual(cli.find_tasi_frame(buffer), frame)
        self.assertEqual(buffer, bytearray())

    def test_mlx_writer_merges_combined_raw_files_in_session_json(self):
        with TemporaryDirectory() as tmp:
            with cli.MlxCaptureWriter(
                Path(tmp),
                {
                    "kind": "combined-test",
                    "rawFiles": {
                        "tasiSerialFramesBin": "raw/tasi_serial_frames.bin",
                        "joinedSummaryCsv": "joined_summary.csv",
                    },
                },
                [0x1234],
                session_prefix="mac_mlx_tasi",
            ) as writer:
                session_dir = writer.session_dir

            session_text = (session_dir / "session.json").read_text(encoding="utf-8")
            self.assertIn("raw/frameData.u16le", session_text)
            self.assertIn("raw/tasi_serial_frames.bin", session_text)
            self.assertIn("joined_summary.csv", session_text)

    def test_tasi_serial_writer_and_joined_summary(self):
        with TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            frame_without_sum = bytes.fromhex("55 aa 01 0b fa 00 4c 01 fe 00 ff 00")
            raw = frame_without_sum + bytes([sum(frame_without_sum) & 0xFF])
            parsed = cli.parse_tasi_frame(raw)
            timestamp = cli.east8_now()

            with cli.TasiSerialCaptureWriter(session_dir) as tasi_writer:
                sample = tasi_writer.write_frame(timestamp, raw, parsed)
            mlx = cli.MlxFrameSummary(timestamp, 1, 0, 768, 30.0, 20.0, 40.0, 25.0, 26.0)
            with cli.JoinedSummaryWriter(session_dir) as joined:
                joined.write(mlx, sample)

            self.assertEqual((session_dir / "raw" / "tasi_serial_frames.bin").read_bytes(), bytes([len(raw), 0, 0, 0]) + raw)
            self.assertIn("25.000000", (session_dir / "joined_summary.csv").read_text(encoding="utf-8"))
            self.assertIn("33.200000", (session_dir / "joined_summary.csv").read_text(encoding="utf-8"))

    def test_joined_summary_can_include_mlx_channel(self):
        with TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            timestamp = cli.east8_now()
            mlx = cli.MlxFrameSummary(timestamp, 1, 0, 768, 30.0, 20.0, 40.0, 25.0, 26.0, channel="right")

            with cli.JoinedSummaryWriter(session_dir, include_mlx_channel=True) as joined:
                joined.write(mlx, None)

            csv_text = (session_dir / "joined_summary.csv").read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("mlx_channel,mlx_timestamp_east8"))
            self.assertIn("right,", csv_text)


if __name__ == "__main__":
    unittest.main()
