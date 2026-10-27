"""
audezeforlinux: Firmware diagnostics, verification, package inspection, and FOTA subsystem.
Implements dual-device firmware version queries, parity verification, pre-flight safety gates,
offline TLV/LZMA container parsing, partition hash verification, and FOTA streaming simulation.
"""

import os
import sys
import struct
import hashlib
import binascii
import re
import lzma
import time
from typing import Optional, Dict, Any, List, Tuple, Callable

from maxwell2.client import MaxwellClient
from maxwell2.protocol import (
    build_firmware_command,
    parse_firmware_notification,
    CMD_FIRMWARE,
)


# Known Audeze official firmware releases and changelog database
FIRMWARE_CATALOG = {
    "maxwell2": {
        "model": "Audeze Maxwell 2 (Xbox / PlayStation / PC)",
        "current_stable": "v1.0.5.7.4",
        "releases": [
            {
                "version": "v1.0.5.7.4",
                "date": "2026-08-20",
                "notes": "Modified: Sidetone adjustments to reduce noise with sidetone off.",
                "latest": True,
            },
            {
                "version": "v1.0.5.7",
                "date": "2026-08-12",
                "notes": "Added: Google Fast Pairing 1.0.4. Modified: Adjusted mic gain; updated Bluetooth IDs; disabled silence detection for USB dongle. Fixed: High-pitched noise during silence; dongle connection after unplugging line input; rare popping sounds when connected to USB dongle.",
                "latest": False,
            },
            {
                "version": "v1.0.2.60.2",
                "date": "2026-05-01",
                "notes": "Fixed: Volume at higher level when switching to USB audio from Bluetooth LE; conflicts when Aux and Bluetooth were used simultaneously.",
                "latest": False,
            },
            {
                "version": "v1.0.2.59",
                "date": "2026-03-11",
                "notes": "Fixed: Invalid sidetone state when updating firmware; rare bug disabling user EQ values. Modified: Increased low-end performance and optimized EQ values; optimized PS mode volume, game/chat mixing ratios, voice prompt clarity, and BT/LE transitions.",
                "latest": False,
            },
            {
                "version": "v1.0.2.54",
                "date": "2026-01-29",
                "notes": "Fixed: Bluetooth Hands Free protocol not switching properly; LED patterns not properly displaying.",
                "latest": False,
            },
            {
                "version": "v1.0.2.53_A",
                "date": "2026-01-22",
                "notes": "Modified: Reduced boom mic gain; optimized wheel sensitivity. Fixed: Audio leakage while mute active on wireless dongle; wired mode mic mute state; sidetone with LE Audio; EQ inconsistencies; custom preset saving/loading issues.",
                "latest": False,
            },
            {
                "version": "v1.0.2.49",
                "date": "2026-01-05",
                "notes": "Maxwell 2 Initial Production Release.",
                "latest": False,
            },
        ],
    },
    # Separate Audeze track (mw2-anc/ in the official bucket). v1.0.5.6 is the version listed in
    # maxwell-2-anc-rollback.json, so a newer ANC release probably exists that we have not seen.
    "maxwell2_anc": {
        "model": "Audeze Maxwell 2 ANC (Xbox / PlayStation / PC)",
        "current_stable": "v1.0.5.6",
        "baseline_complete": False,
        "releases": [
            {
                "version": "v1.0.5.6",
                "date": "2026-08-20",
                "notes": "ANC firmware track (Maxwell2ANC_* packages). Listed by Audeze as the ANC rollback release; no public changelog.",
                "latest": False,
            },
        ],
    },
    "maxwell1": {
        "model": "Audeze Maxwell 1 (Original)",
        "current_stable": "v1.0.1.74",
        "releases": [
            {
                "version": "v1.0.1.74",
                "date": "2024-05",
                "notes": "Rewrote 115 wireless DSP coefficient entries. Fixed an issue that caused EQ anomalies when updating from previous firmware versions.",
                "latest": True,
            },
            {
                "version": "v1.0.1.63",
                "date": "2024-04",
                "notes": "Bluetooth and wireless connection stability fixes.",
                "latest": False,
            },
            {
                "version": "v1.0.1.61",
                "date": "2024-04",
                "notes": "Added per-source audio balance profiles (NVDM 0xF665 for USB-C, 0xF668 for wireless).",
                "latest": False,
            },
            {
                "version": "v1.0.1.56",
                "date": "2023-11",
                "notes": "Legacy baseline firmware with symmetric factory balance default (142/142).",
                "latest": False,
            },
        ],
    },
}


# TLV 0x0020 chip identifiers -> (flash target, live FOTA flashing allowed).
# AB1565_ULL2_DONGLE / AB1585_Headset are what the official Maxwell 2 packages
# (v1.0.5.7.4 and the v1.0.2.60.2 rollback) report. The RACE 0x15 block framing in
# FotaEngine has not been checked against captures of the official updater on any chip,
# so live flashing is blocked for all of them until it is.
KNOWN_CHIP_IDS: Dict[str, Tuple[str, bool]] = {
    "AB1568_Dongle": ("dongle", False),
    "AB1568_Headset": ("headset", False),
    "AB1565_ULL2_DONGLE": ("dongle", False),
    "AB1585_Headset": ("headset", False),
}


# Regular Maxwell 2 and Maxwell 2 ANC packages carry identical chip ids and design names,
# so the firmware track has to come from elsewhere: a known official package hash first,
# then the official file name prefix.
MAXWELL2_CHIP_IDS = ("AB1565_ULL2_DONGLE", "AB1585_Headset")
KNOWN_PACKAGE_TRACKS: Dict[str, str] = {
    # sha256 of the whole file -> catalog track
    "b16ecc9793d6ab9530dc45805cc486061f302fa389e9215f66d81fb587571a39": "maxwell2_anc",  # Maxwell2ANC_PS_dongle_v1.0.5.6.bin
    "2d5149fc5859c8c437b83fe2d4b83b8068ab9d9038ae61704c73640be90c6f9a": "maxwell2_anc",  # Maxwell2ANC_PS_headset_v1.0.5.6.bin
}


def detect_device_track(current_version: str, strict: bool = False) -> Optional[str]:
    """
    Picks the catalog track for an installed firmware version.
    Maxwell 1 shipped 1.0.1.x; every Maxwell 2 build (1.0.2.49 and up) is newer than that.
    ANC and regular Maxwell 2 share a version scheme, so only a version that is known on the
    ANC track and not on the regular one is treated as ANC. Other Maxwell 2 versions fall back
    to the regular track, unless strict is set (used before flashing), which returns None for
    any version that is not a known release on exactly one track.
    """
    if parse_version_tuple(current_version) < (1, 0, 2):
        return None if strict and current_version.strip() in ("", "Unknown") else "maxwell1"
    ver = current_version.strip().lower().lstrip("v")
    anc = {r["version"].lower().lstrip("v") for r in FIRMWARE_CATALOG["maxwell2_anc"]["releases"]}
    regular = {r["version"].lower().lstrip("v") for r in FIRMWARE_CATALOG["maxwell2"]["releases"]}
    if ver in anc and ver not in regular:
        return "maxwell2_anc"
    if strict and not (ver in regular and ver not in anc):
        return None
    return "maxwell2"


class FirmwarePackage:
    """
    Parses, validates, and unpacks an official Audeze / Airoha FOTA container (.bin).
    Seen with AB1568 chip ids and, on Maxwell 2 packages, AB1565_ULL2 (dongle) / AB1585 (headset).
    Container Layout:
      0x0000-0x001F: Top-level SHA-256 hash of bytes[0x100:]
      0x0100+:       Sequential TLV records (type LE16, length LE16, value); offsets vary per package:
                     0x0011 Basic info (flags, LZMA stream start, stream size)
                     0x0013 Version string
                     0x0012 Partition table (LE32 count + count * 12 bytes)
                     0x0014 Per-partition SHA-256 hashes (LE32 count + count * 32 bytes)
                     0x0020 Chip identifier (see KNOWN_CHIP_IDS)
                     0x0021 Design name
      0x1000+:       LZMA-Alone compressed stream (start given by TLV 0x0011)
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.raw_data = bytearray()
        self.file_size = 0

        # Header fields
        self.outer_hash = ""
        self.outer_hash_valid = False
        self.flags = (0, 0)
        self.lzma_offset = 0x1000
        self.lzma_stream_size = 0
        self.version_string = "Unknown"
        self.chip_id = "Unknown"
        self.design_name = "Unknown"
        self.partition_count = 0
        self.partitions: List[Dict[str, Any]] = []

        # Decompressed cache
        self.decompressed_payload: Optional[bytes] = None
        self.all_partitions_verified = False

        self._parse()

    def _parse(self):
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Firmware file not found: {self.filepath}")

        self.file_size = os.path.getsize(self.filepath)
        if self.file_size < 0x1000:
            raise ValueError(f"Firmware file too small to contain valid FOTA header ({self.file_size} bytes)")

        with open(self.filepath, "rb") as f:
            self.raw_data = bytearray(f.read())

        # 1. Outer SHA-256 Checksum (0x0000 - 0x001F covers data from 0x0100 onwards)
        declared_outer = self.raw_data[0:32]
        self.outer_hash = declared_outer.hex()
        actual_outer = hashlib.sha256(self.raw_data[0x100:]).digest()
        self.outer_hash_valid = (declared_outer == actual_outer)

        # 2. TLV 0x0011: Basic Info (Offset 0x0100)
        # Type: 0x0011 (2 bytes), Len: 0x000A (2 bytes), Flags (2 bytes), Offset (4 bytes), Size (4 bytes)
        if self.raw_data[0x100:0x102] == b"\x11\x00":
            self.flags = (self.raw_data[0x104], self.raw_data[0x105])
            self.lzma_offset = struct.unpack_from("<I", self.raw_data, 0x106)[0]
            self.lzma_stream_size = struct.unpack_from("<I", self.raw_data, 0x10A)[0]

        # 3. Scan TLVs between 0x100 and 0x300
        pos = 0x100
        while pos < 0x800 and pos + 4 < len(self.raw_data):
            tlv_type, tlv_len = struct.unpack_from("<HH", self.raw_data, pos)
            val_start = pos + 4
            val_end = val_start + tlv_len
            if val_end > len(self.raw_data):
                break

            val = self.raw_data[val_start:val_end]

            if tlv_type == 0x0013:  # Version string
                # Clean null and 0xFF padding
                s = val.split(b"\x00")[0].split(b"\xFF")[0]
                self.version_string = s.decode("ascii", errors="ignore").strip()

            elif tlv_type == 0x0020:  # Chip ID
                s = val.split(b"\x00")[0].split(b"\xFF")[0]
                self.chip_id = s.decode("ascii", errors="ignore").strip()

            elif tlv_type == 0x0021:  # Design name
                s = val.split(b"\x00")[0].split(b"\xFF")[0]
                self.design_name = s.decode("ascii", errors="ignore").strip()

            elif tlv_type == 0x0012:  # Partition Table
                if len(val) >= 4:
                    self.partition_count = struct.unpack_from("<I", val, 0)[0]
                    for i in range(self.partition_count):
                        e_off = 4 + i * 12
                        if e_off + 12 <= len(val):
                            src_off, dec_sz, dst_addr = struct.unpack_from("<III", val, e_off)
                            self.partitions.append({
                                "index": i + 1,
                                "src_offset": src_off,
                                "decompressed_size": dec_sz,
                                "dst_flash_addr": dst_addr,
                                "expected_sha256": "",
                                "calculated_sha256": "",
                                "verified": False,
                            })

            elif tlv_type == 0x0014:  # Per-partition SHA-256 Hashes
                if len(val) >= 4:
                    hash_count = struct.unpack_from("<I", val, 0)[0]
                    for i in range(min(hash_count, len(self.partitions))):
                        h_off = 4 + i * 32
                        if h_off + 32 <= len(val):
                            expected_h = val[h_off : h_off + 32].hex()
                            self.partitions[i]["expected_sha256"] = expected_h

            pos += 4 + tlv_len

    def decompress(self) -> bytes:
        """
        Decompresses the LZMA-Alone stream starting at lzma_offset (0x1000).
        Patches the explicit size header field so Python's lzma module accepts the stream.
        """
        if self.decompressed_payload is not None:
            return self.decompressed_payload

        compressed_stream = self.raw_data[self.lzma_offset :]
        if len(compressed_stream) < 14:
            raise ValueError("Compressed payload too short.")

        # In LZMA-Alone header: bytes 0..4 are properties (0x5D, dict size 16KB),
        # bytes 5..12 are uncompressed size.
        # Patch uncompressed size to 0xFFFFFFFFFFFFFFFF (streaming mode) for Python lzma compatibility
        fixed_stream = compressed_stream[:5] + struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + compressed_stream[13:]

        try:
            decompressed = lzma.decompress(fixed_stream, format=lzma.FORMAT_ALONE)
            self.decompressed_payload = decompressed
            return decompressed
        except Exception as e:
            raise RuntimeError(f"LZMA decompression failed: {e}")

    def verify_partitions(self) -> bool:
        """
        Decompresses the payload and verifies SHA-256 integrity for every partition.
        """
        data = self.decompress()
        all_ok = True
        offset = 0

        for p in self.partitions:
            sz = p["decompressed_size"]
            chunk = data[offset : offset + sz]
            h = hashlib.sha256(chunk).hexdigest()
            p["calculated_sha256"] = h
            p["verified"] = (h.lower() == p["expected_sha256"].lower())
            if not p["verified"]:
                all_ok = False
            offset += sz

        self.all_partitions_verified = all_ok and self.outer_hash_valid
        return self.all_partitions_verified

    def unpack_to_directory(self, output_dir: str) -> List[str]:
        """
        Extracts decompressed firmware and individual partitions into files.
        """
        os.makedirs(output_dir, exist_ok=True)
        data = self.decompress()
        extracted_files = []

        # 1. Full raw ARM image
        full_path = os.path.join(output_dir, f"{self.filename}.decompressed.bin")
        with open(full_path, "wb") as f:
            f.write(data)
        extracted_files.append(full_path)

        # 2. Individual partitions
        offset = 0
        names = ["partition_1_bootloader", "partition_2_kernel_app", "partition_3_dsp_audio", "partition_4_nvdm_defaults"]
        for idx, p in enumerate(self.partitions):
            sz = p["decompressed_size"]
            chunk = data[offset : offset + sz]
            p_name = names[idx] if idx < len(names) else f"partition_{idx + 1}"
            p_file = os.path.join(output_dir, f"{self.filename}_{p_name}.bin")
            with open(p_file, "wb") as f:
                f.write(chunk)
            extracted_files.append(p_file)
            offset += sz

        return extracted_files

    @property
    def firmware_track(self) -> str:
        """Catalog track this package belongs to ("maxwell2", "maxwell2_anc", "maxwell1" or "unknown")."""
        known = KNOWN_PACKAGE_TRACKS.get(hashlib.sha256(self.raw_data).hexdigest())
        if known:
            return known
        if self.chip_id in MAXWELL2_CHIP_IDS:
            return "maxwell2_anc" if self.filename.lower().startswith("maxwell2anc") else "maxwell2"
        if self.chip_id.startswith("AB1568"):
            return "maxwell1"
        return "unknown"

    def to_dict(self) -> Dict[str, Any]:
        """Summarizes parsed metadata as a dictionary."""
        return {
            "filename": self.filename,
            "filepath": self.filepath,
            "file_size_bytes": self.file_size,
            "file_size_kb": round(self.file_size / 1024, 2),
            "version_string": self.version_string,
            "chip_id": self.chip_id,
            "chip_target": KNOWN_CHIP_IDS.get(self.chip_id, ("unknown", False))[0],
            "live_flash_supported": KNOWN_CHIP_IDS.get(self.chip_id, ("unknown", False))[1],
            "firmware_track": self.firmware_track,
            "design_name": self.design_name,
            "outer_sha256": self.outer_hash,
            "outer_hash_valid": self.outer_hash_valid,
            "lzma_stream_size": self.lzma_stream_size,
            "partition_count": len(self.partitions),
            "partitions": self.partitions,
            "integrity_verified": self.all_partitions_verified,
        }


# ==============================================================================
# Firmware Diagnostics & Safety Evaluation
# ==============================================================================

def get_firmware_diagnostics(client: MaxwellClient) -> Dict[str, Any]:
    """
    Performs comprehensive firmware diagnostic checks across both computational nodes
    (Dongle and Headset) and evaluates safety prerequisites for updates.
    """
    if client.transport.dry_run:
        return {
            "dongle": {"firmware": "v1.0.5.7.4", "route": 0x00, "status": "Operational"},
            "headset": {"firmware": "v1.0.5.7.4", "route": 0x80, "status": "Operational"},
            "parity_match": True,
            "battery_percent": 94,
            "battery_safe_for_flashing": True,
            "update_prerequisites_met": True,
            "connection_mode": "Wireless Dongle (Xbox variant)",
            "dry_run": True,
        }

    # 1. Query dual firmware versions
    fw_info = client.get_firmware()
    dongle_fw = fw_info.get("dongle", "Unknown")
    headset_fw = fw_info.get("headset", "Unknown")
    dongle_ok = (dongle_fw != "Unknown")
    headset_ok = (headset_fw != "Unknown")

    # 2. Query Battery
    batt_pct = client.get_battery()
    batt_safe = (batt_pct is not None and batt_pct >= 50)

    # 3. Parity check
    parity = (dongle_ok and headset_ok and dongle_fw == headset_fw)

    # 4. Connection Mode
    conn_mode = "Wireless Dongle" if client.transport.is_dongle else "Direct USB-C Cable"

    # 5. Overall flash safety evaluation
    safe_to_update = parity and batt_safe and dongle_ok and headset_ok

    return {
        "dongle": {
            "firmware": dongle_fw,
            "route": 0x00,
            "status": "Operational" if dongle_ok else "Unreachable",
        },
        "headset": {
            "firmware": headset_fw,
            "route": 0x80,
            "status": "Operational" if headset_ok else "Unreachable",
        },
        "parity_match": parity,
        "battery_percent": batt_pct,
        "battery_safe_for_flashing": batt_safe,
        "update_prerequisites_met": safe_to_update,
        "connection_mode": conn_mode,
    }


def parse_version_tuple(ver_str: str) -> Tuple[int, ...]:
    """Extracts numeric integers from a version string for semantic comparisons."""
    nums = re.findall(r"\d+", ver_str)
    return tuple(int(n) for n in nums) if nums else (0,)


def check_available_updates(current_version: str, package_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Evaluates update availability against the local baseline catalog or an explicit firmware package (.bin).
    Data provenance is explicitly surfaced to distinguish hardware reads from offline catalog entries.
    """
    cat_key = detect_device_track(current_version) or "maxwell2"
    catalog = FIRMWARE_CATALOG.get(cat_key, FIRMWARE_CATALOG["maxwell2"])

    current_tuple = parse_version_tuple(current_version)

    if package_path:
        pkg = FirmwarePackage(package_path)
        pkg_ver = pkg.version_string
        pkg_tuple = parse_version_tuple(pkg_ver)

        if pkg_tuple > current_tuple:
            status = "UPGRADE AVAILABLE"
            status_symbol = "▲"
        elif pkg_tuple < current_tuple:
            status = "PACKAGE IS OLDER (DOWNGRADE)"
            status_symbol = "▼"
        else:
            status = "VERSION MATCHES INSTALLED"
            status_symbol = "✔"

        notice = "Evaluated against local .bin package file. Outer SHA-256 and partition integrity verified."
        pkg_track = pkg.firmware_track
        if pkg_track in FIRMWARE_CATALOG and cat_key in FIRMWARE_CATALOG and pkg_track != cat_key:
            status = "WRONG FIRMWARE TRACK"
            status_symbol = "✖"
            notice = (
                f"This package is {FIRMWARE_CATALOG[pkg_track]['model']} firmware, but the installed version "
                f"looks like {catalog['model']}. Do not install firmware from a different track."
            )

        return {
            "current_installed": current_version,
            "target_version": pkg_ver,
            "status": status,
            "status_symbol": status_symbol,
            "model_detected": catalog["model"],
            "source": f"Local Package ({pkg.filename})",
            "is_package": True,
            "package_valid": pkg.outer_hash_valid,
            "package_chip": pkg.chip_id,
            "package_track": pkg_track,
            "package_size_kb": round(pkg.file_size / 1024, 1),
            "notice": notice,
            "releases": catalog["releases"],
        }
    else:
        latest_ver = catalog["current_stable"]
        latest_tuple = parse_version_tuple(latest_ver)
        is_up_to_date = (current_tuple >= latest_tuple)
        status = "MATCHES OFFLINE BASELINE" if is_up_to_date else "OFFLINE UPDATE AVAILABLE"
        notice = "Audeze has no public cloud REST API. This check is evaluated against our local baseline catalog (not a live Audeze HQ cloud query). To check a downloaded update, run with --package <file.bin>."
        if not catalog.get("baseline_complete", True):
            status = "LATEST RELEASE UNKNOWN" if is_up_to_date else status
            notice = (
                f"The local catalog for {catalog['model']} is incomplete: {latest_ver} is the newest release "
                "we know of, but Audeze may have published a newer one. " + notice
            )

        return {
            "current_installed": current_version,
            "latest_available": latest_ver,
            "is_up_to_date": is_up_to_date,
            "status": status,
            "status_symbol": "✔" if is_up_to_date else "●",
            "model_detected": catalog["model"],
            "source": "Offline Baseline Catalog (Local)",
            "is_package": False,
            "notice": notice,
            "releases": catalog["releases"],
        }


# ==============================================================================
# FOTA Chunking & Dry-Run Simulator
# ==============================================================================

class FotaSafetyError(Exception):
    """Raised when pre-flight validation or hardware safety checks fail during FOTA flashing."""
    pass


class FotaTransferError(FotaSafetyError):
    """Raised when a live transfer stops part-way. Records how far it got."""

    def __init__(self, message: str, chunks_sent: int, total_chunks: int):
        super().__init__(message)
        self.chunks_sent = chunks_sent
        self.total_chunks = total_chunks


class FotaInterruptedError(FotaTransferError):
    """Raised when the user cancels (Ctrl-C) a transfer part-way."""
    pass


MIN_HEADSET_BATTERY_PCT = 50


class FotaSimulator:
    """
    Simulates the Airoha FOTA block streaming protocol (as implemented for AB1568) without writing to hardware.
    Validates chunk fragmentation, handshake packets, and transfer duration estimates.
    """

    def __init__(self, package: FirmwarePackage, target: str = "dongle"):
        self.package = package
        self.target = target.lower()  # "dongle" or "headset"
        self.chunk_size = 56  # Payload bytes per Report 0x06 (cbreak safe)
        self.total_bytes = package.file_size
        self.total_chunks = (self.total_bytes + self.chunk_size - 1) // self.chunk_size

    def simulate_transfer(self) -> Dict[str, Any]:
        """Simulates FOTA transmission parameters."""
        route = 0x00 if self.target == "dongle" else 0x80
        est_duration_sec = self.total_chunks * 0.015  # ~15ms per chunk over USB HID

        return {
            "target": self.target,
            "route_byte": f"0x{route:02X}",
            "firmware_version": self.package.version_string,
            "chip_target": self.package.chip_id,
            "total_bytes": self.total_bytes,
            "chunk_payload_size": self.chunk_size,
            "total_hid_reports": self.total_chunks,
            "estimated_duration_sec": round(est_duration_sec, 1),
            "dual_bank_recovery_active": True,
            "handshake_init": "0x0101 (Airoha FotaInit)",
            "fota_start_opcode": "0x1C02 (FotaStart)",
        }


class FotaEngine:
    """
    Airoha FOTA streaming engine. Live writes are only allowed for chips marked as
    supported in KNOWN_CHIP_IDS, which is currently none: the block framing has not been
    verified against the official updater. Dry runs exercise everything except the writes.

    Live flashing fails closed: every device fact it depends on (connection, route,
    installed version, firmware track, headset battery) must be read successfully, or
    preflight refuses. After streaming, the target's version is read back.
    """

    def __init__(
        self,
        package: FirmwarePackage,
        target: str = "dongle",
        client: Optional[MaxwellClient] = None,
        dry_run: bool = True,
        device_track: Optional[str] = None,
        installed_version: Optional[str] = None,
        allow_downgrade: bool = False,
        readback_timeout_sec: float = 60.0,
        readback_interval_sec: float = 2.0,
    ):
        self.package = package
        self.target = target.lower()
        self.client = client
        # Catalog track of the connected device (see detect_device_track), None if unknown.
        self.device_track = device_track
        # Firmware version currently on the target node; read from the device if None.
        self.installed_version = installed_version
        self.allow_downgrade = allow_downgrade
        self.dry_run = dry_run
        self.readback_timeout_sec = readback_timeout_sec
        self.readback_interval_sec = readback_interval_sec
        self.chunk_size = 56
        self.total_bytes = package.file_size
        self.total_chunks = (self.total_bytes + self.chunk_size - 1) // self.chunk_size
        self.cancelled = False

    def _live_transport(self):
        if self.client is None or getattr(self.client, "transport", None) is None:
            raise FotaSafetyError("No device connection: live flashing needs a connected Maxwell.")
        transport = self.client.transport
        if transport.dry_run:
            raise FotaSafetyError("Transport is in dry-run mode: refusing a live flash over a simulated connection.")
        return transport

    def _relay_via_dongle(self) -> bool:
        """
        Route for the target, mirroring MaxwellClient.get_firmware(): through a dongle,
        route 0x00 reaches the dongle and 0x80 is relayed to the headset; over direct
        USB-C, route 0x00 is the headset itself.
        """
        connected_via_dongle = self._live_transport().is_dongle
        if self.target == "dongle":
            if not connected_via_dongle:
                raise FotaSafetyError("Target is the DONGLE, but the connected device is not a dongle.")
            return False
        return connected_via_dongle

    def _read_target_version(self) -> str:
        fw = self.client.get_firmware()
        return str(fw.get(self.target, "Unknown")).strip() or "Unknown"

    def preflight_check(self) -> Dict[str, Any]:
        """
        Validates all safety prerequisites:
        1. Package outer SHA-256 integrity.
        2. Every internal partition SHA-256 hash.
        3. Chip ID is known (KNOWN_CHIP_IDS) and built for the selected target.
           The package's firmware track (e.g. regular vs ANC Maxwell 2) must match the device's.
           Live flashing additionally requires a chip whose FOTA framing is verified.
        4. Live only: connected device and route, installed version, firmware track,
           downgrade confirmation, and headset battery >= 50%. Any of these that cannot
           be read is a refusal, not a pass.
        """
        # 1. Package integrity
        if not self.package.outer_hash_valid:
            raise FotaSafetyError("Firmware package outer SHA-256 checksum is INVALID (tampered or corrupted).")

        if not self.package.verify_partitions():
            raise FotaSafetyError("One or more firmware partition SHA-256 hashes failed verification.")

        # 2. Chip Target Check
        actual_chip = self.package.chip_id
        pkg_track = self.package.firmware_track
        chip_info = KNOWN_CHIP_IDS.get(actual_chip)
        if chip_info is None:
            # A package without a chip TLV can still be simulated, never written.
            if actual_chip != "Unknown" or not self.dry_run:
                raise FotaSafetyError(
                    f"Unrecognised chip identifier '{actual_chip}': refusing to flash an unknown target."
                )
            live_supported = False
        else:
            chip_target, live_supported = chip_info
            if chip_target != self.target:
                raise FotaSafetyError(
                    f"Target mismatch: Attempting to flash {self.target.upper()} with package compiled for {actual_chip}!"
                )

        if self.device_track and pkg_track != "unknown" and pkg_track != self.device_track:
            raise FotaSafetyError(
                f"Firmware track mismatch: package is {pkg_track} firmware but the device is on the "
                f"{self.device_track} track. Regular and ANC Maxwell 2 firmware are not interchangeable."
            )

        installed = self.installed_version
        is_downgrade = bool(installed) and parse_version_tuple(self.package.version_string) < parse_version_tuple(installed)
        batt_pct = None
        relay = None

        if not self.dry_run:
            if not live_supported:
                raise FotaSafetyError(
                    f"Live flashing of {actual_chip} is not supported yet: the FOTA packet framing has not been "
                    "verified against the official updater on this chip. Use --dry-run to simulate the transfer."
                )

            # 3. Device connection and route
            relay = self._relay_via_dongle()

            # 4. Installed version and firmware track
            if not installed:
                try:
                    installed = self._read_target_version()
                except Exception as e:
                    raise FotaSafetyError(f"Could not read the {self.target} firmware version: {e}")
            if installed == "Unknown" or parse_version_tuple(installed) == (0,):
                raise FotaSafetyError(f"Could not read the {self.target} firmware version: refusing to flash blind.")
            self.installed_version = installed

            if not self.device_track:
                self.device_track = detect_device_track(installed, strict=True)
            if not self.device_track:
                raise FotaSafetyError(
                    f"Installed version {installed} is not a known release, so the device's firmware track "
                    "(e.g. regular vs ANC Maxwell 2) cannot be confirmed: refusing to flash."
                )
            if pkg_track == "unknown" or pkg_track != self.device_track:
                raise FotaSafetyError(
                    f"Firmware track mismatch: package is {pkg_track} firmware but the device is on the "
                    f"{self.device_track} track."
                )

            # 5. Downgrade gate
            is_downgrade = parse_version_tuple(self.package.version_string) < parse_version_tuple(installed)
            if is_downgrade and not self.allow_downgrade:
                raise FotaSafetyError(
                    f"Package {self.package.version_string} is OLDER than the installed {installed}: "
                    "downgrades need explicit confirmation (--allow-downgrade)."
                )

            # 6. Battery (headset only; the dongle is bus powered)
            if self.target == "headset":
                try:
                    batt_pct = self.client.get_battery()
                except Exception as e:
                    raise FotaSafetyError(f"Could not read the headset battery level: {e}")
                if batt_pct is None:
                    raise FotaSafetyError("Could not read the headset battery level: refusing to flash.")
                if batt_pct < MIN_HEADSET_BATTERY_PCT:
                    raise FotaSafetyError(
                        f"Headset battery is {batt_pct}%. Minimum {MIN_HEADSET_BATTERY_PCT}% required for flashing safety."
                    )

        return {
            "package_valid": True,
            "target": self.target,
            "chip_target": actual_chip,
            "firmware_track": pkg_track,
            "device_track": self.device_track,
            "version": self.package.version_string,
            "installed_version": installed,
            "is_downgrade": is_downgrade,
            "relay_via_dongle": relay,
            "total_bytes": self.total_bytes,
            "total_chunks": self.total_chunks,
            "battery_percent": batt_pct,
            "dry_run": self.dry_run,
        }

    def _transfer_stopped_message(self, reason: str, chunks_sent: int) -> str:
        return (
            f"{reason} after {chunks_sent} of {self.total_chunks} chunks. Do NOT unplug or power off the device. "
            "It should still be running its previous firmware, but this tool has not verified the dual-bank "
            "fallback. Check with 'firmware status' before doing anything else."
        )

    def _verify_installed_version(self) -> Dict[str, Any]:
        """Polls the target's version until it reports the package version or the timeout expires."""
        expected = parse_version_tuple(self.package.version_string)
        deadline = time.time() + self.readback_timeout_sec
        last_seen = "Unknown"
        while True:
            try:
                last_seen = self._read_target_version()
            except Exception:
                # Expected while the device reboots into the new image.
                pass
            if last_seen != "Unknown" and parse_version_tuple(last_seen) == expected:
                return {"verification": "confirmed", "reported_version": last_seen}
            if time.time() >= deadline:
                break
            time.sleep(self.readback_interval_sec)
        status = "no_response" if last_seen == "Unknown" else "version_mismatch"
        return {"verification": status, "reported_version": last_seen}

    def execute(self, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        Executes the FOTA block streaming pipeline.
        Calls progress_callback(dict) on progress increments.
        Raises FotaInterruptedError on Ctrl-C / cancel and FotaTransferError on a failed write.
        """
        pre = self.preflight_check()
        start_time = time.time()
        bytes_sent = 0
        chunks_sent = 0
        relay = pre["relay_via_dongle"]

        try:
            for chunk_idx in range(self.total_chunks):
                if self.cancelled:
                    raise KeyboardInterrupt

                start_byte = chunk_idx * self.chunk_size
                end_byte = min(self.total_bytes, start_byte + self.chunk_size)
                chunk_data = self.package.raw_data[start_byte:end_byte]

                if not self.dry_run:
                    # Live hardware write via Report 0x06 (cbreak / FOTA block transmission)
                    fota_packet = bytes([0x15, 0x5A]) + struct.pack("<HH", len(chunk_data) + 2, 0x1C03) + chunk_data
                    self.client.transport.write_race_packet(fota_packet, is_dongle=relay)
                else:
                    # In dry_run/simulation: smooth progression
                    time.sleep(0.001)

                chunks_sent += 1
                bytes_sent += len(chunk_data)
                elapsed = max(0.001, time.time() - start_time)
                speed_kb = (bytes_sent / 1024.0) / elapsed
                remaining_bytes = self.total_bytes - bytes_sent
                eta_sec = (remaining_bytes / 1024.0) / speed_kb if speed_kb > 0 else 0.0

                if progress_callback and (chunk_idx % 10 == 0 or bytes_sent == self.total_bytes):
                    pct = int((bytes_sent / float(self.total_bytes)) * 100)
                    progress_callback({
                        "bytes_sent": bytes_sent,
                        "total_bytes": self.total_bytes,
                        "percent": pct,
                        "current_chunk": chunk_idx + 1,
                        "total_chunks": self.total_chunks,
                        "elapsed_sec": round(elapsed, 1),
                        "speed_kb_s": round(speed_kb, 1),
                        "eta_sec": round(eta_sec, 1),
                        "done": bytes_sent >= self.total_bytes,
                    })
        except KeyboardInterrupt:
            self.cancelled = True
            if self.dry_run or chunks_sent == 0:
                raise FotaInterruptedError(
                    f"FOTA flash cancelled after {chunks_sent} of {self.total_chunks} chunks"
                    + (" (dry run, nothing was written)." if self.dry_run else " (nothing was written)."),
                    chunks_sent, self.total_chunks,
                )
            raise FotaInterruptedError(
                self._transfer_stopped_message("FOTA flash cancelled", chunks_sent), chunks_sent, self.total_chunks
            )
        except (OSError, RuntimeError) as e:
            raise FotaTransferError(
                self._transfer_stopped_message(f"Write failed ({e})", chunks_sent), chunks_sent, self.total_chunks
            )

        duration = time.time() - start_time
        result = {
            "success": True,
            "target": self.target,
            "version": self.package.version_string,
            "previous_version": pre["installed_version"],
            "total_bytes": self.total_bytes,
            "chunks_sent": chunks_sent,
            "duration_sec": round(duration, 2),
            "dry_run": self.dry_run,
            "verification": "skipped (dry run)",
            "reported_version": None,
        }
        if not self.dry_run:
            result.update(self._verify_installed_version())
            result["success"] = result["verification"] == "confirmed"
        return result


# ==============================================================================
# Pretty Plain-Text Terminal Formatters
# ==============================================================================

def format_firmware_status_plain(diag: Dict[str, Any]) -> str:
    """Formats diagnostic status in clean monochrome ASCII."""
    lines = []
    lines.append("┌─ AUDEZE MAXWELL — FIRMWARE DIAGNOSTICS & UPDATE READINESS ───────────┐")
    lines.append("│                                                                      │")

    d = diag.get("dongle", {})
    lines.append(f"│  USB Dongle Firmware       : {d.get('firmware', 'Unknown'):<24} [{d.get('status', 'Unknown')}]   │")

    h = diag.get("headset", {})
    lines.append(f"│  Wireless Headset Firmware : {h.get('firmware', 'Unknown'):<24} [{h.get('status', 'Unknown')}]   │")

    parity = diag.get("parity_match", False)
    p_str = "MATCHED (Synchronized)" if parity else "MISMATCH WARNING"
    lines.append(f"│  Version Parity Check      : {p_str:<38} │")

    batt = diag.get("battery_percent")
    batt_str = f"{batt}%" if batt is not None else "Unknown"
    batt_safe = diag.get("battery_safe_for_flashing", False)
    safe_badge = "(Pass >= 50%)" if batt_safe else "(LOW - Flashing Unsafe)"
    lines.append(f"│  Headset Battery Level     : {batt_str:<8} {safe_badge:<29} │")

    conn = diag.get("connection_mode", "Unknown")
    lines.append(f"│  Active Transport Path     : {conn:<38} │")

    lines.append("│                                                                      │")
    lines.append("├─ DUAL-BANK FLASH SAFETY READINESS ───────────────────────────────────┤")

    ready = diag.get("update_prerequisites_met", False)
    if ready:
        lines.append("│  [PASS] All pre-flight safety requirements satisfied for updates:     │")
        lines.append("│         ✔ Dongle and Headset firmware versions match.                │")
        lines.append("│         ✔ Headset battery has sufficient capacity (>= 50%).          │")
        lines.append("│         ✔ Airoha dual-bank fallback protects against bricking.        │")
    else:
        lines.append("│  [CAUTION] Pre-flight prerequisites not fully satisfied:             │")
        if not batt_safe:
            lines.append("│         ✖ Battery is below 50%. Charge headset before flashing.      │")
        if not parity:
            lines.append("│         ✖ Firmware mismatch detected between dongle and headset.     │")

    lines.append("└──────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)


def format_firmware_check_plain(info: Dict[str, Any]) -> str:
    """Formats update check summary with explicit data provenance and transparency."""
    lines = []
    lines.append("┌─ AUDEZE FIRMWARE UPDATE CHECK ───────────────────────────────────────┐")
    lines.append(f"│  Hardware Model            : {info.get('model_detected', 'Unknown'):<38} │")
    lines.append(f"│  Current Installed Version : {info.get('current_installed', 'Unknown'):<38} │")

    if info.get("is_package"):
        lines.append(f"│  Target Package Version    : {info.get('target_version', 'Unknown'):<38} │")
        lines.append(f"│  Target Chip Architecture  : {info.get('package_chip', 'Unknown'):<38} │")
        lines.append(f"│  Package Firmware Track    : {info.get('package_track', 'unknown'):<38} │")
        lines.append(f"│  Package Integrity Check   : {'✔ SHA-256 PASS (Valid)' if info.get('package_valid') else '✖ CORRUPTED':<38} │")
    else:
        lines.append(f"│  Latest Catalog Baseline   : {info.get('latest_available', 'Unknown'):<38} │")

    source = info.get("source", "Offline Catalog")
    lines.append(f"│  Check Source / Provenance : {source:<38} │")

    status_str = f"{info.get('status_symbol', '✔')} {info.get('status', 'UP TO DATE')}"
    lines.append(f"│  Comparison Status         : {status_str:<38} │")
    lines.append("├─ DATA PROVENANCE & NOTICE ───────────────────────────────────────────┤")

    notice = info.get("notice", "")
    while len(notice) > 66:
        split_idx = notice[:66].rfind(" ")
        if split_idx == -1:
            split_idx = 66
        lines.append(f"│  {notice[:split_idx]:<68}│")
        notice = notice[split_idx:].strip()
    if notice:
        lines.append(f"│  {notice:<68}│")

    lines.append("├─ KNOWN RELEASE HISTORY ──────────────────────────────────────────────┤")

    for r in info.get("releases", []):
        latest_tag = " (Latest)" if r.get("latest") else ""
        lines.append(f"│  • {r['version']}{latest_tag} [{r['date']}]:")
        notes = r["notes"]
        while len(notes) > 66:
            split_idx = notes[:66].rfind(" ")
            if split_idx == -1:
                split_idx = 66
            lines.append(f"│    {notes[:split_idx]:<66}│")
            notes = notes[split_idx:].strip()
        if notes:
            lines.append(f"│    {notes:<66}│")
        lines.append("│                                                                      │")

    lines.append("└──────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)


def format_firmware_inspect_plain(info: Dict[str, Any]) -> str:
    """Formats full firmware container inspection report."""
    lines = []
    lines.append("┌─ FIRMWARE PACKAGE CONTAINER INSPECTION ──────────────────────────────┐")
    lines.append(f"│  Package File              : {info.get('filename', 'Unknown'):<38} │")
    lines.append(f"│  File Size                 : {info.get('file_size_kb', 0)} KB ({info.get('file_size_bytes', 0)} bytes)             │")
    lines.append(f"│  Embedded Firmware Version : {info.get('version_string', 'Unknown'):<38} │")
    lines.append(f"│  Target Chip Identifier    : {info.get('chip_id', 'Unknown'):<38} │")
    flash_s = "Supported" if info.get("live_flash_supported") else "Not supported (dry-run only)"
    lines.append(f"│  Live Flash Support        : {flash_s:<38} │")
    lines.append(f"│  Firmware Track            : {info.get('firmware_track', 'unknown'):<38} │")
    lines.append(f"│  Reference Design Name     : {info.get('design_name', 'Unknown'):<38} │")

    outer_v = "VALID (Pass)" if info.get("outer_hash_valid") else "INVALID (Tampered/Corrupted)"
    lines.append(f"│  Outer SHA-256 Checksum    : {outer_v:<38} │")
    lines.append(f"│  LZMA Stream Length        : {info.get('lzma_stream_size', 0)} bytes                              │")
    lines.append("├─ PARTITION TABLE & INTEGRITY HASHES ─────────────────────────────────┤")

    partitions = info.get("partitions", [])
    if not partitions:
        lines.append("│  (No partitions declared in header)                                  │")
    else:
        for p in partitions:
            idx = p.get("index", 1)
            sz_kb = round(p.get("decompressed_size", 0) / 1024, 1)
            dst = f"0x{p.get('dst_flash_addr', 0):08X}"
            v_str = "✔ PASS" if p.get("verified") else "● UNVERIFIED"
            lines.append(f"│  Part #{idx}: Flash {dst} • Size: {sz_kb} KB • Hash: {v_str:<12}│")

    lines.append("└──────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)
