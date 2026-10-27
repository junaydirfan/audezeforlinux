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
| `AB1568_Dongle` | Dongle | **Blocked** (dry-run only) |
| `AB1568_Headset` | Headset | **Blocked** (dry-run only) |
| `AB1565_ULL2_DONGLE` | Maxwell 2 dongle | **Blocked** (dry-run only) |
| `AB1585_Headset` | Maxwell 2 headset | **Blocked** (dry-run only) |

The Maxwell 2 ids come from the official Maxwell 2 packages (v1.0.5.7.4 and the v1.0.2.60.2 rollback) published in Audeze's `maxwell-2-fw.json` list. The container format below parses and verifies on them. The RACE `0x15` FOTA block framing in section 3.1 has **not** been verified against captures of the official updater on **any** chip, so `firmware flash` refuses live writes for all of them. (Until October 2026 the live write path also crashed before sending its first chunk, so it has never written to a device.) Packages with an unrecognised or missing chip id are also refused for live flashing.

### Firmware tracks: regular vs ANC Maxwell 2
Audeze publishes the Maxwell 2 ANC firmware as a **separate track** (`mw2-anc/` in the official bucket, `Maxwell2ANC_*` packages; v1.0.5.6 is listed in `maxwell-2-anc-rollback.json`). The ANC packages report **the same** chip ids (`AB1565_ULL2_DONGLE`, `AB1585_Headset`) and design names as regular Maxwell 2 packages, but the images differ (the v1.0.5.6 ANC headset image is 2,625,444 bytes, the v1.0.5.7.4 regular one 2,316,701 bytes). So the chip id can't tell the tracks apart:

- A package's track (`firmware_track` in `firmware inspect`) comes from a table of known official package hashes, then from the `Maxwell2ANC` file name prefix.
- The device's track is inferred from its installed version. Only versions known on exactly one track count. `firmware check` falls back to the regular track for unknown versions; the flash path does not.
- Preflight refuses a package whose track differs from the device's. A live flash of a Maxwell 2-family chip also needs the device track to be known.
- The ANC catalog is incomplete (the newest ANC release is unknown), so `firmware check` says so instead of reporting "up to date". The ANC USB product IDs are also still unknown.

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

## 4. Firmware Image Container Format (Reverse-Engineered Ground Truth)

Official Audeze Maxwell firmware distributions (`.bin`) encapsulate Airoha multi-binary packages. The header is a sequence of TLV records (`type` LE16, `length` LE16, then `length` bytes) starting at `0x0100`, followed by `0xFF` padding up to the LZMA stream. **The TLV offsets, partition count and flash addresses vary between packages**: read the records in order rather than assuming fixed offsets. `FirmwarePackage` scans the records this way.

Example layout of an AB1568 package with 4 partitions (offsets are specific to this package):

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
0x01EE-0x01FC : TLV 0x0020 = Chip identifier ("AB1568_Headset" or "AB1568_Dongle")
0x0200-0x0214 : TLV 0x0021 = Design reference name ("headset_ref_design" or "dongle_ref_design")
0x0216-0x021A : TLV 0x00F0 = Misc flag (1 byte: 0x01)
0x021B-0x0FFF : 0xFF padding
0x1000+       : LZMA-Alone compressed stream (lc=3, lp=0, pb=2, dict=16KB, decompressed size ~3.2 MB)
```

The TLV order is the same on Maxwell 2 packages, but the record sizes, and therefore the offsets, differ. Values observed with `firmware inspect`:

| Package | Partitions | TLV 0x0020 offset | Flash addresses |
|---|---|---|---|
| AB1568 example above | 4 | `0x01EE` | `0x08013000` … `0x08450000` |
| `Maxwell2ANC_PS_dongle_v1.0.5.6.bin` (`AB1565_ULL2_DONGLE`) | 2 | `0x0196` | `0x00013000`, `0x000E2000` |
| `Maxwell2ANC_PS_headset_v1.0.5.6.bin` (`AB1585_Headset`) | 6 | `0x0246` | `0x00015000` … `0x007E0000` |

The partition table (TLV `0x0012`) and hash table (TLV `0x0014`) both start with an LE32 count, so they are `4 + count × 12` and `4 + count × 32` bytes long.

### Partition Mapping (AB1568 example package only)
The roles below were identified on the 4-partition AB1568 package. Maxwell 2 (AB1565_ULL2 / AB1585) packages use a different partition count and address map, and their partition roles have not been identified.

1. **Partition 1 (Bootloader / HAL)**: `dst_flash = 0x08013000`, size ~1.1 MB
2. **Partition 2 (Kernel / App / FreeRTOS)**: `dst_flash = 0x08133000` (contains reset vector), size ~1.6 MB
3. **Partition 3 (Tensilica HiFi DSP Audio Pipeline)**: `dst_flash = 0x0832C000`, size ~80 KB
4. **Partition 4 (NVDM Factory Defaults & Audio Calibration)**: `dst_flash = 0x08450000`, size ~300 KB

### Dual-Bank Flash Safety Model
The following describes the AB1568. It has not been confirmed for the Maxwell 2 chips. The Airoha AB1568 employs a **dual-bank flash architecture**:
- The current operating firmware runs from **Bank A**.
- Firmware updates stream sequentially into the inactive **Bank B**.
- The bootloader verifies every partition hash (`TLV 0x0014`) and the outer SHA-256 before swapping the active bank flag.
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
Decompresses the LZMA payload and extracts every partition declared in the package as raw binary files for inspection.

### 5. FOTA Transmission Simulation (Dry-Run Mode)
```bash
audezeforlinux firmware simulate path/to/firmware.bin --target headset
```
Simulates the entire FOTA block fragmentation layout across 62-byte HID reports without transmitting to physical hardware.

### 6. FOTA Flashing Engine
```bash
# Dry-run execution (simulates chunking and runs the package checks):
audezeforlinux firmware flash path/to/firmware.bin --target dongle --dry-run

# Live physical flash (currently refused for every chip, see section 3):
audezeforlinux firmware flash path/to/firmware.bin --target dongle --confirm-risk [--allow-downgrade]
```
The engine streams the package as 56-byte `0x15 0x5A … 0x1C03` blocks over HID Report `0x06`. It does **not** yet send any start or commit commands, or read the device's `0x15 0x5B` acknowledgements, because those steps are not known. They will be added from USB captures of the official updater, not guessed.

Live flashing **fails closed**. Every check runs before the `CONFIRM` prompt, and anything that can't be read is a refusal:

| Check | Refused when |
|---|---|
| Package | Outer SHA-256 or any partition hash fails; chip id unknown or built for the other target |
| Protocol | The chip isn't marked as verified in `KNOWN_CHIP_IDS` (currently none are) |
| Connection & route | No live connection; `--target dongle` while connected over direct USB-C. Through a dongle, route `0x00` reaches the dongle and `0x80` the headset; over USB-C, `0x00` is the headset |
| Installed version | Can't be read, or isn't a known release (so the firmware track can't be confirmed) |
| Firmware track | Package track (regular / ANC Maxwell 2 / Maxwell 1) differs from the device's |
| Downgrade | Package is older than the installed version, unless `--allow-downgrade` is passed. The prompt then asks you to type `DOWNGRADE` |
| Battery (headset) | Can't be read, or below 50% |

During the transfer, Ctrl-C or a failed write stops it and reports how many chunks were sent (exit code 3). The tool says to leave the device powered and check `firmware status`. The AB1568 dual-bank fallback described in section 4 has **not** been verified by this tool, so an interrupted transfer is not guaranteed to be safe.

After the transfer, the target's version is read back (polling while the device reboots). The flash only counts as successful if it reports the package version. Otherwise the CLI exits with code 4.

The live path is covered by tests against a fake device (`TestFotaLiveHarness` in `tests/test_protocol.py`), which enable live writes only inside each test.
