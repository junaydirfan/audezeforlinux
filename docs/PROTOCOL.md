# Audeze Maxwell 2 Protocol Specification

This document details the communication protocol between host software and the Audeze Maxwell 2 headset/dongle over USB HID.

> [!NOTE]
> Every command and structure is explicitly marked as either **CONFIRMED** (verified through physical hardware report descriptors, live hardware tests, or byte-for-byte protocol captures) or **HYPOTHESIS** (derived from static binary disassembly of Audeze HQ 1.2.48 or inferred from Maxwell 1 behavior).

---

## 1. Transport Layer: USB HID Framing

### 1.1 Interface & Endpoints [CONFIRMED]
- **Interface**: the HID interface whose report descriptor declares the vendor collection **usage page `0xFF13`** (usage 1, report IDs `0x06` / `0x07`). Its interface number isn't fixed: interface 5 on the Maxwell 2 dongle with the headset on, interface 0 on the same dongle with the headset off (it re-enumerates with fewer audio interfaces), interface 0 on the Maxwell 2 headset over USB-C, and interface 0 on the original Maxwell dongle (per HeadsetControl). So the transport selects nodes by usage page (from `/sys/class/hidraw/*/device/report_descriptor`), not by interface number.
- **Stuck dongle** [OBSERVED in the field, both generations]: a dongle can keep enumerating, playing audio and applying writes while answering every query with an empty (`len 0`) input report, until it is replugged (HeadsetControl issues #460, #573). `audezeforlinux doctor` detects this: the dongle doesn't even answer a query addressed to itself (route `0x00`), which a dongle with its headset switched off still does.
- **Driver**: Linux `usbhid` / `hidraw`.
- **Interrupt IN Endpoint**: `0x82` (EP 2 IN), 64-byte max packet size, 1ms polling interval.
- **Output Transfer**: HID Output Reports via Interrupt OUT or Control Endpoint (SET_REPORT).

### 1.2 Report Structure [CONFIRMED]
All communication uses fixed 62-byte HID reports (1 byte Report ID + 61 bytes payload):

#### Host-to-Device (Output Report 0x06)
| Offset | Field | Value / Meaning | Status |
|:---:|:---|:---|:---:|
| `0x00` | Report ID | `0x06` | CONFIRMED |
| `0x01` | Chunk Length | Length of RACE packet data in this chunk (1..59) | CONFIRMED |
| `0x02` | Routing Byte | `0x80` = Wireless Dongle transport<br>`0x00` = USB-C Direct Headset transport | CONFIRMED |
| `0x03..` | RACE Payload | Slice of RACE frame (up to 59 bytes) | CONFIRMED |
| Pad | Zero Padding | Padded with `0x00` to 62 bytes total | CONFIRMED |

#### Device-to-Host (Input Report 0x07)
| Offset | Field | Value / Meaning | Status |
|:---:|:---|:---|:---:|
| `0x00` | Report ID | `0x07` | CONFIRMED |
| `0x01` | Chunk Length | Length of RACE packet data in this chunk (1..59) | CONFIRMED |
| `0x02` | Route / Metadata | Status / routing indicator | CONFIRMED |
| `0x03..` | RACE Payload | Slice of RACE response frame (up to 59 bytes) | CONFIRMED |

---

## 2. Protocol Engine: Airoha RACE Framing

The Audeze Maxwell series runs an Airoha Bluetooth/DSP SoC (AB156x family) using the **RACE** (Remote Access Control Engine) protocol.

### 2.1 Packet Header Layout [CONFIRMED]
Every reassembled RACE packet begins with a 6-byte header:

```
+------------+------------+--------------------+--------------------+-----------------------+
| Sync Byte  | Direction  | Length (LE16)      | Command ID (LE16)  | Payload               |
| 0x05       | 0x5A/5B/5D | len(Payload) + 2   | Opcode             | 0..N bytes            |
+------------+------------+--------------------+--------------------+-----------------------+
```

- **Sync Byte** (`0x05`): Protocol identifier.
- **Direction**:
  - `0x5A`: Host Request ($\text{Host} \rightarrow \text{Device}$).
  - `0x5B`: Synchronous Response / Acknowledgment ($\text{Device} \rightarrow \text{Host}$).
  - `0x5D`: Asynchronous Notification / Telemetry event ($\text{Device} \rightarrow \text{Host}$).
- **Length**: 16-bit little-endian integer. Equal to `len(Payload) + 2` (length of Command ID + Payload). The total RACE packet size is `Length + 4`.
- **Command ID**: 16-bit little-endian opcode identifying the operation.

---

## 3. Command Reference

### 3.1 Status & Telemetry

#### Read Active Preset Index [CONFIRMED]
Reads which EQ preset is currently active on the headset.
- **Request Opcode**: `0x0901`
- **Request Bytes**: `05 5A 04 00 01 09 00 00`
  - Command: `0x0901`
  - Sub-command: `0x00` (Preset Index)
- **Response Format**: `05 5B 06 00 01 09 00 00 <reserved> <index>`
- **Preset Indices**:
  - `1`: Audeze (Default)
  - `2`: Treble Boost
  - `3`: Bass Boost
  - `4`: Immersive
  - `5`: Competition
  - `6`: Footsteps
  - `7`: Preset 1 (Custom Slot 0)
  - `8`: Preset 2 (Custom Slot 1)
  - `9`: Preset 3 (Custom Slot 2)
  - `10`: Preset 4 (Custom Slot 3)

#### Select Active Preset [CONFIRMED]
Switches the active preset on the headset.
- **Request Opcode**: `0x0900`
- **Request Format**: `05 5A 05 00 00 09 00 00 <index>`
- **Response Format**: `05 5B 05 00 00 09 00 00 <status>` (status `0x00` = success)

#### Battery Level Query [CONFIRMED]
Queries the headset's current battery percentage.
- **Request Opcode**: `0x0CD6`
- **Request Bytes**: `05 5A 03 00 D6 0C 00`
- **Immediate Response (ACK)**: `05 5B 03 00 D6 0C 00`
- **Asynchronous Notification**: `05 5D 05 00 D6 0C <status> <role> <level>`
  - `status`: `0x00` = OK. `role`: `0x00` (only value seen).
  - `level`: Integer `0..100` representing battery percentage.
- **Error ACK**: `05 5B 03 00 D6 0C 02`: no notification follows. Seen for a request byte of `0x01`, and over the dongle on route `0x00`.
- **Charging state** [CONFIRMED on 3329:4B28 + 4B2E]: no RACE field or input report carries it (input reports `0x03`, `0x04` and `0x0A` read all zero while charging). The headset does enumerate as its own USB device (e.g. `4B2E`) only while a USB-C data cable powers it, so that node's presence means it is charging. It keeps answering status queries over USB even when switched off; the dongle gets no reply then.
- **Re-enumeration**: switching the headset on or off makes both the headset and the dongle re-enumerate, often with swapped `/dev/hidrawN` numbers. An open file descriptor goes dead (`ENODEV` on write) and the device has to be reopened.
- The level read just before and after a power cycle while charging can differ a lot (52% → 70% observed), so treat a single reading as approximate.

#### Firmware Version Query [CONFIRMED]
Queries the firmware version string.
- **Request Opcode**: `0x1C07`
- **Request Bytes**: `05 5A 03 00 07 1C 00`
- **Targeting**:
  - Sent with routing byte `0x00` over dongle: queries the Dongle's firmware.
  - Sent with routing byte `0x80` over dongle: queries the Headset's firmware.
- **Immediate Response (ACK)**: `05 5B 03 00 07 1C 00`
- **Asynchronous Notification**: `05 5D <len_lo> <len_hi> 07 1C <str_len> <ASCII text>`

---

### 3.2 Headset Controls & Settings

Controls are accessed via two command families:
1. **Settings Group (Opcode `0x0901` Read / `0x0900` Write)**
2. **Pair Controls Group (Opcode `0x2C83` Read / `0x2C82` Write)**

#### Settings Group (0x0901 / 0x0900)
- **Read Request**: `05 5A 04 00 01 09 <sub> 00`
- **Read Reply**: `05 5B 06 00 01 09 <sub> 00 <flags> <value>`
- **Write Request**: `05 5A 05 00 00 09 <sub> 00 <value>`
- **Write Reply**: `05 5B 05 00 00 09 <status> 00 <value>`

| Subcommand | Function | Type | Range / Values | Status |
|:---:|:---|:---:|:---|:---:|
| `0x00` | Preset Index | R/W | `1..10` | CONFIRMED |
| `0x20` | Voice Prompt Level | R/W | `0..MAX` | CONFIRMED |
| `0x22` | Microphone Physical Mute | Read-only | `0x00` = Muted, `0xFF` = Unmuted | CONFIRMED |
| `0x24` | AI Noise Suppression | R/W | `0` = Off, `1` = Low, `2` = High | CONFIRMED |
| `0x28` | Volume Limiter | R/W | `136` = Limiter ON, `142` = Limiter OFF | CONFIRMED |
| `0x2C` | Sidetone Level | R/W | `0..MAX` | CONFIRMED |
| `0x2D` | Max Sidetone Query | Read-only | Hardware max level (e.g. 31 or 100) | CONFIRMED |
| `0x29` | Left Channel Gain | R/W | `0..150` (Safety cap, excludes 136/142) | CONFIRMED |
| `0x2A` | Right Channel Gain | R/W | `0..150` (Safety cap, excludes 136/142) | CONFIRMED |

#### Pair Controls Group (0x2C83 / 0x2C82)
- **Read Request**: `05 5A 04 00 83 2C <sub> 00`
- **Read Reply**: `05 5B 06 00 83 2C <status> <sub> 00 <value>`
- **Write Request**: `05 5A 05 00 82 2C <sub> 00 <value>`

| Subcommand | Function | Range / Values | Status |
|:---:|:---|:---|:---:|
| `0x01` | Auto Shutdown Timer | `0` = Never, `300` = 5m, `600` = 10m, `1200` = 20m | CONFIRMED |
| `0x07` | Sidetone Toggle | `0` = Sidetone Disabled, `1` = Sidetone Enabled | CONFIRMED |
| `0x0B` | Game / Chat Balance Mix | `0..20` (`10` = 50% Game / 50% Chat center) | CONFIRMED |

---

### 3.3 Custom Equalizer Architecture

The Audeze Maxwell stores custom EQ in Non-Volatile Device Memory (NVDM) across 4 custom preset slots (`0..3`, mapping to UI Presets 7..10).

#### NVDM Keys [CONFIRMED]
- **UI Slider Records**: Keys `0xEF00` to `0xEF03` (Slot 0..3)
- **DSP Filter Coefficients**: Keys `0xE42C` to `0xE42F` (Slot 0..3)

#### Read NVDM Key (Opcode `0x0A0C`) [CONFIRMED]
- **Request Format**: `05 5A 06 00 0C 0A <key_lo> <key_hi> <max_len_lo> <max_len_hi>`
- **Example (Read Custom 1 UI)**: `05 5A 06 00 0C 0A 00 EF E8 03` (key `0xEF00`, max 1000 bytes)
- **Example (Read Custom 1 DSP)**: `05 5A 06 00 0C 0A 2C E4 E8 03` (key `0xE42C`, max 1000 bytes)
- **Response Format**: `05 5B <len_le16> 0C 0A <status> <key_le16> <data_len_le16> <data...>`

#### Write NVDM Key (Opcode `0x0A0D`) [CONFIRMED]
- **Request Format**: `05 5A <len_le16> 0D 0A <key_le16> <data...>`
- **Response Format**: `05 5B 05 00 0D 0A <status> <key_le16>`

#### Live Temporary EQ Apply (Opcode `0x0E03`) [CONFIRMED on Maxwell 1 / HYPOTHESIS on Maxwell 2]
Applies DSP coefficients immediately in RAM without writing to flash.
- **Request Format**: `05 5A <len_le16> 03 0E 00 <coefficients>`
- **Response Format**: `05 5B 03 00 03 0E <status>`

#### Storage Capacity Query (Opcode `0x0A03`) [CONFIRMED]
Checks if flash has space for an 844-byte coefficient block.
- **Request Bytes**: `05 5A 04 00 03 0A 4C 03` (844 = `0x034C`)
- **Response Format**: `05 5B 03 00 03 0A <flag>` (`0x01` = fits, `0x00` = insufficient space)

---

### 3.4 EQ Data Formats

#### 1. UI Slider Record Layout (`0xEF00 + slot`) [CONFIRMED]
Total length: 185 bytes (5-byte header + 10 bands $\times$ 18 bytes):
- **Header**: 5 bytes.
- **10 Filter Bands** (each 18 bytes, little-endian):
  - `Offset 0..1`: Band tag (`u16`, 0..9)
  - `Offset 2..5`: Center frequency in Hz $\times$ 100 (`i32`, e.g. 100000 for 1000 Hz)
  - `Offset 6..9`: Gain in dB $\times$ 100 (`i32`, e.g. +300 for +3.0 dB, -450 for -4.5 dB)
  - `Offset 10..13`: Q / bandwidth $\times$ 100 (`i32`, e.g. 141 for Q=1.41)
  - `Offset 14..17`: Reserved (`i32`)

Standard 10 Graphic Bands:
1. 32 Hz
2. 64 Hz
3. 125 Hz
4. 250 Hz
5. 500 Hz
6. 1000 Hz (1 kHz)
7. 2000 Hz (2 kHz)
8. 4000 Hz (4 kHz)
9. 8000 Hz (8 kHz)
10. 16000 Hz (16 kHz)

#### 2. DSP Biquad Coefficients (`0xE42C + slot`) [CONFIRMED on Maxwell 2 3329:4B28]
This record is what shapes the sound: writing only the slider record changes nothing audible. A flat record regenerated from the layout below matches the device byte for byte.

Fixed size: **844 bytes**: header `04 00 00 00`, then one 210-byte block per sample rate.

| Rate ID | Sample rate |
|---|---|
| `1` | 44.1 kHz |
| `2` | 48.0 kHz |
| `5` | 88.2 kHz |
| `6` | 96.0 kHz |

Each block holds:
- `u16` rate ID, `u16` `0x0067`, `u16` stage count (10)
- 10 stages of 5 words (`b0 b1 b2 a1 a2`, `a0` = 1), with 23/22/23/22/23 fractional bits
- 1 gain word with 18 fractional bits

Each word is a signed 24-bit value stored in 4 bytes as `mid hi 00 lo`. For example, `ff 7f 00 ff` is `0x7FFFFF`, which is 1.0 with 23 fractional bits.

The stages are RBJ peaking filters at the 10 graphic band centers with Q = 2. The slider record's `q` field is the bandwidth in Hz (f/2), not the filter Q. In a flat record, stage 0's numerator is scaled by 1/1.19 and the gain word is 1.19, so the net level is 0 dB. Curves that boost must be lowered as a whole, and numerators that overflow the word range are scaled down and made up in the gain word.

Other keys seen on Maxwell 2: `0xE42A` is an 844-byte non-flat curve, and `0xE429` and `0xE42B` are 124-byte one-stage records in the same format. Their purpose is unknown, and they don't change with the active preset. The factory presets' filters are not in this key range.
