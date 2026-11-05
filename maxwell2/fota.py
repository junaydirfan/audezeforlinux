"""
Airoha RACE FOTA (firmware update) commands for the Maxwell 2 headset (AB158x) and dongle (AB156x).

How an update works on these chips:
  The device has a FOTA partition: a staging area separate from the running firmware. The host
  erases it, writes the official .bin package into it page by page, and asks the device to check
  it. Nothing that runs changes until COMMIT: the device then reboots, and its bootloader unpacks
  the staged package over the firmware partitions. Stopping before commit leaves the device on
  its current firmware.

Sources:
  - Command IDs, payload layouts and the page checksum follow race-toolkit
    (github.com/auracast-research/race-toolkit, librace/), which implements this flow and reports
    it working on Airoha-based Sony headphones.
  - Confirmed on a Maxwell 2 (Xbox dongle 3329:4B28 + headset 3329:4B2E, firmware v1.0.5.7.4):
    the partition query answers in this layout (headset 0x004BC000 + 0x323000, dongle
    0x00263000 + 0x194000), page reads work, and the dongle relays FOTA commands to the
    headset on route 0x80. See docs/FIRMWARE.md.
"""

import struct
from typing import List, Tuple, Dict, Any

from maxwell2.protocol import (
    SYNC_BYTE,
    SYNC_BYTE_FOTA,
    DIRECTION_RESPONSE,
    ProtocolError,
    build_race_packet,
    parse_race_packet,
)

# RACE command IDs
CMD_FOTA_PARTITION_INFO = 0x1C00
CMD_FOTA_INTEGRITY_CHECK = 0x1C01
CMD_FOTA_COMMIT = 0x1C02
CMD_FOTA_STOP = 0x1C03
CMD_FOTA_WRITE_STATE = 0x1C06
CMD_FOTA_START = 0x1C08
CMD_FOTA_START_TRANSACTION = 0x1C0A
CMD_STORAGE_PAGE_PROGRAM = 0x0402
CMD_STORAGE_PAGE_READ = 0x0403
CMD_STORAGE_PARTITION_ERASE = 0x0404
CMD_READ_SDK_VERSION = 0x0301
CMD_GET_BUILD_VERSION = 0x1E08

# FOTA state values written with CMD_FOTA_WRITE_STATE, in the order the updater sends them.
# Their exact meaning isn't documented; race-toolkit sends these same values.
FOTA_STATE_START_1 = b"\x01\x02"
FOTA_STATE_START_2 = b"\x10\x02"
FOTA_STATE_DOWNLOADED = b"\x11\x02"

STORAGE_TYPE_FLASH = 0x00
PAGE_SIZE = 0x100
SECTOR_SIZE = 0x1000
MAX_PAGES_PER_WRITE = 3   # race-toolkit's default; more may fail on some devices
STATUS_CHECKSUM_ERROR = 0x0A


def _crc8_table(poly: int = 0x31) -> List[int]:
    table = []
    for value in range(256):
        for _ in range(8):
            value = ((value << 1) ^ poly) & 0xFF if value & 0x80 else (value << 1) & 0xFF
        table.append(value)
    return table


_CRC8_TABLE = _crc8_table()


def fota_checksum(page: bytes) -> int:
    """
    Per-page checksum for PAGE_PROGRAM: CRC-8 (polynomial 0x31, initial value 0, MSB first),
    with the result bit-reversed. Equivalent to race-toolkit's two lookup tables.
    """
    crc = 0
    for byte in page:
        crc = _CRC8_TABLE[crc ^ byte]
    return int(f"{crc:08b}"[::-1], 2)


# ==============================================================================
# Request builders
# ==============================================================================

def build_partition_info_query() -> bytes:
    return build_race_packet(CMD_FOTA_PARTITION_INFO, b"\x00")


def build_fota_start() -> bytes:
    return build_race_packet(CMD_FOTA_START, b"\x01\x00", head=SYNC_BYTE_FOTA)


def build_start_transaction() -> bytes:
    return build_race_packet(CMD_FOTA_START_TRANSACTION, b"", head=SYNC_BYTE_FOTA)


def build_write_state(state: bytes) -> bytes:
    if len(state) != 2:
        raise ProtocolError(f"FOTA state must be 2 bytes, got {len(state)}")
    return build_race_packet(CMD_FOTA_WRITE_STATE, state, head=SYNC_BYTE_FOTA)


def build_integrity_check() -> bytes:
    return build_race_packet(CMD_FOTA_INTEGRITY_CHECK, b"\x01\x00\x00", head=SYNC_BYTE_FOTA)


def build_commit() -> bytes:
    return build_race_packet(CMD_FOTA_COMMIT, b"\x00", head=SYNC_BYTE_FOTA)


def build_partition_erase(address: int, length: int) -> bytes:
    """Erases flash. The payload order is storage type, LENGTH, then ADDRESS."""
    if address % SECTOR_SIZE or length % SECTOR_SIZE or length <= 0:
        raise ProtocolError(f"Erase must be whole 4 KB sectors: address 0x{address:08X}, length 0x{length:X}")
    return build_race_packet(CMD_STORAGE_PARTITION_ERASE, struct.pack("<BII", STORAGE_TYPE_FLASH, length, address))


def build_page_program(address: int, data: bytes) -> bytes:
    """Writes 1..MAX_PAGES_PER_WRITE consecutive 256-byte pages starting at address."""
    if address % PAGE_SIZE:
        raise ProtocolError(f"Page address 0x{address:08X} is not 256-byte aligned")
    if not data or len(data) % PAGE_SIZE:
        raise ProtocolError(f"Page data must be whole 256-byte pages, got {len(data)} bytes")
    pages = len(data) // PAGE_SIZE
    if pages > MAX_PAGES_PER_WRITE:
        raise ProtocolError(f"At most {MAX_PAGES_PER_WRITE} pages per write, got {pages}")
    payload = bytearray(struct.pack("<BB", STORAGE_TYPE_FLASH, pages))
    for i in range(pages):
        page = data[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]
        payload += struct.pack("<BI", fota_checksum(page), address + i * PAGE_SIZE) + page
    return build_race_packet(CMD_STORAGE_PAGE_PROGRAM, bytes(payload), head=SYNC_BYTE_FOTA)


def build_page_read(address: int) -> bytes:
    """Reads one 256-byte page (the size field carries pages of 256 bytes, so 0x01)."""
    if address % PAGE_SIZE:
        raise ProtocolError(f"Page address 0x{address:08X} is not 256-byte aligned")
    return build_race_packet(CMD_STORAGE_PAGE_READ, struct.pack("<BBI", STORAGE_TYPE_FLASH, PAGE_SIZE >> 8, address))


# ==============================================================================
# Response parsers. Every one raises ProtocolError unless the device reported success.
# ==============================================================================

def _response_payload(packet: bytes, expected_cmd: int) -> bytes:
    cmd, payload = parse_race_packet(packet, expected_dir=DIRECTION_RESPONSE)
    if cmd != expected_cmd:
        raise ProtocolError(f"Expected reply to 0x{expected_cmd:04X}, got 0x{cmd:04X}")
    if not payload:
        raise ProtocolError(f"Empty reply to 0x{expected_cmd:04X}")
    return payload


def parse_status_response(packet: bytes, expected_cmd: int) -> None:
    """For replies that carry only a status byte (start, state, erase, integrity, commit)."""
    status = _response_payload(packet, expected_cmd)[0]
    if status != 0:
        raise ProtocolError(f"Device rejected command 0x{expected_cmd:04X}: status 0x{status:02X}")


def parse_partition_info(packet: bytes) -> Dict[str, int]:
    """Returns {'address', 'length'} of the FOTA staging partition."""
    payload = _response_payload(packet, CMD_FOTA_PARTITION_INFO)
    if len(payload) < 11:
        raise ProtocolError(f"FOTA partition reply too short ({len(payload)} bytes)")
    status, _, address, length = struct.unpack_from("<BHII", payload, 0)
    if status != 0:
        raise ProtocolError(f"Device rejected FOTA partition query: status 0x{status:02X}")
    if length == 0 or address % SECTOR_SIZE or length % SECTOR_SIZE:
        raise ProtocolError(f"Implausible FOTA partition: address 0x{address:08X}, length 0x{length:X}")
    return {"address": address, "length": length}


def parse_page_program(packet: bytes, address: int, pages: int) -> None:
    payload = _response_payload(packet, CMD_STORAGE_PAGE_PROGRAM)
    if len(payload) < 7:
        raise ProtocolError(f"Page program reply too short ({len(payload)} bytes)")
    status, _, reply_pages, reply_address = struct.unpack_from("<BBBI", payload, 0)
    if status == STATUS_CHECKSUM_ERROR:
        raise ProtocolError(f"Device reported a checksum error writing 0x{address:08X}")
    if status != 0:
        raise ProtocolError(f"Device rejected page write at 0x{address:08X}: status 0x{status:02X}")
    if reply_address != address or reply_pages != pages:
        raise ProtocolError(
            f"Page write reply is for 0x{reply_address:08X} x{reply_pages}, expected 0x{address:08X} x{pages}"
        )


def parse_page_read(packet: bytes, address: int) -> bytes:
    payload = _response_payload(packet, CMD_STORAGE_PAGE_READ)
    if len(payload) < 8 + PAGE_SIZE:
        raise ProtocolError(f"Page read reply too short ({len(payload)} bytes)")
    status = payload[0]
    reply_address = struct.unpack_from("<I", payload, 4)[0]
    if status != 0:
        raise ProtocolError(f"Device rejected page read at 0x{address:08X}: status 0x{status:02X}")
    if reply_address != address:
        raise ProtocolError(f"Page read reply is for 0x{reply_address:08X}, expected 0x{address:08X}")
    return bytes(payload[8:8 + PAGE_SIZE])


def parse_version_string(packet: bytes, expected_cmd: int) -> str:
    """SDK (0x0301) and build (0x1E08) version replies: status byte, then NUL-separated ASCII fields."""
    payload = _response_payload(packet, expected_cmd)
    text = payload[1:].decode("ascii", errors="replace")
    return " ".join(part for part in text.replace("#", "").split("\x00") if part.strip())


# ==============================================================================
# Planning helpers
# ==============================================================================

def pad_to_pages(data: bytes) -> bytes:
    """Pads with 0xFF (erased flash) to a whole number of 256-byte pages."""
    remainder = len(data) % PAGE_SIZE
    return data + b"\xFF" * (PAGE_SIZE - remainder) if remainder else data


def erase_length_for(size: int) -> int:
    """Smallest whole-sector length covering size bytes."""
    return (size + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE


def ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """True if [start, start+length) ranges a and b overlap."""
    return a[0] < b[0] + b[1] and b[0] < a[0] + a[1]
