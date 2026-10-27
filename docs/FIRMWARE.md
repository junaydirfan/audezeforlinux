# Audeze Maxwell 2 Firmware Architecture & Update Specification

## 1. Executive Summary & Safety Policy

Firmware updates are the highest-risk hardware operation on the Audeze Maxwell 2. An incomplete, corrupted, or out-of-sync firmware write can render the headset or USB wireless dongle inoperable ("bricked"), requiring factory service or low-level JTAG/SWD hardware recovery.

### Safety Invariants
1. **Never Auto-Update**: No update will ever begin automatically or in the background.
2. **Dual-Target Parity Check**: The Audeze Maxwell system consists of **two independent computational nodes** (the USB Dongle and the Headset). Their firmware versions must match. Flashing one without the other can break the proprietary 2.4 GHz wireless pairing link.
3. **Power & Transport Prerequisite**: The headset must report **$\ge 50\%$ battery**, and firmware writes to the headset should ideally take place over direct USB-C rather than over the air (FOTA).
4. **Mandatory Checksum Verification**: Every firmware package must be parsed, uncompressed, and cryptographically verified before any transport packets are sent.
5. **Dry-Run Analysis First**: Any flashing implementation will require an explicit dry-run step that verifies packet layout against official captures.

---

## 2. Hardware Identification & Current Status

Querying the user's live device via `CMD_FIRMWARE` (`0x1C07`):

| Device Node | Transport Routing Byte | Active Firmware Version | Hardware Status |
|:---|:---:|:---:|:---:|
| **USB Dongle** | `0x00` | **`v1.0.5.7.4`** | Operational |
| **Wireless Headset** | `0x80` | **`v1.0.5.7.4`** | Operational & Paired |

Both nodes are currently synchronized on version `v1.0.5.7.4`.

---

## 3. Protocol Architecture: Airoha FOTA & RACE 0x15

The Audeze Maxwell and its dongle run **Airoha (MediaTek) AB15xx** System-on-Chips. The chip a package is built for is stored in TLV `0x0020`:

| Chip identifier (TLV 0x0020) | Target | Live flashing |
|---|---|---|
| `AB1568_Dongle` | Dongle | Supported |
| `AB1568_Headset` | Headset | Supported |
| `AB1565_ULL2_DONGLE` | Maxwell 2 dongle | **Blocked** (dry-run only) |
| `AB1585_Headset` | Maxwell 2 headset | **Blocked** (dry-run only) |

The Maxwell 2 ids come from the official Maxwell 2 packages (v1.0.5.7.4 and the v1.0.2.60.2 rollback) published in Audeze's `maxwell-2-fw.json` list. The container format below parses and verifies on them. The RACE `0x15` FOTA block framing in section 3.1 has **not** been verified on these chips yet, so `firmware flash` refuses live writes to them until USB captures of the official updater confirm it. Packages with an unrecognised or missing chip id are also refused for live flashing.

### 3.1 Packet Framing
While standard settings and telemetry use RACE sync byte `0x05`, Airoha's Firmware-Over-The-Air (FOTA) subsystem uses sync byte **`0x15`**:

```
+------------+------------+--------------------+--------------------+-----------------------+
| Sync Byte  | Direction  | Length (LE16)      | Command ID (LE16)  | Payload               |
| 0x15       | 0x5A / 0x5B| len(Payload) + 2   | FOTA Opcode        | Block data            |
+------------+------------+--------------------+--------------------+-----------------------+
```

- **`0x15 0x5A`**: Host $\rightarrow$ Device firmware block transmission.
- **`0x15 0x5B`**: Device $\rightarrow$ Host block acknowledgment and flash status.

### 3.2 Dual-Device Update Flow
1. **Pre-flight Check**:
   - Query dongle version: `0x05 0x5A 0x03 0x00 0x07 0x1C 0x00` (route `0x00`)
   - Query headset version: `0x05 0x5A 0x03 0x00 0x07 0x1C 0x00` (route `0x80`)
   - Query battery level: `0x05 0x5A 0x03 0x00 0xD6 0x0C 0x00` (must be $\ge 50\%$)
2. **Phase A: Update USB Dongle**:
   - Dongle switches to bootloader mode.
   - Host streams LZMA-compressed firmware partitions via 62-byte HID reports.
   - Dongle verifies flash CRC32 and reboots.
3. **Phase B: Update Headset**:
   - If connected via USB-C: updated directly over USB HID.
   - If connected wirelessly: updated via FOTA over 2.4 GHz link.
   - Headset commits image to secondary flash bank, verifies checksum, and switches partition on reboot.

---

---

## 4. Firmware Image Container Format (Reverse-Engineered Ground Truth)

Official Audeze Maxwell firmware distributions (`.bin`) encapsulate Airoha multi-binary packages structured as follows:

```
0x0000-0x001F : 32-byte top-level SHA-256 checksum of data[0x100:]
0x0020-0x00FF : 0xFF padding
0x0100-0x010D : TLV 0x0011 = Basic info (10 bytes value)
                bytes 0-1: flags (01 01)
                bytes 2-5: LZMA stream start (LE 0x00001000)
                bytes 6-9: LZMA stream size (LE 32-bit integer — must match exact compressed size)
0x010E-0x0129 : TLV 0x0013 = Version string (e.g. "v1.0.5.7.4\0" + 0xFF padding)
0x012E-0x0165 : TLV 0x0012 = Partition table (52 bytes: 4 count + 4 entries × 12 bytes)
                Each entry: src_offset (LE32), decompressed_size (LE32), dst_flash_addr (LE32)
0x0166-0x01ED : TLV 0x0014 = Per-partition SHA-256 hashes (132 bytes: 4 count + 4 × 32-byte hashes)
0x01EE-0x01FC : TLV 0x0020 = Chip identifier (e.g. "AB1568_Dongle", "AB1565_ULL2_DONGLE", "AB1585_Headset")
0x0200-0x0214 : TLV 0x0021 = Design reference name ("headset_ref_design" or "dongle_ref_design")
0x0216-0x021A : TLV 0x00F0 = Misc flag (1 byte: 0x01)
0x021B-0x0FFF : 0xFF padding
0x1000+       : LZMA-Alone compressed stream (lc=3, lp=0, pb=2, dict=16KB, decompressed size ~3.2 MB)
```

### Partition Mapping (Airoha AB1568 SPI Flash)
1. **Partition 1 (Bootloader / HAL)**: `dst_flash = 0x08013000`, size ~1.1 MB
2. **Partition 2 (Kernel / App / FreeRTOS)**: `dst_flash = 0x08133000` (contains reset vector), size ~1.6 MB
3. **Partition 3 (Tensilica HiFi DSP Audio Pipeline)**: `dst_flash = 0x0832C000`, size ~80 KB
4. **Partition 4 (NVDM Factory Defaults & Audio Calibration)**: `dst_flash = 0x08450000`, size ~300 KB

### Dual-Bank Flash Safety Model
The Airoha AB1568 employs a **dual-bank flash architecture**:
- The current operating firmware runs from **Bank A**.
- Firmware updates stream sequentially into the inactive **Bank B**.
- The bootloader verifies all 4 partition hashes (`TLV 0x0014`) and the outer SHA-256 before swapping the active bank flag.
- If a transmission is aborted or corrupted, the bootloader reverts to Bank A, preventing hard bricking.
- Verification is **keyless SHA-256 only** — no asymmetric ECDSA/RSA keys exist in the bootloader.

---

## 5. Firmware Management CLI Commands

### 1. Dual-Node Status & Pre-Flight Diagnostic Gate
```bash
audezeforlinux firmware
# or
audezeforlinux firmware status
```
Queries both dongle and headset, evaluates version parity, and checks if battery is $\ge 50\%$.

### 2. Catalog & Package Update Comparison
```bash
audezeforlinux firmware check
# or compare directly against a downloaded firmware package:
audezeforlinux firmware check --package path/to/firmware.bin
```
Evaluates installed firmware against the known baseline catalog or directly against a downloaded official `.bin` package.

> [!NOTE]
> Audeze does not provide a public, unauthenticated cloud REST API for checking new firmware. By default, `firmware check` evaluates against our offline verified baseline catalog. Use `--package <file.bin>` to compare against a newly acquired firmware binary directly.

### 3. Deep Package Inspection & Hash Verification
```bash
audezeforlinux firmware inspect path/to/firmware.bin
```
Parses all TLVs, verifies the outer SHA-256 checksum, decompresses LZMA partitions, and validates each partition's SHA-256 hash.

### 4. Partition Unpacker & Decompressor
```bash
audezeforlinux firmware unpack path/to/firmware.bin --out ./extracted_firmware
```
Decompresses the LZMA payload and extracts all 4 individual partitions as raw binary files for inspection.

### 5. FOTA Transmission Simulation (Dry-Run Mode)
```bash
audezeforlinux firmware simulate path/to/firmware.bin --target headset
```
Simulates the entire FOTA block fragmentation layout across 62-byte HID reports without transmitting to physical hardware.

### 6. FOTA Flashing Engine with Dual-Bank Protection
```bash
# Dry-run execution (simulates chunking, battery check, and hash verification):
audezeforlinux firmware flash path/to/firmware.bin --target dongle --dry-run

# Live physical flash (requires explicit safety flag and typing 'CONFIRM'):
audezeforlinux firmware flash path/to/firmware.bin --target dongle --confirm-risk
```
Streams 56-byte chunks into the inactive Bank B of the target SoC over USB HID Report `0x06`. If any error or power drop occurs, the bootloader automatically reverts to Bank A on reboot, preventing any bricking risk.
