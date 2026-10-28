"""
Unit tests for Audeze Maxwell 2 protocol builder, framer, and parsers.
Requires NO hardware connection - pure offline packet verification.
"""

import unittest
import struct
import os
import hashlib
from maxwell2.protocol import (
    build_race_packet,
    parse_race_packet,
    frame_output_reports,
    InputStream,
    build_read_preset_command,
    build_select_preset_command,
    build_battery_command,
    build_firmware_command,
    build_read_control_command,
    build_write_control_command,
    build_read_ui_eq_command,
    build_read_coefficients_command,
    parse_preset_index,
    parse_battery_notification,
    parse_firmware_notification,
    parse_control_response,
    parse_ui_eq_preset,
    ProtocolError,
    CMD_SETTING_READ,
    CMD_SETTING_WRITE,
    CMD_BATTERY,
    CMD_FIRMWARE,
    CMD_NVDM_READ,
    SUB_PRESET_INDEX,
    SUB_MIC_MUTE,
    SUB_SIDETONE_ON,
    SUB_CHAT_MIX,
    SUB_NOISE_REDUCTION,
    KEY_UI_BASE,
    KEY_COEFF_BASE,
    REPORT_ID_OUTPUT,
    REPORT_ID_INPUT,
    REPORT_LENGTH,
)


class TestProtocolFraming(unittest.TestCase):

    def test_build_race_packet_header(self):
        # Read preset command should be: 05 5A 04 00 01 09 00 00
        packet = build_read_preset_command()
        expected = bytes.fromhex("05 5A 04 00 01 09 00 00")
        self.assertEqual(packet, expected)

        cmd, payload = parse_race_packet(packet, expected_dir=0x5A)
        self.assertEqual(cmd, CMD_SETTING_READ)
        self.assertEqual(payload, b"\x00\x00")

    def test_frame_output_reports_single(self):
        packet = build_read_preset_command()  # 8 bytes
        reports = frame_output_reports(packet, is_dongle=True)
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(len(rep), REPORT_LENGTH)
        self.assertEqual(rep[0], REPORT_ID_OUTPUT)
        self.assertEqual(rep[1], 8)  # chunk length
        self.assertEqual(rep[2], 0x80)  # dongle route flag
        self.assertEqual(rep[3:11], packet)
        self.assertEqual(rep[11:], b"\x00" * (REPORT_LENGTH - 11))

    def test_frame_output_reports_chunking(self):
        # 120 bytes of dummy payload -> should split into 3 chunks (59 + 59 + 2)
        dummy_data = bytes(range(120))
        reports = frame_output_reports(dummy_data, is_dongle=False)
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0][1], 59)
        self.assertEqual(reports[0][2], 0x00)  # direct route
        self.assertEqual(reports[1][1], 59)
        self.assertEqual(reports[2][1], 2)

    def test_input_stream_reassembly(self):
        stream = InputStream()
        # Complete RACE packet: 05 5B 06 00 01 09 00 00 00 07 (Preset 1 / Custom)
        race_packet = bytes.fromhex("05 5B 06 00 01 09 00 00 00 07")
        report = bytes([REPORT_ID_INPUT, len(race_packet), 0x00]) + race_packet
        report += b"\x00" * (REPORT_LENGTH - len(report))

        packets = stream.feed(report)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0], race_packet)

        # Parse preset
        preset = parse_preset_index(packets[0])
        self.assertEqual(preset["index"], 7)
        self.assertEqual(preset["name"], "Preset 1 (Custom)")
        self.assertTrue(preset["is_custom"])
        self.assertEqual(preset["slot"], 0)


class TestCommandBuildersAndParsers(unittest.TestCase):

    def test_battery_command_and_notification(self):
        cmd = build_battery_command()
        self.assertEqual(cmd, bytes.fromhex("05 5A 03 00 D6 0C 00"))

        # Notification: 05 5D 05 00 D6 0C 00 00 55 (status 0, sub 0, 85%)
        notif = bytes.fromhex("05 5D 05 00 D6 0C 00 00 55")
        level = parse_battery_notification(notif)
        self.assertEqual(level, 85)

    def test_firmware_command_and_notification(self):
        cmd = build_firmware_command()
        self.assertEqual(cmd, bytes.fromhex("05 5A 03 00 07 1C 00"))

        # Notification with ASCII text: "v1.0.1.74" (len 9)
        fw_text = b"v1.0.1.74"
        payload = bytes([0x00, 0x00, len(fw_text)]) + fw_text
        notif = struct.pack("<BBHH", 0x05, 0x5D, len(payload) + 2, CMD_FIRMWARE) + payload
        parsed_fw = parse_firmware_notification(notif)
        self.assertEqual(parsed_fw, "v1.0.1.74")

    def test_select_preset_validation(self):
        cmd = build_select_preset_command(3)
        self.assertEqual(cmd, bytes.fromhex("05 5A 05 00 00 09 00 00 03"))

        with self.assertRaises(ProtocolError):
            build_select_preset_command(0)  # out of bounds

        with self.assertRaises(ProtocolError):
            build_select_preset_command(11)  # out of bounds

    def test_controls_building_and_parsing(self):
        # Read mic mute
        cmd_mute = build_read_control_command(SUB_MIC_MUTE)
        self.assertEqual(cmd_mute, bytes.fromhex("05 5A 04 00 01 09 22 00"))

        # Parse reply: mic unmuted (0xFF)
        reply_mute = bytes.fromhex("05 5B 06 00 01 09 22 00 00 FF")
        val = parse_control_response(reply_mute, SUB_MIC_MUTE)
        self.assertEqual(val, 0xFF)

        # Write sidetone ON
        cmd_st = build_write_control_command(SUB_SIDETONE_ON, 1)
        self.assertEqual(cmd_st, bytes.fromhex("05 5A 05 00 82 2C 07 00 01"))

        # Write Chat Mix to center (10)
        cmd_cm = build_write_control_command(SUB_CHAT_MIX, 10)
        self.assertEqual(cmd_cm, bytes.fromhex("05 5A 05 00 82 2C 0B 00 0A"))

        # Write AI Noise Reduction to High (2)
        cmd_nr = build_write_control_command(SUB_NOISE_REDUCTION, 2)
        self.assertEqual(cmd_nr, bytes.fromhex("05 5A 05 00 00 09 24 00 02"))

        # Invalid bounds
        with self.assertRaises(ProtocolError):
            build_write_control_command(SUB_CHAT_MIX, 25)

    def test_nvdm_keys_and_eq_ui_parser(self):
        # Read UI key for Slot 0 (Preset 1) -> Key 0xEF00
        cmd_ui = build_read_ui_eq_command(0)
        self.assertEqual(cmd_ui, bytes.fromhex("05 5A 06 00 0C 0A 00 EF E8 03"))

        # Read Coeff key for Slot 3 (Preset 4) -> Key 0xE42F
        cmd_coeff = build_read_coefficients_command(3)
        self.assertEqual(cmd_coeff, bytes.fromhex("05 5A 06 00 0C 0A 2F E4 E8 03"))

        # Synthesize a valid 10-band slider record for Slot 0:
        # Header: 5 bytes (0x01, 0x02, 0x03, 0x04, 0x05)
        # 10 bands x 18 bytes
        header = b"\x01\x00\x00\x00\x00"
        bands_data = bytearray()
        freqs = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        for idx, freq in enumerate(freqs):
            # tag=idx, freq=freq*100, gain=+3dB (300), q=141, flags=0
            bands_data.extend(struct.pack("<Hiiii", idx, freq * 100, 300, 141, 0))

        eq_payload = header + bytes(bands_data)
        data_len = len(eq_payload)
        # NVDM read response header: status (0), key (0xEF00), data_len
        nvdm_resp_payload = struct.pack("<BHH", 0, KEY_UI_BASE, data_len) + eq_payload
        race_resp = struct.pack("<BBHH", 0x05, 0x5B, len(nvdm_resp_payload) + 2, CMD_NVDM_READ) + nvdm_resp_payload

        parsed = parse_ui_eq_preset(race_resp, expected_slot=0)
        self.assertEqual(parsed["slot"], 0)
        self.assertEqual(len(parsed["bands"]), 10)
        self.assertEqual(parsed["bands"][0]["frequency_hz"], 32.0)
        self.assertEqual(parsed["bands"][0]["gain_db"], 3.0)
        self.assertEqual(parsed["bands"][5]["frequency_hz"], 1000.0)

    def test_write_ui_eq_builder(self):
        from maxwell2.protocol import build_write_ui_eq_command, CMD_NVDM_WRITE
        freqs = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        bands = [{"frequency_hz": f, "gain_db": 2.5} for f in freqs]
        packet = build_write_ui_eq_command(slot=0, bands=bands)

        cmd, payload = parse_race_packet(packet, expected_dir=0x5A)
        self.assertEqual(cmd, CMD_NVDM_WRITE)
        # Payload starts with key (0xEF00) + 185 bytes data = 187 bytes
        self.assertEqual(len(payload), 187)
        self.assertEqual(struct.unpack_from("<H", payload, 0)[0], KEY_UI_BASE)

        # Out-of-bounds gain test
        bad_bands = copy_bands = [{"frequency_hz": f, "gain_db": 15.0} for f in freqs]
        with self.assertRaises(ProtocolError):
            build_write_ui_eq_command(slot=0, bands=bad_bands)

    def test_malformed_race_packets(self):
        # Bad sync byte
        with self.assertRaises(ProtocolError):
            parse_race_packet(bytes.fromhex("04 5A 04 00 01 09 00 00"))

        # Bad length (says 10 bytes, but only 8 provided)
        with self.assertRaises(ProtocolError):
            parse_race_packet(bytes.fromhex("05 5A 0A 00 01 09 00 00"))

        # Too short
        with self.assertRaises(ProtocolError):
            parse_race_packet(b"\x05\x5A\x02")

    def test_safety_bounds_and_controls(self):
        # Noise reduction valid: 0, 1, 2
        build_write_control_command(SUB_NOISE_REDUCTION, 0)
        build_write_control_command(SUB_NOISE_REDUCTION, 1)
        build_write_control_command(SUB_NOISE_REDUCTION, 2)
        with self.assertRaises(ProtocolError):
            build_write_control_command(SUB_NOISE_REDUCTION, 3)

        # Sidetone enable: only 0 or 1
        with self.assertRaises(ProtocolError):
            build_write_control_command(SUB_SIDETONE_ON, 2)

        # Opcode 0x28 bounds check (136 or 142)
        build_write_control_command(0x28, 136)
        build_write_control_command(0x28, 142)
        with self.assertRaises(ProtocolError):
            build_write_control_command(0x28, 100)

        # Disallowed control write (Mic mute is read-only)
        with self.assertRaises(ProtocolError):
            build_write_control_command(SUB_MIC_MUTE, 1)

    def test_input_stream_self_healing(self):
        """Verifies that InputStream auto-recovers when leading garbage/zeros appear."""
        stream = InputStream()

        # Valid packet: 05 5A 04 00 01 09 00 00
        valid_packet = bytes.fromhex("05 5A 04 00 01 09 00 00")

        # Report with leading zeroes/garbage before the valid RACE packet
        corrupted_chunk = b"\x00\x00\x00\xFF" + valid_packet
        report = bytes([0x07, len(corrupted_chunk), 0x80]) + corrupted_chunk
        report += b"\x00" * (62 - len(report))

        packets = stream.feed(report)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0], valid_packet)

        # Clear buffer test
        stream.clear()
        self.assertEqual(len(stream.buffer), 0)

    def test_firmware_package_parsing_and_verification(self):
        """Verifies TLV parsing, LZMA decompression, and partition SHA verification."""
        import tempfile, lzma
        from maxwell2.firmware import FirmwarePackage, FotaSimulator, check_available_updates

        # Test catalog
        chk = check_available_updates("v1.0.5.7.4")
        self.assertTrue(chk["is_up_to_date"])
        self.assertEqual(chk["latest_available"], "v1.0.5.7.4")

        # Build synthetic package
        p1 = b"PARTITION_BOOTLOADER_" * 20
        p2 = b"PARTITION_KERNEL_APP_" * 20
        parts = [p1, p2]
        decomp = p1 + p2

        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 16384, "lc": 3, "lp": 0, "pb": 2}]
        comp = bytearray(lzma.compress(decomp, format=lzma.FORMAT_ALONE, filters=filters))
        comp[5:13] = struct.pack("<Q", len(decomp))

        hdr = bytearray(b"\xFF" * 0x1000)
        hdr[0x100:0x10E] = struct.pack("<HHBBII", 0x0011, 10, 1, 1, 0x1000, len(comp))
        hdr[0x10E:0x12E] = struct.pack("<HH", 0x0013, 28) + b"v1.0.5.7.4\x00" + b"\xFF" * 17

        pt = struct.pack("<I", 2)
        pt += struct.pack("<III", 0x1000, len(p1), 0x08013000)
        pt += struct.pack("<III", 0x1000 + len(p1), len(p2), 0x08133000)
        hdr[0x12E : 0x12E + 4 + len(pt)] = struct.pack("<HH", 0x0012, len(pt)) + pt

        hashes = struct.pack("<I", 2) + hashlib.sha256(p1).digest() + hashlib.sha256(p2).digest()
        h_off = 0x12E + 4 + len(pt)
        hdr[h_off : h_off + 4 + len(hashes)] = struct.pack("<HH", 0x0014, len(hashes)) + hashes

        full = hdr + comp
        full[0:32] = hashlib.sha256(full[0x100:]).digest()

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(full)
            t_path = f.name

        try:
            pkg = FirmwarePackage(t_path)
            self.assertEqual(pkg.version_string, "v1.0.5.7.4")
            self.assertEqual(len(pkg.partitions), 2)
            self.assertTrue(pkg.outer_hash_valid)
            self.assertTrue(pkg.verify_partitions())

            sim = FotaSimulator(pkg, "dongle")
            res = sim.simulate_transfer()
            self.assertEqual(res["target"], "dongle")
            self.assertGreater(res["total_hid_reports"], 0)

            # Test check_available_updates with package comparison
            chk_pkg_match = check_available_updates("v1.0.5.7.4", package_path=t_path)
            self.assertEqual(chk_pkg_match["status"], "VERSION MATCHES INSTALLED")
            self.assertTrue(chk_pkg_match["package_valid"])

            # Test FotaEngine execution in dry-run mode
            from maxwell2.firmware import FotaEngine
            engine = FotaEngine(pkg, target="dongle", dry_run=True)
            pre = engine.preflight_check()
            self.assertTrue(pre["package_valid"])
            progress_events = []
            flash_res = engine.execute(progress_callback=lambda p: progress_events.append(p))
            self.assertTrue(flash_res["success"])
            self.assertGreater(len(progress_events), 0)
        finally:
            if os.path.exists(t_path):
                os.unlink(t_path)

    def _write_package(self, chip_id=None, name=None, version="v1.0.5.7.4"):
        """Writes a minimal valid FOTA container, optionally with a TLV 0x0020 chip id."""
        import tempfile, lzma
        p1 = b"PARTITION_" * 40
        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 16384, "lc": 3, "lp": 0, "pb": 2}]
        comp = bytearray(lzma.compress(p1, format=lzma.FORMAT_ALONE, filters=filters))
        comp[5:13] = struct.pack("<Q", len(p1))

        hdr = bytearray(b"\xFF" * 0x1000)
        hdr[0x100:0x10E] = struct.pack("<HHBBII", 0x0011, 10, 1, 1, 0x1000, len(comp))
        ver = version.encode("ascii") + b"\x00"
        hdr[0x10E:0x12E] = struct.pack("<HH", 0x0013, 28) + ver + b"\xFF" * (28 - len(ver))
        pt = struct.pack("<I", 1) + struct.pack("<III", 0x1000, len(p1), 0x08013000)
        hdr[0x12E : 0x12E + 4 + len(pt)] = struct.pack("<HH", 0x0012, len(pt)) + pt
        hashes = struct.pack("<I", 1) + hashlib.sha256(p1).digest()
        h_off = 0x12E + 4 + len(pt)
        hdr[h_off : h_off + 4 + len(hashes)] = struct.pack("<HH", 0x0014, len(hashes)) + hashes
        if chip_id is not None:
            c = chip_id.encode("ascii") + b"\x00"
            c_off = h_off + 4 + len(hashes)
            hdr[c_off : c_off + 4 + len(c)] = struct.pack("<HH", 0x0020, len(c)) + c

        full = hdr + comp
        full[0:32] = hashlib.sha256(full[0x100:]).digest()
        if name:
            d = tempfile.mkdtemp()
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(full)
            self.addCleanup(os.rmdir, d)
            self.addCleanup(os.unlink, path)
            return path
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(full)
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_fota_preflight_chip_gating(self):
        """Maxwell 2 chips are recognised but blocked from live flashing; mismatches always fail."""
        from maxwell2.firmware import FirmwarePackage, FotaEngine, FotaSafetyError

        cases = [
            # chip id, target, dry_run, should pass
            # Live flashing is blocked for every chip until the protocol is verified.
            ("AB1568_Dongle", "dongle", True, True),
            ("AB1568_Dongle", "dongle", False, False),
            ("AB1568_Headset", "headset", False, False),
            ("AB1568_Dongle", "headset", True, False),
            ("AB1565_ULL2_DONGLE", "dongle", True, True),
            ("AB1565_ULL2_DONGLE", "dongle", False, False),
            ("AB1585_Headset", "headset", True, True),
            ("AB1585_Headset", "headset", False, False),
            ("AB1585_Headset", "dongle", True, False),
            ("AB9999_Mystery", "dongle", True, False),
            (None, "dongle", True, True),
            (None, "dongle", False, False),
        ]
        for chip, target, dry_run, ok in cases:
            with self.subTest(chip=chip, target=target, dry_run=dry_run):
                pkg = FirmwarePackage(self._write_package(chip))
                self.assertEqual(pkg.chip_id, chip or "Unknown")
                engine = FotaEngine(pkg, target=target, client=None, dry_run=dry_run)
                if ok:
                    self.assertEqual(engine.preflight_check()["chip_target"], chip or "Unknown")
                else:
                    with self.assertRaises(FotaSafetyError):
                        engine.preflight_check()

        info = FirmwarePackage(self._write_package("AB1585_Headset")).to_dict()
        self.assertEqual(info["chip_target"], "headset")
        self.assertFalse(info["live_flash_supported"])

    def test_firmware_track_detection(self):
        """Regular and ANC Maxwell 2 share chip ids, so the track comes from version / package identity."""
        from maxwell2.firmware import FirmwarePackage, detect_device_track, check_available_updates

        self.assertEqual(detect_device_track("v1.0.1.74"), "maxwell1")
        self.assertEqual(detect_device_track("v1.0.2.60.2"), "maxwell2")
        self.assertEqual(detect_device_track("v1.0.5.7.4"), "maxwell2")
        self.assertEqual(detect_device_track("v1.0.5.6"), "maxwell2_anc")
        self.assertEqual(detect_device_track("v1.0.5.9"), "maxwell2")
        # Strict mode (used before flashing) refuses to guess on versions it has not seen.
        self.assertIsNone(detect_device_track("v1.0.5.9", strict=True))
        self.assertIsNone(detect_device_track("Unknown", strict=True))
        self.assertEqual(detect_device_track("v1.0.5.6", strict=True), "maxwell2_anc")
        self.assertEqual(detect_device_track("v1.0.5.7.4", strict=True), "maxwell2")

        chk = check_available_updates("v1.0.5.6")
        self.assertIn("ANC", chk["model_detected"])
        self.assertEqual(chk["status"], "LATEST RELEASE UNKNOWN")

        regular = FirmwarePackage(self._write_package("AB1585_Headset", "Maxwell2_PS_headset_v1.0.5.7.4.bin"))
        anc = FirmwarePackage(self._write_package("AB1585_Headset", "Maxwell2ANC_PS_headset_v1.0.5.6.bin"))
        self.assertEqual(regular.firmware_track, "maxwell2")
        self.assertEqual(anc.firmware_track, "maxwell2_anc")
        self.assertEqual(FirmwarePackage(self._write_package("AB1568_Dongle")).firmware_track, "maxwell1")

        wrong = check_available_updates("v1.0.5.6", package_path=regular.filepath)
        self.assertEqual(wrong["status"], "WRONG FIRMWARE TRACK")
        same = check_available_updates("v1.0.5.6", package_path=anc.filepath)
        self.assertEqual(same["package_track"], "maxwell2_anc")
        self.assertNotEqual(same["status"], "WRONG FIRMWARE TRACK")

    def test_fota_preflight_track_gating(self):
        """Preflight refuses cross-track packages and live Maxwell 2 flashes with an unknown device track."""
        from maxwell2.firmware import FirmwarePackage, FotaEngine, FotaSafetyError

        regular = FirmwarePackage(self._write_package("AB1585_Headset", "Maxwell2_PS_headset_v1.0.5.7.4.bin"))
        anc = FirmwarePackage(self._write_package("AB1585_Headset", "Maxwell2ANC_PS_headset_v1.0.5.6.bin"))

        cases = [
            # package, device track, dry_run, error substring (None = passes)
            (regular, "maxwell2_anc", True, "track mismatch"),
            (anc, "maxwell2", True, "track mismatch"),
            (anc, "maxwell2_anc", True, None),
            (anc, None, True, None),
            (anc, None, False, "not supported yet"),
            (anc, "maxwell2_anc", False, "not supported yet"),
        ]
        for pkg, device_track, dry_run, err in cases:
            with self.subTest(pkg=pkg.filename, device_track=device_track, dry_run=dry_run):
                engine = FotaEngine(pkg, target="headset", dry_run=dry_run, device_track=device_track)
                if err is None:
                    self.assertEqual(engine.preflight_check()["firmware_track"], "maxwell2_anc")
                else:
                    with self.assertRaisesRegex(FotaSafetyError, err):
                        engine.preflight_check()



class FakeFotaTransport:
    """Stands in for Transport on the live flash path: records writes, can fail or be interrupted."""

    def __init__(self, is_dongle=True, fail_at=None, interrupt_at=None, product_id=None):
        self.dry_run = False
        self.is_dongle = is_dongle
        self.product_id = product_id
        self.fail_at = fail_at
        self.interrupt_at = interrupt_at
        self.writes = []  # (packet, is_dongle route flag)

    def write_race_packet(self, race_packet, is_dongle=None):
        if len(self.writes) == self.interrupt_at:
            raise KeyboardInterrupt
        if len(self.writes) == self.fail_at:
            raise OSError("Broken pipe")
        self.writes.append((race_packet, self.is_dongle if is_dongle is None else is_dongle))
        return []


class FakeFotaClient:
    """Stands in for MaxwellClient: serves battery and per-node versions, switching version after the transfer."""

    def __init__(self, transport, total_chunks=None, versions=None, after=None, battery=85, battery_error=None,
                 version_error=None):
        self.transport = transport
        self.total_chunks = total_chunks
        self.versions = versions or {"dongle": "v1.0.2.60.2", "headset": "v1.0.2.60.2"}
        self.after = after
        self.battery = battery
        self.battery_error = battery_error
        self.version_error = version_error

    def get_battery(self):
        if self.battery_error:
            raise self.battery_error
        return self.battery

    def get_firmware(self):
        if self.version_error:
            raise self.version_error
        done = self.total_chunks is not None and len(self.transport.writes) >= self.total_chunks
        return dict(self.after) if (done and self.after) else dict(self.versions)


class TestFotaBlockedByDefault(unittest.TestCase):
    """Without the test-only patch, no chip may be flashed live, even with a healthy device."""

    def test_no_chip_allows_live_flashing(self):
        from maxwell2.firmware import KNOWN_CHIP_IDS, FirmwarePackage, FotaEngine, FotaSafetyError
        self.assertFalse(any(live for _, live in KNOWN_CHIP_IDS.values()))
        pkg = FirmwarePackage(TestCommandBuildersAndParsers._write_package(self, "AB1585_Headset",
                                                                           "Maxwell2_PS_headset_v1.0.5.7.4.bin"))
        transport = FakeFotaTransport()
        engine = FotaEngine(pkg, target="headset", client=FakeFotaClient(transport), dry_run=False)
        with self.assertRaisesRegex(FotaSafetyError, "not supported yet"):
            engine.execute()
        self.assertEqual(transport.writes, [])


class TestFotaLiveHarness(unittest.TestCase):
    """Exercises the live flash path against a fake device, with live writes enabled only inside each test."""

    def setUp(self):
        from unittest import mock
        from maxwell2 import firmware
        patcher = mock.patch.dict(firmware.KNOWN_CHIP_IDS, {
            "AB1565_ULL2_DONGLE": ("dongle", True),
            "AB1585_Headset": ("headset", True),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    _write_package = TestCommandBuildersAndParsers._write_package

    def _engine(self, target="headset", version="v1.0.5.7.4", transport=None, after="match", **client_kw):
        from maxwell2.firmware import FirmwarePackage, FotaEngine
        chip = "AB1585_Headset" if target == "headset" else "AB1565_ULL2_DONGLE"
        pkg = FirmwarePackage(self._write_package(chip, f"Maxwell2_PS_{target}_{version}.bin", version))
        transport = transport or FakeFotaTransport()
        total = (pkg.file_size + 55) // 56
        if after == "match":
            after = {"dongle": version, "headset": version}
        client = FakeFotaClient(transport, total_chunks=total, after=after, **client_kw)
        engine = FotaEngine(pkg, target=target, client=client, dry_run=False, readback_timeout_sec=0)
        return engine, transport

    def test_routes_per_target(self):
        from maxwell2.firmware import FotaSafetyError
        # Through a dongle: dongle is route 0x00, headset is relayed on 0x80.
        engine, t = self._engine("dongle", transport=FakeFotaTransport(is_dongle=True))
        self.assertTrue(engine.execute()["success"])
        self.assertEqual({flag for _, flag in t.writes}, {False})
        engine, t = self._engine("headset", transport=FakeFotaTransport(is_dongle=True))
        engine.execute()
        self.assertEqual({flag for _, flag in t.writes}, {True})
        # Direct USB-C: the headset is route 0x00, and there is no dongle to flash.
        engine, t = self._engine("headset", transport=FakeFotaTransport(is_dongle=False))
        engine.execute()
        self.assertEqual({flag for _, flag in t.writes}, {False})
        engine, t = self._engine("dongle", transport=FakeFotaTransport(is_dongle=False))
        with self.assertRaisesRegex(FotaSafetyError, "not a dongle"):
            engine.execute()
        self.assertEqual(t.writes, [])

    def test_streams_whole_package_in_order(self):
        engine, t = self._engine()
        engine.execute()
        payload = b"".join(pkt[6:] for pkt, _ in t.writes)
        self.assertEqual(payload, bytes(engine.package.raw_data))
        self.assertTrue(all(pkt[:2] == b"\x15\x5a" for pkt, _ in t.writes))

    def test_fails_closed_on_unreadable_device_state(self):
        from maxwell2.firmware import FotaSafetyError
        cases = [
            ("battery None", dict(battery=None), "battery"),
            ("battery error", dict(battery_error=OSError("timeout")), "battery"),
            ("battery low", dict(battery=30), "30%"),
            ("version unknown", dict(versions={"dongle": "Unknown", "headset": "Unknown"}), "version"),
            ("version error", dict(version_error=OSError("timeout")), "version"),
            ("unknown release", dict(versions={"dongle": "v1.0.5.9", "headset": "v1.0.5.9"}), "not a known release"),
            ("ANC device", dict(versions={"dongle": "v1.0.5.6", "headset": "v1.0.5.6"}), "track mismatch"),
        ]
        for name, kw, err in cases:
            with self.subTest(name):
                engine, t = self._engine(**kw)
                with self.assertRaisesRegex(FotaSafetyError, err):
                    engine.execute()
                self.assertEqual(t.writes, [])

        # The ANC Xbox dongle PID marks the device as ANC even when its version is a regular release.
        engine, t = self._engine(transport=FakeFotaTransport(product_id=0x4BA8))
        with self.assertRaisesRegex(FotaSafetyError, "track mismatch"):
            engine.execute()
        self.assertEqual(t.writes, [])

        from maxwell2.firmware import FirmwarePackage, FotaEngine
        pkg = FirmwarePackage(self._write_package("AB1585_Headset", "Maxwell2_PS_headset_v1.0.5.7.4.bin"))
        for client in (None, FakeFotaClient(type("T", (), {"dry_run": True, "is_dongle": True})())):
            with self.subTest(client=client):
                with self.assertRaisesRegex(FotaSafetyError, "device connection|dry-run"):
                    FotaEngine(pkg, target="headset", client=client, dry_run=False).preflight_check()

    def test_downgrade_needs_confirmation(self):
        from maxwell2.firmware import FotaSafetyError
        newer = {"dongle": "v1.0.5.7.4", "headset": "v1.0.5.7.4"}
        engine, t = self._engine(version="v1.0.2.60.2", versions=newer)
        with self.assertRaisesRegex(FotaSafetyError, "OLDER"):
            engine.execute()
        self.assertEqual(t.writes, [])
        engine, t = self._engine(version="v1.0.2.60.2", versions=newer)
        engine.allow_downgrade = True
        self.assertTrue(engine.preflight_check()["is_downgrade"])
        self.assertTrue(engine.execute()["success"])

    def test_interrupt_and_write_failure(self):
        from maxwell2.firmware import FotaInterruptedError, FotaTransferError
        engine, _ = self._engine(transport=FakeFotaTransport(interrupt_at=5))
        with self.assertRaises(FotaInterruptedError) as cm:
            engine.execute()
        self.assertEqual(cm.exception.chunks_sent, 5)
        self.assertIn("Do NOT unplug", str(cm.exception))

        engine, _ = self._engine(transport=FakeFotaTransport(interrupt_at=0))
        with self.assertRaisesRegex(FotaInterruptedError, "nothing was written"):
            engine.execute()

        engine, _ = self._engine(transport=FakeFotaTransport(fail_at=3))
        with self.assertRaises(FotaTransferError) as cm:
            engine.execute()
        self.assertNotIsInstance(cm.exception, FotaInterruptedError)
        self.assertEqual(cm.exception.chunks_sent, 3)
        self.assertIn("Broken pipe", str(cm.exception))

    def test_post_flash_readback(self):
        res = self._engine()[0].execute()
        self.assertEqual((res["success"], res["verification"]), (True, "confirmed"))

        stale = {"dongle": "v1.0.2.60.2", "headset": "v1.0.2.60.2"}
        res = self._engine(after=stale)[0].execute()
        self.assertEqual((res["success"], res["verification"]), (False, "version_mismatch"))
        self.assertEqual(res["reported_version"], "v1.0.2.60.2")

        gone = {"dongle": "Unknown", "headset": "Unknown"}
        res = self._engine(after=gone)[0].execute()
        self.assertEqual((res["success"], res["verification"]), (False, "no_response"))


if __name__ == "__main__":
    unittest.main()
