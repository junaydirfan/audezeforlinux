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

    def _write_package(self, chip_id=None, name=None):
        """Writes a minimal valid FOTA container, optionally with a TLV 0x0020 chip id."""
        import tempfile, lzma
        p1 = b"PARTITION_" * 40
        filters = [{"id": lzma.FILTER_LZMA1, "dict_size": 16384, "lc": 3, "lp": 0, "pb": 2}]
        comp = bytearray(lzma.compress(p1, format=lzma.FORMAT_ALONE, filters=filters))
        comp[5:13] = struct.pack("<Q", len(p1))

        hdr = bytearray(b"\xFF" * 0x1000)
        hdr[0x100:0x10E] = struct.pack("<HHBBII", 0x0011, 10, 1, 1, 0x1000, len(comp))
        hdr[0x10E:0x12E] = struct.pack("<HH", 0x0013, 28) + b"v1.0.5.7.4\x00" + b"\xFF" * 17
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
            ("AB1568_Dongle", "dongle", False, True),
            ("AB1568_Headset", "headset", False, True),
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
            (anc, None, False, "regular or ANC"),
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


if __name__ == "__main__":
    unittest.main()
