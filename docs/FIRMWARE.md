# Audeze Maxwell 2 Firmware Architecture & Update Specification

## 1. Executive Summary & Safety Policy

Firmware updates are the highest-risk hardware operation on the Audeze Maxwell 2. An incomplete, corrupted, or out-of-sync firmware write can render the headset or USB wireless dongle inoperable ("bricked"), requiring factory service or low-level JTAG/SWD hardware recovery.

### Safety Invariants
1. **Never Auto-Update**: No update will ever begin automatically or in the background.
2. **Dual-Target Parity Check**: The Audeze Maxwell system consists of **two independent computational nodes** (the USB Dongle and the Headset). Their firmware versions must match. Flashing one without the other can break the proprietary 2.4 GHz wireless pairing link.
3. **Power & Transport Prerequisite**: The headset must report **$\ge 50\%$ battery**, and firmware writes to the headset should ideally take place over direct USB-C rather than over the air (FOTA).
4. **Mandatory Checksum Verification**: Every firmware package must be parsed, uncompressed, and cryptographically verified before any transport packets are sent.
5. **Nothing Installs Before Commit**: The package is written to a separate staging area and read back in full before the device is asked to install it (section 3.2). `--stage-only` runs everything except the install.

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
| `AB1565_ULL2_DONGLE` | Maxwell 2 dongle | **Experimental** (`--experimental`) |
| `AB1585_Headset` | Maxwell 2 headset | **Experimental** (`--experimental`) |

The Maxwell 2 ids come from the official Maxwell 2 packages (v1.0.5.7.4 and the v1.0.2.60.2 rollback) published in Audeze's `maxwell-2-fw.json` list. The container format below parses and verifies on them.

"Experimental" means that the update commands in section 3.1 answer correctly on a real Maxwell 2 (both chips, read-only requests), but a complete update has not been run through yet. No chip is marked verified (`KNOWN_CHIP_IDS`), so a live update needs `--experimental`. AB1568 (Maxwell 1) packages are refused for live flashing, and so are packages with an unrecognised or missing chip id.

Before November 2026 the live path sent the package under `0x1C03`, which is Airoha's *stop update* command, and it sent no start, erase or commit. It could never have updated a device, and it never ran against one.

### Firmware tracks: regular vs ANC Maxwell 2
Audeze publishes the Maxwell 2 ANC firmware as a **separate track** (`mw2-anc/` in the official bucket, `Maxwell2ANC_*` packages; v1.0.5.6 is listed in `maxwell-2-anc-rollback.json`). The ANC packages report **the same** chip ids (`AB1565_ULL2_DONGLE`, `AB1585_Headset`) and design names as regular Maxwell 2 packages, but the images differ (the v1.0.5.6 ANC headset image is 2,625,444 bytes, the v1.0.5.7.4 regular one 2,316,701 bytes). So the chip id can't tell the tracks apart:

- A package's track (`firmware_track` in `firmware inspect`) comes from a table of known official package hashes, then from the `Maxwell2ANC` file name prefix.
- The device's track is inferred from its installed version. Only versions known on exactly one track count. `firmware check` falls back to the regular track for unknown versions; the flash path does not.
- Preflight refuses a package whose track differs from the device's. A live flash of a Maxwell 2-family chip also needs the device track to be known.
- The ANC catalog is incomplete (the newest ANC release is unknown), so `firmware check` says so instead of reporting "up to date". The ANC dongles are `3329:4BA8` (Xbox) and `3329:4BA9` (PlayStation); a live flash through either treats the device as ANC regardless of version. The ANC USB-C ID is still unknown.

### 3.1 Update commands
The commands follow [race-toolkit](https://github.com/auracast-research/race-toolkit) (`librace/`), which implements Airoha's update flow and reports it working on other Airoha headphones. The implementation is in `maxwell2/fota.py`. RACE packets have the usual header (`head`, `type`, `length` LE16 = payload + 2, `cmd` LE16). Update commands use head **`0x15`** rather than the usual `0x05`, though some, marked below, keep `0x05`. Replies come back with the same command id and type `0x5B`, and their first payload byte is a status (0 = success).

| Step | Cmd | Head | Payload | Reply |
|---|---|---|---|---|
| Query staging partition | `0x1C00` | `0x05` | `00` | `<BHII`: status, ?, address, length |
| Start | `0x1C08` | `0x15` | `01 00` | status |
| Start transaction | `0x1C0A` | `0x15` | none | status |
| Set state | `0x1C06` | `0x15` | `01 02`, then `10 02` | status |
| Erase | `0x0404` | `0x05` | `<BII`: storage type 0, **length, then address** | status |
| Write pages | `0x0402` | `0x15` | `<BB` type 0, page count (1–3), then per page `<BI` checksum, address + 256 bytes | `<BBBI`: status (`0x0A` = checksum error), 0, pages, address |
| Read page | `0x0403` | `0x05` | `<BBI`: type 0, `01` (one 256-byte page), address | status, type, 2 bytes, address LE32, 256 bytes |
| Integrity check | `0x1C01` | `0x15` | `01 00 00` | status |
| Set state | `0x1C06` | `0x15` | `11 02` | status |
| Commit | `0x1C02` | `0x15` | `00` | status; the device then reboots and installs |

`0x1C03` is *stop update*. The host never sends it, but **the device does**. On the Maxwell 2 headset, an update session left idle ends on its own; page reads don't count as activity. The next update command then gets `15 5a 05 00 03 1c 00 80 04` (a stop) instead of a reply. Seen after the ~70 s read-back, when the integrity check got a stop. So the engine restarts the session (start, start transaction, both states) after the read-back. This leaves the staged data intact. A stop received at any point fails the run at once rather than waiting for a timeout. The page checksum is CRC-8 (polynomial `0x31`, initial value 0, MSB first) with the result bit-reversed: 256 zero bytes give `0x00` and 256 `0xFF` bytes give `0xB4`.

### 3.2 How an update works
The device has a **staging (FOTA) partition**, separate from the running firmware. The host erases it, writes the `.bin` package into it unchanged (still compressed), and the device checks it. Nothing that runs changes until the **commit**. Then the device reboots, and its bootloader unpacks the staged package over the firmware partitions listed in the package. So a transfer that stops before the commit, for whatever reason, leaves the device on its current firmware. This is a staging area, not the dual-bank (A/B) scheme this document used to describe.

Staging partitions reported by a Maxwell 2 (Xbox v1.0.5.7.4) with the query above:

| Node | Staging partition | Size | Official package |
|---|---|---|---|
| Headset (AB1585, SDK `IoT_SDK_for_BT_Audio_V5.4.0.AB158x`) | `0x004BC000` | `0x323000` (3.2 MB) | 2,341,532 bytes (9,147 pages) |
| Dongle (AB1565) | `0x00263000` | `0x194000` (1.6 MB) | 1,227,746 bytes (4,796 pages) |

The headset's staging partition begins exactly where package partition 4 (`0x471000` + `0x4B000`) ends and ends before partition 5 (`0x7E0000`), so it doesn't overlap anything the package installs. Before erasing, preflight checks this against every partition in the package being flashed.

Routing is the same as for settings. Through the dongle, route `0x00` reaches the dongle and `0x80` is relayed to the headset; the relay carries the update commands too. Over USB-C, route `0x00` is the headset. A page read takes about 8 ms over USB-C, so reading back a whole headset package takes about 75 s.

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

### What protects an interrupted update
See section 3.2: the package is staged, and nothing is installed until the commit. Earlier versions of this document described a dual-bank (A/B) scheme with automatic fallback. That was never confirmed and doesn't match what the Maxwell 2 reports. The risky window is from the commit until the bootloader finishes installing. How the bootloader recovers from power loss inside that window isn't known, so don't unplug or power off the device then.

The package's integrity is protected by plain SHA-256 hashes (outer and per partition). No signature is involved.

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
Evaluates installed firmware against Audeze's live update manifest, the known baseline catalog, or a downloaded official `.bin` package.

> [!NOTE]
> For the regular Maxwell 2, `firmware check` reads `maxwell-2-fw.json` from Audeze's update bucket (the manifest the official Windows app reads). It falls back to the local catalog if that read fails, and says so. `--offline` skips the network. No current manifest is known for the ANC track or the Maxwell 1, so those are always checked against the local catalog. Through a dongle, both the dongle and headset versions are read, and the older one decides whether an update is due. An ANC or Maxwell 1 USB product ID picks the track directly. If the device doesn't report a version, the check says so rather than reporting "up to date".

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

### 5. Firmware Update
```bash
# Simulation (the default without --confirm-risk): runs the package checks and plans the transfer
audezeforlinux firmware flash path/to/firmware.bin --target headset

# Live, stage only: erase, write, read back and device check, then stop. Nothing is installed.
audezeforlinux firmware flash path/to/firmware.bin --target headset --confirm-risk --experimental --stage-only

# Live, full update
audezeforlinux firmware flash path/to/firmware.bin --target headset --confirm-risk --experimental [--allow-downgrade]
```

| Option | Effect |
|---|---|
| `--confirm-risk` | Write to the device. Without it the update is only simulated (`--dry-run` also forces simulation) |
| `--experimental` | Needed for the Maxwell 2 chips until a complete update has been verified (section 3) |
| `--stage-only` | Stop after the integrity check, without committing. A safe way to test the transfer on your own device |
| `--pages-per-write 1-3` | 256-byte pages per write request. The default is 1; up to 3 is faster if the device accepts it |
| `--allow-downgrade` | Allow a package older than the installed version. The prompt then asks for `DOWNGRADE` |

A live run follows section 3.1. It takes sole use of the device (no other instance can open it during the update) and stops instead of reconnecting if the device disappears. Before starting, it saves the headset settings and custom EQ to a backup ('backup list'). It writes every page and retries a page up to 3 times on a rejection, a checksum error or a missing reply. Then it reads **every page back** and compares it with the package before asking the device to check it and committing. After the commit it polls the target's version until it reports the package version.

Live flashing **fails closed**. Every check runs before the `CONFIRM` prompt, and anything that can't be read is a refusal:

| Check | Refused when |
|---|---|
| Package | Outer SHA-256 or any partition hash fails; chip id unknown or built for the other target |
| Protocol | The chip isn't verified in `KNOWN_CHIP_IDS` (none are) and isn't an experimental chip run with `--experimental` |
| Connection & route | No live connection; `--target dongle` while connected over direct USB-C |
| Installed version | Can't be read, or isn't a known release (so the firmware track can't be confirmed) |
| Firmware track | Package track (regular / ANC Maxwell 2 / Maxwell 1) differs from the device's |
| Downgrade | Package is older than the installed version, unless `--allow-downgrade` is passed |
| Battery (headset) | Can't be read; still changing; below 50%, or **below 90% while on USB power** (see below) |
| Connection | The device node changed between the checks and the update (unplugged, re-enumerated) |
| Staging partition | Can't be read, is implausible, is smaller than the package, or overlaps any flash range the package installs to |
| Device in use | Another audezeforlinux instance (e.g. the dashboard) has it open |

**Battery readings while charging run high.** Measured on a Maxwell 2 headset: on the charger it read 85%. A minute after unplugging, the reading had slid to 62%; plugged back in, it climbed to 85% again within a minute. Earlier, 34% became 72% within two minutes of plugging in. So while the headset is on USB power (always the case when updating over USB-C), the update needs a 90% reading, roughly 50% real charge. Off the charger it needs 50%. If the power state can't be determined, 90% applies. Charging from a wall adapter can't be detected, so don't update over the dongle while it's on a wall charger. After a plug or unplug, the reading moves by about 1 point every 2 s, so preflight takes readings 3 s apart until two agree, and refuses if the reading hasn't settled within 2 minutes.

**Connection pinning.** The route byte means different devices depending on the node: route `0x00` on the headset's USB node is the headset, but on the dongle it's the dongle. So preflight records the exact node (path, product ID, route). The update refuses to start if it changed since, for example after the headset was unplugged during the `CONFIRM` prompt. No request can reconnect silently mid-run.

**After the commit**, the CLI shows "Installing" with the elapsed time while it polls the target's version for up to 10 minutes. When the package version equals the installed one (a same-version reinstall), a matching version alone proves nothing, because the old firmware reports it too. The device must also have been seen going away (a failed read or a reconnect). Otherwise the result is `no_reboot_seen`.

**Settings after an update.** A full headset update resets the custom EQ slots to flat, but keeps the preset, sidetone, chat mix, noise reduction, mic state and the dongle pairing. Before an update, the CLI saves all settings and custom EQ to a backup. After a confirmed install, it rewrites every custom EQ slot that differs from that snapshot, without changing the active preset, and lists the restored slots. A slot that can't be restored is reported, and the update still counts as successful.

**Reconnecting after the reboot.** The headset or dongle re-enumerates, often both, as the dongle resets with the headset. The transport looks for the same kind of node it had open. That matters after a dongle update with the headset on USB-C: the headset's node would answer a "dongle" version query with its own version. A dongle version only counts when it comes from a dongle node. If a reconnect finds no device at all, the next request tries again.

Exit codes: 1 refused, 3 stopped part-way, 4 committed but the new version couldn't be confirmed. If a run stops before the commit, the message says so and that the device is still on its current firmware. If it stops after the commit (Ctrl-C, or contact lost while the device reboots), it says to leave the device powered and check `firmware status`.

`tests/test_fota.py` runs the engine against a simulated device that keeps a staging area in memory. It checks the exact request sequence, head bytes and routes, and the byte layout of every request (the erase field order in particular). It also covers every way a run can stop: rejected requests, missing replies, checksum errors, wrong data read back, an erase that silently didn't happen, the device disappearing, Ctrl-C, a refused or unanswered commit, and every preflight refusal.

### Verification status on real hardware
| Step | Status |
|---|---|
| Staging partition query, dongle and headset (also relayed through the dongle) | Confirmed, read-only |
| Page read | Confirmed, read-only (the headset's staging area reads all `0xFF`) |
| Start, state, erase, page write, read-back, integrity check (headset over USB-C) | **Confirmed** 2026-11-04: `--stage-only` with the Xbox v1.0.5.7.4 package, 9,147 pages in 193 s (writes about 20 KB/s, read-back about 30 KB/s, so the erase took only seconds). Every page read back identical, integrity check status 0. Settings and version unchanged afterwards, also after a power cycle: the bootloader ignores an uncommitted staged package |
| Commit and install (headset over USB-C) | **Confirmed** 2026-11-04: a full same-version update (Xbox v1.0.5.7.4). The headset rebooted about 3 s after the commit, and the dongle re-enumerated with it. Afterwards the staging area was blank, meaning the bootloader consumed the package (an uncommitted one stays). It reports v1.0.5.7.4, and the dongle pairing survived. **Custom EQ slots were reset to flat**; presets, sidetone, chat mix, noise reduction and mic state were kept. Restore EQ curves from the pre-update backup |
| Stage-only and full update of the dongle (route `0x00` on the dongle node) | **Confirmed** 2026-11-04: stage-only took 105 s (4,796 pages, all identical) and the full update 110 s. The dongle rebooted about 2 s after the commit, answered again within seconds, then dropped off USB once more about 30 s later before settling. Afterwards its staging area was blank, it reports v1.0.5.7.4, the headset answers through it, and the headset's settings and EQ were untouched |
| Headset update relayed through the dongle (wireless) | Not yet run (the staging query, page reads and the staging check work on that route) |

**When does an install count as confirmed?** The bootloader clears the staging area once it has installed the package, while a package that was never committed stays there through reboots. On both chips, the device can answer with its (old) version before the install has run. So the version check also reads the first staging page, and only counts the update as installed once that page no longer holds the package (`still_staged` otherwise). If the staging area can't be read, it falls back to "version matches, plus a reboot was seen when the version didn't change".
