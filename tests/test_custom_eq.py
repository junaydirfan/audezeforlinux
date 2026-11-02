import cmath
import hashlib
import math
import os
import struct
import tempfile
import unittest
from unittest import mock

from maxwell2.client import MaxwellClient
from maxwell2.protocol import (
    EQ_BAND_FREQS,
    KEY_COEFF_BASE,
    KEY_UI_BASE,
    ProtocolError,
    build_coefficient_blob,
    build_race_packet,
    build_write_coefficients_command,
    parse_nvdm_write_ack,
    parse_race_packet,
    validate_coefficient_blob,
)

# Records read back from a Maxwell 2 (3329:4B28). The cut curve is the one confirmed audible.
FLAT_SHA256 = "60dad9fcf6c0ca7db5e2c9d7fbb6e40df7c7e5f4db522bf22146931873b31152"
CUT_SHA256 = "08a903be62f7af199014a0700a1e55d7b835db212aed7d233e543d3d0dbd0abb"


def bands(gains):
    return [
        {"band_index": 513, "frequency_hz": f, "gain_db": g, "q": f / 2.0}
        for f, g in zip(EQ_BAND_FREQS, gains)
    ]


def word(blob, offset):
    high, low = struct.unpack_from("<HH", blob, offset)
    value = (high << 16) | low
    return (value - (1 << 32) if value & 0x80000000 else value) >> 8


def response_db(blob, block, sample_rate, freq):
    """Decodes one block of a record and returns its level at freq."""
    base = 4 + block * 210 + 6
    z = cmath.exp(-2j * math.pi * freq / sample_rate)
    level = word(blob, base + 200) / 2**18
    for stage in range(10):
        w = [word(blob, base + stage * 20 + i * 4) for i in range(5)]
        b0, b1, b2, a1, a2 = w[0] / 2**23, w[1] / 2**22, w[2] / 2**23, w[3] / 2**22, w[4] / 2**23
        level *= abs((b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z))
    return 20 * math.log10(level)


class CoefficientBlobTests(unittest.TestCase):
    def test_flat_matches_device_record(self):
        blob = build_coefficient_blob(bands([0.0] * 10))
        self.assertEqual(hashlib.sha256(blob).hexdigest(), FLAT_SHA256)

    def test_cut_matches_verified_record(self):
        blob = build_coefficient_blob(bands([0, 0, 0, 0, 0, -12, -12, -12, -12, 0]))
        self.assertEqual(hashlib.sha256(blob).hexdigest(), CUT_SHA256)

    def test_extreme_curves_keep_shape_and_stay_below_full_scale(self):
        for gains in ([12.0] * 10, [-12.0] * 10, [12, -12, 12, -12, 12, -12, 12, -12, 12, -12],
                      [0, 0, 0, 0, 0, 0, 0, 0, 0, 12]):
            blob = build_coefficient_blob(bands(gains))
            validate_coefficient_blob(blob)
            levels = [response_db(blob, 1, 48000, 20 * 1000 ** (i / 299)) for i in range(300)]
            self.assertLessEqual(max(levels), 0.05)

    def test_boost_is_relative_to_lowered_curve(self):
        blob = build_coefficient_blob(bands([0, 0, 0, 0, 0, 0, 0, 0, 0, 12]))
        boost = response_db(blob, 1, 48000, 16000) - response_db(blob, 1, 48000, 1000)
        self.assertAlmostEqual(boost, 12.0, delta=0.5)

    def test_gain_limits_are_enforced(self):
        with self.assertRaises(ProtocolError):
            build_coefficient_blob(bands([12.5] + [0.0] * 9))

    def test_malformed_record_is_refused(self):
        blob = bytearray(build_coefficient_blob(bands([0.0] * 10)))
        blob[4] = 9
        with self.assertRaises(ProtocolError):
            validate_coefficient_blob(bytes(blob))
        with self.assertRaises(ProtocolError):
            build_write_coefficients_command(0, bytes(blob))

    def test_write_ack_rejection(self):
        ack = build_race_packet(0x0A0D, struct.pack("<BH", 1, KEY_COEFF_BASE))
        with self.assertRaises(ProtocolError):
            parse_nvdm_write_ack(b"\x05\x5B" + ack[2:], KEY_COEFF_BASE)


class FakeDevice:
    """In-memory NVDM store speaking just enough RACE for write_custom_eq."""

    dry_run = False

    def __init__(self):
        self.nvdm = {KEY_COEFF_BASE + s: build_coefficient_blob(bands([0.0] * 10)) for s in range(4)}
        self.preset = 4
        self.preset_history = []
        self.corrupt_key = None
        self.reject_key = None

    def _reply(self, cmd, payload):
        return b"\x05\x5B" + struct.pack("<HH", len(payload) + 2, cmd) + payload

    def exchange(self, packet, timeout_sec=1.5):
        cmd, payload = parse_race_packet(packet)
        if cmd == 0x0901:
            return self._reply(cmd, bytes([0, 0, 0, self.preset]))
        key = struct.unpack_from("<H", payload)[0]
        if cmd == 0x0A0C:
            data = self.nvdm.get(key, bytes(5) + bands_record())
            return self._reply(cmd, struct.pack("<BHH", 0, key, len(data)) + data)
        if key == self.reject_key:
            self.reject_key = None
            return self._reply(cmd, struct.pack("<BH", 1, key))
        data = payload[2:]
        if key == self.corrupt_key:
            self.corrupt_key = None
            data = b"\x00" + data[1:]
        self.nvdm[key] = data
        return self._reply(cmd, struct.pack("<BH", 0, key))

    def write_race_packet(self, packet):
        _, payload = parse_race_packet(packet)
        self.preset = payload[2]
        self.preset_history.append(self.preset)

    def poll_input_report(self):
        return None


def bands_record():
    return b"".join(struct.pack("<Hiiii", 513, f * 100, 0, f * 50, 0) for f in EQ_BAND_FREQS)


class WriteCustomEqTests(unittest.TestCase):
    def setUp(self):
        self.backups = tempfile.TemporaryDirectory()
        self.addCleanup(self.backups.cleanup)
        patcher = mock.patch.dict(os.environ, {"MAXWELL2_BACKUP_DIR": self.backups.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch("maxwell2.client.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.device = FakeDevice()
        self.client = MaxwellClient(self.device)
        self.curve = bands([3.0, 0, 0, 0, 0, 0, 0, 0, 0, 0])

    def test_save_writes_curve_and_sliders_then_activates(self):
        self.client.write_custom_eq(1, self.curve)
        self.assertEqual(self.device.nvdm[KEY_COEFF_BASE + 1], build_coefficient_blob(self.curve))
        self.assertIn(KEY_UI_BASE + 1, self.device.nvdm)
        self.assertEqual(self.device.preset, 8)

    def test_resaving_active_slot_forces_a_reload(self):
        self.device.preset = 8
        self.client.write_custom_eq(1, self.curve)
        self.assertEqual(self.device.preset_history, [9, 8])

    def test_failed_verification_restores_previous_curve(self):
        original = self.device.nvdm[KEY_COEFF_BASE]
        self.device.corrupt_key = KEY_COEFF_BASE
        with self.assertRaises(RuntimeError):
            self.client.write_custom_eq(0, self.curve)
        self.assertEqual(self.device.nvdm[KEY_COEFF_BASE], original)
        self.assertEqual(self.device.preset_history, [])

    def test_rejected_slider_write_restores_previous_curve(self):
        original = self.device.nvdm[KEY_COEFF_BASE]
        self.device.reject_key = KEY_UI_BASE
        with self.assertRaises(ProtocolError):
            self.client.write_custom_eq(0, self.curve)
        self.assertEqual(self.device.nvdm[KEY_COEFF_BASE], original)

    def test_unrecognised_record_is_left_alone(self):
        self.device.nvdm[KEY_COEFF_BASE] = bytes(844)
        with self.assertRaises(ProtocolError):
            self.client.write_custom_eq(0, self.curve)
        self.assertEqual(self.device.nvdm[KEY_COEFF_BASE], bytes(844))

    def test_numeric_preset_string_is_accepted(self):
        self.client.set_preset("3")
        self.assertEqual(self.device.preset, 3)


if __name__ == "__main__":
    unittest.main()
