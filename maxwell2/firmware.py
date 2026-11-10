"""
audezeforlinux: Firmware diagnostics, verification, package inspection, and FOTA subsystem.
Implements dual-device firmware version queries, parity verification, pre-flight safety gates,
offline TLV/LZMA container parsing, partition hash verification, and FOTA streaming simulation.
"""

import os
import sys
import json
import struct
import urllib.request
import hashlib
import binascii
import re
import lzma
import time
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Tuple, Callable

from maxwell2.backup import create_backup
from maxwell2.client import MaxwellClient
from maxwell2.transport import ANC_PIDS, DeviceAbortError, DeviceBusyError, list_maxwell_devices
from maxwell2.protocol import (
    build_firmware_command,
    parse_firmware_notification,
    CMD_FIRMWARE,
    CHUNK_MAX,
    ProtocolError,
)
from maxwell2.fota import (
    CMD_FOTA_STOP,
    FOTA_STATE_START_1,
    FOTA_STATE_START_2,
    FOTA_STATE_DOWNLOADED,
    MAX_PAGES_PER_WRITE,
    PAGE_SIZE,
    build_commit,
    build_fota_start,
    build_integrity_check,
    build_page_program,
    build_page_read,
    build_partition_erase,
    build_partition_info_query,
    build_start_transaction,
    build_write_state,
    pad_to_pages,
    parse_page_program,
    parse_page_read,
    parse_partition_info,
    parse_status_response,
    ranges_overlap,
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


# TLV 0x0020 chip identifiers -> (flash target, live FOTA flashing verified).
# AB1565_ULL2_DONGLE / AB1585_Headset are what the official Maxwell 2 packages
# (v1.0.5.7.4 and the v1.0.2.60.2 rollback) report. None is marked verified until a complete
# flash has been confirmed on hardware; the Maxwell 2 chips can be flashed with
# experimental=True (see FOTA_EXPERIMENTAL_CHIPS). The AB1568 (Maxwell 1) is untested.
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
    "6eee723fda209d91744381a2b1f6c41b3df4d6e045f6038e7766b5d450b20259": "maxwell2",  # Maxwell2_XBOX_dongle_v1.0.5.7.4.bin
    "a3c3d54183628dee68655aef204ece820b49fed6f8aaaffbd3407672b01afe4a": "maxwell2",  # Maxwell2_XBOX_headset_v1.0.5.7.4.bin
}


# Audeze's update manifests, the ones the official Windows app reads. Only the regular Maxwell 2 track
# has a known current one: the ANC bucket only exposes a rollback list (see MAXWELL2_FIRMWARE_NOTES.md).
OFFICIAL_BUCKET = "https://audeze-maxwell2.s3.us-east-1.amazonaws.com/"
OFFICIAL_MANIFESTS = {"maxwell2": OFFICIAL_BUCKET + "maxwell-2-fw.json"}
MANIFEST_PACKAGE_KEYS = ("ps_dongle", "ps_headset", "xbox_dongle", "xbox_headset")
MAXWELL1_PIDS = (0x4B18, 0x4B19, 0x4B1A, 0x4B1E)


def track_from_pid(pid: Optional[int]) -> Optional[str]:
    """Catalog track the USB product id proves, or None when the PID alone can't tell (Maxwell 2 USB-C
    ids are not known to be ANC-free, so they fall back to the version)."""
    if pid in ANC_PIDS:
        return "maxwell2_anc"
    if pid in MAXWELL1_PIDS:
        return "maxwell1"
    return None


def parse_official_manifest(data: Any) -> Dict[str, Any]:
    """Validates a downloaded manifest: a version string, plus package links only inside Audeze's bucket."""
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"v?\d+(\.\d+)+(_\w+)?", version.strip()):
        raise ValueError("manifest has no valid version")
    packages = {k: data[k] for k in MANIFEST_PACKAGE_KEYS
                if isinstance(data.get(k), str) and data[k].startswith(OFFICIAL_BUCKET)}
    return {"version": version.strip(), "packages": packages}


def fetch_official_release(track: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Reads the latest release from Audeze's manifest. Raises OSError (network) or ValueError (bad data)."""
    req = urllib.request.Request(OFFICIAL_MANIFESTS[track], headers={"User-Agent": "audezeforlinux"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_official_manifest(json.loads(resp.read(64 * 1024)))


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
        # Roles are only known for the 4-partition AB1568 (Maxwell 1) layout; other packages get
        # neutral names with their flash address (e.g. Maxwell 2 partition 1 holds DSP code).
        ab1568_names = ["partition_1_bootloader", "partition_2_kernel_app", "partition_3_dsp_audio",
                        "partition_4_nvdm_defaults"]
        known_roles = self.chip_id.startswith("AB1568") and len(self.partitions) == len(ab1568_names)
        for idx, p in enumerate(self.partitions):
            sz = p["decompressed_size"]
            chunk = data[offset : offset + sz]
            p_name = ab1568_names[idx] if known_roles else f"partition_{idx + 1}_0x{p['dst_flash_addr']:08X}"
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
    usb_powered = headset_usb_powered(client)
    batt_needed = required_battery_pct(usb_powered)
    batt_safe = (batt_pct is not None and batt_pct >= batt_needed)

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
        "battery_required_percent": batt_needed,
        "usb_powered": usb_powered,
        "update_prerequisites_met": safe_to_update,
        "connection_mode": conn_mode,
    }


def parse_version_tuple(ver_str: str) -> Tuple[int, ...]:
    """Extracts numeric integers from a version string for semantic comparisons."""
    nums = re.findall(r"\d+", ver_str)
    return tuple(int(n) for n in nums) if nums else (0,)


def check_available_updates(current_version: str, package_path: Optional[str] = None,
                            online: bool = False, track: Optional[str] = None) -> Dict[str, Any]:
    """
    Evaluates update availability against Audeze's live manifest (online=True, falling back to the local
    catalog when it can't be read), the local baseline catalog, or an explicit firmware package (.bin).
    track (e.g. from track_from_pid) overrides the guess from the version string.
    Data provenance is explicitly surfaced to distinguish hardware reads from offline catalog entries.
    """
    # A failed read ("Unknown") has no digits; it must not be compared, or taken for Maxwell 1 firmware.
    version_known = bool(re.search(r"\d", current_version or ""))
    cat_key = track or (detect_device_track(current_version) if version_known else None) or "maxwell2"
    catalog = FIRMWARE_CATALOG.get(cat_key, FIRMWARE_CATALOG["maxwell2"])

    current_tuple = parse_version_tuple(current_version or "")

    if package_path:
        pkg = FirmwarePackage(package_path)
        pkg_ver = pkg.version_string
        pkg_tuple = parse_version_tuple(pkg_ver)

        if not version_known:
            status = "INSTALLED VERSION UNKNOWN"
            status_symbol = "?"
        elif pkg_tuple > current_tuple:
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
        packages: Dict[str, str] = {}
        live = False
        offline_notice = ("This check is evaluated against our local baseline catalog. "
                          "To check a downloaded update, run with --package <file.bin>.")
        notice = offline_notice
        if online and cat_key in OFFICIAL_MANIFESTS:
            try:
                official = fetch_official_release(cat_key)
                latest_ver, packages, live = official["version"], official["packages"], True
                notice = "Checked against Audeze's update manifest, the list the official app reads."
                if parse_version_tuple(latest_ver) > parse_version_tuple(catalog["current_stable"]):
                    notice += f" {latest_ver} is newer than our local catalog, so its changelog isn't listed below."
            except (OSError, ValueError) as e:
                notice = f"Couldn't read Audeze's update manifest ({e}). " + offline_notice
        elif online:
            notice = f"Audeze publishes no known update manifest for {catalog['model']}. " + offline_notice

        is_up_to_date = (current_tuple >= parse_version_tuple(latest_ver)) if version_known else None
        if is_up_to_date is None:
            status = "INSTALLED VERSION UNKNOWN"
            notice = "The device didn't report its firmware version (is it connected and switched on?). " + notice
        elif live:
            status = "UP TO DATE" if is_up_to_date else "UPDATE AVAILABLE"
        else:
            status = "MATCHES OFFLINE BASELINE" if is_up_to_date else "OFFLINE UPDATE AVAILABLE"
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
            "status_symbol": {True: "✔", False: "●", None: "?"}[is_up_to_date],
            "model_detected": catalog["model"],
            "source": "Audeze Update Server (Live)" if live else "Offline Baseline Catalog (Local)",
            "is_package": False,
            "notice": notice,
            "packages": packages,
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
# While the headset charges over USB, its reading runs high: on a Maxwell 2 it read 85% on the
# charger and settled at 62% a minute after unplugging, and an earlier 34% became 72% within two
# minutes of plugging in. 90% on the charger is roughly 50% real charge.
MIN_HEADSET_BATTERY_PCT_CHARGING = 90
# After plugging in or unplugging, the reading moves about 1 point every 2 s for up to a minute
# before it settles, so preflight waits until two readings this far apart agree.
BATTERY_SETTLE_INTERVAL_SEC = 3.0
BATTERY_SETTLE_TIMEOUT_SEC = 120.0


def required_battery_pct(usb_powered: Optional[bool]) -> int:
    """Minimum headset reading for an update. Unknown power state counts as charging (the stricter case)."""
    return MIN_HEADSET_BATTERY_PCT if usb_powered is False else MIN_HEADSET_BATTERY_PCT_CHARGING


def headset_usb_powered(client) -> Optional[bool]:
    """True if the headset is on USB power (it's reachable as its own USB node), None if unknown."""
    transport = getattr(client, "transport", None)
    if transport is not None and getattr(transport, "is_dongle", True) is False:
        return True  # talking to the headset's own USB node
    check = getattr(client, "is_usb_powered", None)
    if check is None:
        return None
    try:
        return bool(check())
    except Exception:
        return None

# Maxwell 2 chips whose FOTA commands have been confirmed on real hardware with read-only
# requests (partition query, page reads, relay through the dongle), but not yet through a
# complete flash. Live flashing them needs experimental=True (CLI: --experimental).
FOTA_EXPERIMENTAL_CHIPS = ("AB1565_ULL2_DONGLE", "AB1585_Headset")

FOTA_COMMAND_TIMEOUT_SEC = 5.0
FOTA_ERASE_TIMEOUT_SEC = 120.0       # the headset staging area is 3.2 MB of flash
FOTA_INTEGRITY_TIMEOUT_SEC = 60.0
FOTA_PAGE_TIMEOUT_SEC = 3.0
FOTA_PAGE_ATTEMPTS = 4               # one try plus three retries, like race-toolkit
FOTA_POLL_INTERVAL_SEC = 0.0005
FOTA_INSTALL_TIMEOUT_SEC = 600.0     # the bootloader unpacks the image after commit (duration unmeasured)

FOTA_PHASES = ("start", "erase", "write", "verify", "integrity", "commit", "install")


class FotaSimulator:
    """Describes the FOTA plan for a package (steps, pages, HID reports) without touching hardware."""

    def __init__(self, package: FirmwarePackage, target: str = "dongle", pages_per_write: int = 1):
        self.package = package
        self.target = target.lower()  # "dongle" or "headset"
        self.pages_per_write = pages_per_write
        self.total_bytes = package.file_size
        self.total_pages = len(pad_to_pages(bytes(package.raw_data))) // PAGE_SIZE
        self.total_writes = (self.total_pages + pages_per_write - 1) // pages_per_write
        packet_size = 6 + 2 + (5 + PAGE_SIZE) * pages_per_write
        self.reports_per_write = (packet_size + CHUNK_MAX - 1) // CHUNK_MAX

    def simulate_transfer(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "route_byte": "0x00" if self.target == "dongle" else "0x80 through a dongle, 0x00 over USB-C",
            "firmware_version": self.package.version_string,
            "chip_target": self.package.chip_id,
            "total_bytes": self.total_bytes,
            "total_pages": self.total_pages,
            "page_writes": self.total_writes,
            "total_hid_reports": self.total_writes * self.reports_per_write,
            # Measured on a Maxwell 2 headset over USB-C: ~8 ms per page read; writes are similar.
            "estimated_duration_sec": round(self.total_pages * 0.018 + 30, 1),
            "steps": [
                "0x1C00 query FOTA partition", "0x1C08 start", "0x1C0A start transaction",
                "0x1C06 state 0x0201, 0x0210", "0x0404 erase partition", "0x0402 write pages",
                "0x0403 read back every page", "0x1C01 integrity check", "0x1C06 state 0x0211",
                "0x1C02 commit (device reboots and installs)",
            ],
        }


class FotaEngine:
    """
    Airoha FOTA engine (see maxwell2/fota.py for the protocol).

    Live run: preflight -> start session -> erase the staging partition -> write every page ->
    read every page back and compare -> device integrity check -> commit (the device reboots and
    its bootloader installs the image) -> read the version back.

    Nothing is installed before commit: a run that stops earlier, for any reason, leaves the
    device on its current firmware. stage_only stops after the integrity check on purpose.

    Live writes are allowed only for chips verified in KNOWN_CHIP_IDS (currently none), or for
    FOTA_EXPERIMENTAL_CHIPS when experimental=True. Every device fact the flash depends on must
    be read successfully, or preflight refuses.
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
        experimental: bool = False,
        stage_only: bool = False,
        pages_per_write: int = 1,
        readback_timeout_sec: float = FOTA_INSTALL_TIMEOUT_SEC,
        readback_interval_sec: float = 2.0,
    ):
        if not 1 <= pages_per_write <= MAX_PAGES_PER_WRITE:
            raise ValueError(f"pages_per_write must be 1..{MAX_PAGES_PER_WRITE}, got {pages_per_write}")
        self.package = package
        self.target = target.lower()
        self.client = client
        # Catalog track of the connected device (see detect_device_track), None if unknown.
        self.device_track = device_track
        # Firmware version currently on the target node; read from the device if None.
        self.installed_version = installed_version
        self.allow_downgrade = allow_downgrade
        self.experimental = experimental
        self.stage_only = stage_only
        self.pages_per_write = pages_per_write
        self.dry_run = dry_run
        self.readback_timeout_sec = readback_timeout_sec
        self.readback_interval_sec = readback_interval_sec
        self.image = pad_to_pages(bytes(package.raw_data))
        self.total_bytes = package.file_size
        self.total_pages = len(self.image) // PAGE_SIZE
        self.total_chunks = self.total_pages  # one chunk = one 256-byte page
        self.chunk_size = PAGE_SIZE
        self.fota_partition: Optional[Dict[str, int]] = None
        self.relay: Optional[bool] = None
        self.node: Optional[Tuple[Any, Any, Any]] = None  # (hidraw path, product id, is_dongle) at preflight
        self.phase = "preflight"
        self.pages_written = 0
        self.pages_verified = 0
        self.backup_file: Optional[str] = None
        self.cancelled = False
        self.reboot_seen = False  # the device went away after the commit (see _verify_installed_version)
        self.settings_snapshot: Optional[Dict[str, Any]] = None  # dump_all() taken before the update
        self.eq_restore: Optional[Dict[str, Any]] = None

    # --------------------------------------------------------------------------
    # Device access
    # --------------------------------------------------------------------------

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
        transport = self._live_transport()
        connected_via_dongle = transport.is_dongle
        # The route only means something for this exact node, so pin it (see _target_route). A second
        # preflight (execute() runs it again after the CLI's confirmation prompt) must find the same one.
        node = self._node_identity(transport)
        if self.node is not None and node != self.node:
            raise FotaSafetyError(
                f"The connection changed since the checks ran (was {self.node[0]}, now {node[0]}): "
                "the device was unplugged or re-enumerated. Nothing was written; run the update again."
            )
        self.node = node
        if self.target == "dongle":
            if not connected_via_dongle:
                raise FotaSafetyError("Target is the DONGLE, but the connected device is not a dongle.")
            return False
        return connected_via_dongle

    @staticmethod
    def _node_identity(transport) -> Tuple[Any, Any, Any]:
        return (transport.hidraw_path, transport.product_id, transport.is_dongle)

    @contextmanager
    def _target_route(self):
        """
        Holds the transport and points every request at the target node. Refuses if the transport
        is no longer on the node preflight checked: after a reconnect (headset unplugged from USB-C,
        now reached through the dongle) the same route byte reaches a different device, so the
        headset package could otherwise be sent to the dongle. No reconnect is allowed inside.
        """
        transport = self._live_transport()
        with transport.lock:
            if self.node is not None and self._node_identity(transport) != self.node:
                raise FotaSafetyError(
                    f"The connection changed since the checks ran (was {self.node[0]}, now {transport.hidraw_path}): "
                    "the device was unplugged or re-enumerated. Nothing was written; run the update again."
                )
            previous_route, previous_reconnect = transport.is_dongle, transport.auto_reconnect
            transport.is_dongle = self.relay
            transport.auto_reconnect = False
            try:
                yield transport
            finally:
                transport.is_dongle = previous_route
                transport.auto_reconnect = previous_reconnect

    def _command(self, transport, packet: bytes, timeout: Optional[float] = None) -> None:
        cmd = struct.unpack_from("<H", packet, 4)[0]
        resp = transport.exchange(packet, timeout_sec=FOTA_COMMAND_TIMEOUT_SEC if timeout is None else timeout,
                                  abort_cmds=(CMD_FOTA_STOP,))
        parse_status_response(resp, cmd)

    def _open_session(self, transport) -> None:
        """
        Starts (or restarts) the device's update session. The device ends a session that has been
        idle for a while, sending FOTA stop (0x1C03) instead of answering the next update command;
        reading pages back doesn't count as activity. Seen on a Maxwell 2 headset: after the ~70 s
        read-back, the integrity check got a stop. Restarting the session leaves the staged data as
        it is (race-toolkit relies on this to re-check an already staged package).
        """
        for packet in (build_fota_start(), build_start_transaction(),
                       build_write_state(FOTA_STATE_START_1), build_write_state(FOTA_STATE_START_2)):
            if self.cancelled:
                raise KeyboardInterrupt
            self._command(transport, packet)

    def _query_fota_partition(self) -> Dict[str, int]:
        try:
            with self._target_route() as transport:
                return parse_partition_info(transport.exchange(build_partition_info_query(), timeout_sec=FOTA_COMMAND_TIMEOUT_SEC))
        except (ProtocolError, TimeoutError, OSError) as e:
            raise FotaSafetyError(f"Could not read the {self.target}'s FOTA partition: {e}")

    def _settled_battery(self) -> Optional[int]:
        """Reads the battery until two readings BATTERY_SETTLE_INTERVAL_SEC apart agree."""
        deadline = time.time() + BATTERY_SETTLE_TIMEOUT_SEC
        last = self.client.get_battery()
        while last is not None:
            time.sleep(BATTERY_SETTLE_INTERVAL_SEC)
            now = self.client.get_battery()
            if now == last:
                return now
            if time.time() >= deadline:
                raise FotaSafetyError(
                    f"The headset battery reading is still changing ({last}% -> {now}%), as it does for about a "
                    "minute after plugging in or unplugging. Wait a minute and try again."
                )
            last = now
        return None

    def _read_target_version(self) -> str:
        if self.target == "dongle":
            transport = self.client.transport
            if getattr(transport, "is_dongle", True) is False:
                # On the headset's USB node, route 0x00 reaches the headset, and its version could pass
                # for the dongle's (e.g. after the dongle rebooted and the transport picked the headset).
                if not any(d["is_dongle"] for d in list_maxwell_devices()):
                    return "Unknown"
                transport.reconnect(prefer_dongle=True)
                if transport.is_dongle is False:
                    return "Unknown"
        fw = self.client.get_firmware()
        return str(fw.get(self.target, "Unknown")).strip() or "Unknown"

    # --------------------------------------------------------------------------
    # Preflight
    # --------------------------------------------------------------------------

    def preflight_check(self) -> Dict[str, Any]:
        """
        Validates all safety prerequisites:
        1. Package outer SHA-256 integrity.
        2. Every internal partition SHA-256 hash.
        3. Chip ID is known (KNOWN_CHIP_IDS) and built for the selected target.
           The package's firmware track (e.g. regular vs ANC Maxwell 2) must match the device's.
           Live flashing additionally requires a verified chip, or an experimental one with
           experimental=True.
        4. Live only: connected device and route, installed version, firmware track, downgrade
           confirmation, headset battery (a settled reading of >= 50%, or >= 90% while charging,
           since readings on the charger run high), and the device's FOTA partition: readable, big
           enough for the package, and clear of every flash range the package installs to.
           Any of these that cannot be read is a refusal, not a pass.
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

        if not self.dry_run:
            experimental_ok = self.experimental and actual_chip in FOTA_EXPERIMENTAL_CHIPS
            if not (live_supported or experimental_ok):
                if actual_chip in FOTA_EXPERIMENTAL_CHIPS:
                    raise FotaSafetyError(
                        f"Live flashing of {actual_chip} is not supported yet without --experimental: the FOTA "
                        "commands answer correctly on a Maxwell 2, but a complete flash hasn't been verified. "
                        "Use --dry-run to simulate the transfer."
                    )
                raise FotaSafetyError(
                    f"Live flashing of {actual_chip} is not supported yet: the FOTA protocol has not been "
                    "verified on this chip. Use --dry-run to simulate the transfer."
                )

            # 3. Device connection and route
            self.relay = self._relay_via_dongle()

            # 4. Installed version and firmware track
            if not installed:
                try:
                    installed = self._read_target_version()
                except Exception as e:
                    raise FotaSafetyError(f"Could not read the {self.target} firmware version: {e}")
            if installed == "Unknown" or parse_version_tuple(installed) == (0,):
                raise FotaSafetyError(f"Could not read the {self.target} firmware version: refusing to flash blind.")
            self.installed_version = installed

            if not self.device_track and getattr(self._live_transport(), "product_id", None) in ANC_PIDS:
                self.device_track = "maxwell2_anc"
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
                usb_powered = headset_usb_powered(self.client)
                try:
                    batt_pct = self._settled_battery()
                except FotaSafetyError:
                    raise
                except Exception as e:
                    raise FotaSafetyError(f"Could not read the headset battery level: {e}")
                if batt_pct is None:
                    raise FotaSafetyError("Could not read the headset battery level: refusing to flash.")
                needed = required_battery_pct(usb_powered)
                if batt_pct < needed:
                    if needed == MIN_HEADSET_BATTERY_PCT:
                        raise FotaSafetyError(
                            f"Headset battery is {batt_pct}%. Minimum {needed}% required for flashing safety."
                        )
                    raise FotaSafetyError(
                        f"Headset battery reads {batt_pct}% while charging. Readings on the charger run up to "
                        f"~40 points high, so {needed}% is required while charging ({MIN_HEADSET_BATTERY_PCT}% "
                        "otherwise). Let it charge further and try again."
                    )

            # 7. Staging partition, as reported by the device
            part = self._query_fota_partition()
            if len(self.image) > part["length"]:
                raise FotaSafetyError(
                    f"Package ({len(self.image)} bytes) does not fit the {self.target}'s FOTA partition "
                    f"({part['length']} bytes): wrong package for this device?"
                )
            staging = (part["address"], part["length"])
            for p in self.package.partitions:
                dest = (p["dst_flash_addr"], p["decompressed_size"])
                if ranges_overlap(staging, dest):
                    raise FotaSafetyError(
                        f"The device's FOTA partition 0x{staging[0]:08X}+0x{staging[1]:X} overlaps package partition "
                        f"0x{dest[0]:08X}+0x{dest[1]:X}: refusing, since erasing it could damage installed firmware."
                    )
            self.fota_partition = part

        return {
            "package_valid": True,
            "target": self.target,
            "chip_target": actual_chip,
            "firmware_track": pkg_track,
            "device_track": self.device_track,
            "version": self.package.version_string,
            "installed_version": installed,
            "is_downgrade": is_downgrade,
            "relay_via_dongle": self.relay,
            "fota_partition": self.fota_partition,
            "total_bytes": self.total_bytes,
            "total_pages": self.total_pages,
            "total_chunks": self.total_chunks,
            "battery_percent": batt_pct,
            "experimental": self.experimental and actual_chip in FOTA_EXPERIMENTAL_CHIPS,
            "stage_only": self.stage_only,
            "dry_run": self.dry_run,
        }

    # --------------------------------------------------------------------------
    # Transfer
    # --------------------------------------------------------------------------

    def _report(self, callback, done: int, total: int, start: float):
        if not callback:
            return
        elapsed = max(0.001, time.time() - start)
        rate = done / elapsed
        callback({
            "phase": self.phase,
            "done": done,
            "total": total,
            "percent": int(done * 100 / total) if total else 100,
            "current_chunk": done,
            "total_chunks": total,
            "elapsed_sec": round(elapsed, 1),
            "speed_kb_s": round(rate * PAGE_SIZE / 1024.0, 1),
            "eta_sec": round((total - done) / rate, 1) if rate > 0 else 0.0,
            "done_all": done >= total,
        })

    def _write_pages(self, transport, address: int, data: bytes):
        """Writes one batch of pages, retrying on a NAK, a checksum error or a missing reply."""
        packet = build_page_program(address, data)
        last_error: Optional[Exception] = None
        for attempt in range(FOTA_PAGE_ATTEMPTS):
            try:
                # On a retry, flush first: a late reply to the earlier attempt must not be taken for this one
                resp = transport.exchange(packet, timeout_sec=FOTA_PAGE_TIMEOUT_SEC, drain=attempt > 0,
                                          poll_interval=FOTA_POLL_INTERVAL_SEC, abort_cmds=(CMD_FOTA_STOP,))
                parse_page_program(resp, address, len(data) // PAGE_SIZE)
                return
            except DeviceAbortError:
                raise  # the device ended the session: retrying the page can't help
            except (ProtocolError, TimeoutError) as e:
                last_error = e
        raise ProtocolError(f"Page write at 0x{address:08X} failed after {FOTA_PAGE_ATTEMPTS} attempts: {last_error}")

    def _read_page(self, transport, address: int) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(FOTA_PAGE_ATTEMPTS):
            try:
                resp = transport.exchange(build_page_read(address), timeout_sec=FOTA_PAGE_TIMEOUT_SEC,
                                          drain=attempt > 0, poll_interval=FOTA_POLL_INTERVAL_SEC,
                                          abort_cmds=(CMD_FOTA_STOP,))
                return parse_page_read(resp, address)
            except DeviceAbortError:
                raise
            except (ProtocolError, TimeoutError) as e:
                last_error = e
        raise ProtocolError(f"Page read at 0x{address:08X} failed after {FOTA_PAGE_ATTEMPTS} attempts: {last_error}")

    def _stopped_message(self, reason: str) -> str:
        if self.phase == "start":
            written = "Nothing was written to the firmware staging area."
        elif self.phase == "erase":
            written = "The staging area may be partly erased, but no firmware pages were written to it."
        else:
            written = (f"{self.pages_written} of {self.total_pages} pages had been written to the staging area; "
                       "the next update attempt erases them.")
        return (
            f"{reason} during the {self.phase} step. {written} Nothing was installed: the commit step was never "
            f"sent, so the {self.target} is still running {self.installed_version or 'its current firmware'}. "
            "Run 'firmware status' to check, then retry."
        )

    def _snapshot_settings(self):
        """Saves headset settings and custom EQ before an update, in case the new firmware resets them."""
        dump = getattr(self.client, "dump_all", None)
        if dump is None:
            return
        try:
            self.settings_snapshot = dump()
            self.backup_file = create_backup("pre_firmware_update", self.settings_snapshot)
        except Exception:
            self.backup_file = None  # a failed snapshot doesn't make the update unsafe

    @staticmethod
    def _eq_curve(bands) -> List[Tuple[float, float]]:
        return [(float(b.get("frequency_hz", 0)), float(b.get("gain_db", 0))) for b in bands or []]

    def _restore_custom_eq(self) -> Dict[str, Any]:
        """
        Rewrites every custom EQ slot that differs from the pre-update snapshot. Seen on a Maxwell 2
        headset: a full update reset the custom EQ slots to flat but kept the preset, sidetone,
        chat mix and noise reduction. The active preset isn't changed. Errors are reported, not raised:
        the update itself has already succeeded.
        """
        restored: List[int] = []
        errors: List[str] = []
        saved_slots = (self.settings_snapshot or {}).get("custom_eq_profiles") or {}
        read = getattr(self.client, "read_custom_eq", None)
        write = getattr(self.client, "write_custom_eq", None)
        if not saved_slots or read is None or write is None:
            return {"restored": restored, "errors": errors}
        for slot in range(4):
            bands = (saved_slots.get(f"custom_slot_{slot + 1}") or {}).get("bands")
            if not bands:
                continue  # that slot couldn't be read before the update
            try:
                if self._eq_curve(read(slot).get("bands")) == self._eq_curve(bands):
                    continue
                write(slot, bands, activate=False)
                restored.append(slot + 1)
            except Exception as e:
                errors.append(f"custom EQ slot {slot + 1}: {e}")
        return {"restored": restored, "errors": errors}

    def _simulate(self, progress_callback, start: float) -> Dict[str, Any]:
        self.phase = "write"
        for done in range(0, self.total_pages + 1, max(1, self.total_pages // 50)):
            if self.cancelled:
                raise FotaInterruptedError("FOTA flash cancelled (dry run, nothing was written).", 0, self.total_pages)
            self._report(progress_callback, done, self.total_pages, start)
        self._report(progress_callback, self.total_pages, self.total_pages, start)
        return {
            "success": True,
            "target": self.target,
            "version": self.package.version_string,
            "previous_version": self.installed_version,
            "total_bytes": self.total_bytes,
            "total_pages": self.total_pages,
            "chunks_sent": self.total_pages,
            "duration_sec": round(time.time() - start, 2),
            "dry_run": True,
            "staged_only": self.stage_only,
            "verification": "skipped (dry run)",
            "reported_version": None,
            "backup_file": None,
        }

    def execute(self, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        Runs the update. Calls progress_callback(dict) as each step advances.
        Raises FotaInterruptedError on Ctrl-C and FotaTransferError if a step fails before commit;
        either way nothing has been installed.
        """
        pre = self.preflight_check()
        start = time.time()
        if self.dry_run:
            return self._simulate(progress_callback, start)

        transport = self._live_transport()
        self._snapshot_settings()
        try:
            transport.claim_exclusive()
        except DeviceBusyError as e:
            raise FotaSafetyError(str(e))
        transport.auto_reconnect = False

        committed = False
        base = self.fota_partition["address"]
        try:
            with self._target_route() as t:
                # 1. Open the update session
                self.phase = "start"
                self._open_session(t)

                # 2. Erase exactly the staging partition the device reported
                self.phase = "erase"
                self._report(progress_callback, 0, 1, start)
                self._command(t, build_partition_erase(base, self.fota_partition["length"]), FOTA_ERASE_TIMEOUT_SEC)
                self._report(progress_callback, 1, 1, start)

                # 3. Write the package
                self.phase = "write"
                phase_start = time.time()
                step = self.pages_per_write * PAGE_SIZE
                for offset in range(0, len(self.image), step):
                    if self.cancelled:
                        raise KeyboardInterrupt
                    chunk = self.image[offset:offset + step]
                    self._write_pages(t, base + offset, chunk)
                    self.pages_written += len(chunk) // PAGE_SIZE
                    self._report(progress_callback, self.pages_written, self.total_pages, phase_start)

                # 4. Read every page back: the device's own check comes next, this one is independent of it
                self.phase = "verify"
                phase_start = time.time()
                for offset in range(0, len(self.image), PAGE_SIZE):
                    if self.cancelled:
                        raise KeyboardInterrupt
                    if self._read_page(t, base + offset) != self.image[offset:offset + PAGE_SIZE]:
                        raise ProtocolError(f"Read-back mismatch at 0x{base + offset:08X}: the staged image is not what was sent")
                    self.pages_verified += 1
                    self._report(progress_callback, self.pages_verified, self.total_pages, phase_start)

                # 5. Device-side integrity check of the staged package. The read-back has left the
                #    session idle for over a minute, long enough for the device to end it, so restart it.
                self.phase = "integrity"
                self._open_session(t)
                self._command(t, build_integrity_check(), FOTA_INTEGRITY_TIMEOUT_SEC)

                if self.stage_only:
                    return self._result(pre, start, "staged (not committed)", None, staged=True)

                # 6. Commit: the device reboots and its bootloader installs the staged image
                self.phase = "commit"
                self._command(t, build_write_state(FOTA_STATE_DOWNLOADED))
                committed = True
                try:
                    self._command(t, build_commit())
                except ProtocolError as e:
                    committed = False
                    raise ProtocolError(f"Device refused the commit ({e})")
                except (TimeoutError, OSError):
                    self.reboot_seen = True  # the device may reboot before its reply gets out
        except KeyboardInterrupt:
            self.cancelled = True
            if committed:
                raise FotaInterruptedError(
                    "Cancelled after the commit was sent: the device is installing the update. Do NOT unplug or "
                    "power it off; wait for it to come back and run 'firmware status'.",
                    self.pages_written, self.total_pages,
                )
            raise FotaInterruptedError(self._stopped_message("Cancelled"), self.pages_written, self.total_pages)
        except (ProtocolError, TimeoutError, OSError, RuntimeError) as e:
            if committed:
                raise FotaTransferError(
                    f"Lost contact after the commit ({e}). The device is probably installing the update: do NOT "
                    "unplug or power it off. Wait a few minutes, then run 'firmware status'.",
                    self.pages_written, self.total_pages,
                )
            raise FotaTransferError(self._stopped_message(f"Stopped ({e})"), self.pages_written, self.total_pages)
        finally:
            transport.auto_reconnect = True
            try:
                transport.release_exclusive()
            except Exception:
                pass  # after commit the device has rebooted and this descriptor is gone

        # 7. Wait for the device to come back and report the new version
        self.phase = "install"
        verification = self._verify_installed_version(progress_callback, reboot_seen=self.reboot_seen)
        if verification["verification"] == "confirmed":
            self.phase = "restore"
            self.eq_restore = self._restore_custom_eq()
        return self._result(pre, start, verification["verification"], verification["reported_version"])

    def _result(self, pre: Dict[str, Any], start: float, verification: str, reported: Optional[str],
                staged: bool = False) -> Dict[str, Any]:
        return {
            "success": staged or verification == "confirmed",
            "target": self.target,
            "version": self.package.version_string,
            "previous_version": pre["installed_version"],
            "total_bytes": self.total_bytes,
            "total_pages": self.total_pages,
            "chunks_sent": self.pages_written,
            "pages_verified": self.pages_verified,
            "fota_partition": self.fota_partition,
            "duration_sec": round(time.time() - start, 2),
            "dry_run": False,
            "staged_only": staged,
            "verification": verification,
            "reported_version": reported,
            "backup_file": self.backup_file,
            "eq_restored": (self.eq_restore or {}).get("restored", []),
            "eq_restore_errors": (self.eq_restore or {}).get("errors", []),
        }

    def _staging_consumed(self) -> Optional[bool]:
        """
        Whether the bootloader has taken the staged package: after an install it clears the staging
        area, while an uncommitted package stays there across reboots (both seen on a Maxwell 2
        headset and dongle). True / False, or None if the staging area can't be read right now.
        """
        transport = getattr(self.client, "transport", None)
        if transport is None or not self.fota_partition:
            return None
        if self.target == "dongle":
            if getattr(transport, "is_dongle", None) is not True:
                return None  # only a dongle node can reach the dongle's flash
            route = False
        else:
            # The headset's flash: relayed through a dongle node, direct on the headset's USB node
            route = bool(getattr(transport, "node_is_dongle", None) if getattr(transport, "node_is_dongle", None)
                         is not None else transport.is_dongle)
        address = self.fota_partition["address"]
        try:
            resp = transport.exchange(build_page_read(address), timeout_sec=FOTA_PAGE_TIMEOUT_SEC, is_dongle=route)
            return parse_page_read(resp, address) != self.image[:PAGE_SIZE]
        except Exception:
            return None

    def _verify_installed_version(self, progress_callback=None, reboot_seen: bool = False) -> Dict[str, Any]:
        """
        Polls the target's version until it reports the package version or the timeout expires.
        If the package version equals the one installed before, a matching version proves nothing
        on its own (the old firmware reports it too, if the device hasn't rebooted yet), so the
        device must also have been seen going away: a failed or empty read, or a reconnect.
        """
        expected = parse_version_tuple(self.package.version_string)
        same_version = bool(self.installed_version) and parse_version_tuple(self.installed_version) == expected
        transport = getattr(self.client, "transport", None)
        reconnects_before = getattr(transport, "reconnect_count", 0)
        start = time.time()
        deadline = start + self.readback_timeout_sec
        last_seen = "Unknown"
        staged_still = False
        while True:
            staged_still = False
            try:
                last_seen = self._read_target_version()
            except Exception:
                last_seen = "Unknown"  # expected while the device reboots into the new image
            if last_seen == "Unknown" or getattr(transport, "reconnect_count", 0) != reconnects_before:
                reboot_seen = True
            matches = last_seen != "Unknown" and parse_version_tuple(last_seen) == expected
            if matches:
                consumed = self._staging_consumed()
                if consumed is None:
                    # Can't read the staging area: fall back to version (and reboot, for a same-version update)
                    if reboot_seen or not same_version:
                        return {"verification": "confirmed", "reported_version": last_seen}
                elif consumed:
                    return {"verification": "confirmed", "reported_version": last_seen}
                else:
                    staged_still = True  # answering, but the bootloader hasn't installed the package yet
            if progress_callback:
                elapsed = time.time() - start
                progress_callback({
                    "phase": "install", "done": int(elapsed), "total": int(self.readback_timeout_sec),
                    "percent": min(100, int(elapsed * 100 / self.readback_timeout_sec)) if self.readback_timeout_sec else 100,
                    "current_chunk": 0, "total_chunks": 0, "elapsed_sec": round(elapsed, 1),
                    "speed_kb_s": 0.0, "eta_sec": 0.0, "done_all": False, "reported_version": last_seen,
                })
            if time.time() >= deadline:
                break
            time.sleep(self.readback_interval_sec)
        if last_seen == "Unknown":
            status = "no_response"
        elif parse_version_tuple(last_seen) == expected and staged_still:
            status = "still_staged"    # the package is still in the staging area: not installed
        elif parse_version_tuple(last_seen) == expected:
            status = "no_reboot_seen"  # same version, and the device never went away: install not proven
        else:
            status = "version_mismatch"
        return {"verification": status, "reported_version": last_seen}


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

    def row(text: str) -> str:
        return "│" + text[:70].ljust(70) + "│"

    batt = diag.get("battery_percent")
    batt_str = f"{batt}%" if batt is not None else "Unknown"
    batt_safe = diag.get("battery_safe_for_flashing", False)
    needed = diag.get("battery_required_percent", MIN_HEADSET_BATTERY_PCT)
    charging = needed > MIN_HEADSET_BATTERY_PCT
    safe_badge = (f"(Pass >= {needed}%" + (", charging)" if charging else ")")) if batt_safe else \
        (f"(LOW: {needed}% needed" + (" on charger)" if charging else ")"))
    lines.append(row(f"  Headset Battery Level     : {batt_str:<8} {safe_badge}"))

    conn = diag.get("connection_mode", "Unknown")
    lines.append(f"│  Active Transport Path     : {conn:<38} │")

    lines.append("│                                                                      │")
    lines.append("├─ UPDATE SAFETY READINESS ────────────────────────────────────────────┤")

    ready = diag.get("update_prerequisites_met", False)
    if ready:
        lines.append("│  [PASS] All pre-flight safety requirements satisfied for updates:     │")
        lines.append("│         ✔ Dongle and Headset firmware versions match.                │")
        lines.append(row(f"         ✔ Headset battery has sufficient capacity (>= {needed}%)."))
        lines.append("│         ✔ Updates are staged: nothing installs before the commit.     │")
    else:
        lines.append("│  [CAUTION] Pre-flight prerequisites not fully satisfied:             │")
        if not batt_safe:
            if charging:
                lines.append(row(f"         ✖ Below {needed}% while charging (readings on the charger run"))
                lines.append(row("           high). Let it charge further before flashing."))
            else:
                lines.append(row(f"         ✖ Battery is below {needed}%. Charge headset before flashing."))
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
    for node, ver in info.get("installed_nodes", {}).items():
        lines.append(f"│    {node.capitalize():<24}: {ver:<38} │")

    if info.get("is_package"):
        lines.append(f"│  Target Package Version    : {info.get('target_version', 'Unknown'):<38} │")
        lines.append(f"│  Target Chip Architecture  : {info.get('package_chip', 'Unknown'):<38} │")
        lines.append(f"│  Package Firmware Track    : {info.get('package_track', 'unknown'):<38} │")
        lines.append(f"│  Package Integrity Check   : {'✔ SHA-256 PASS (Valid)' if info.get('package_valid') else '✖ CORRUPTED':<38} │")
    else:
        lines.append(f"│  Latest Available Version  : {info.get('latest_available', 'Unknown'):<38} │")

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
