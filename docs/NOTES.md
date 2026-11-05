# Project Notes & Decision Log

## Objectives
- Reverse-engineer and provide a safe, open-source Linux CLI control tool (`maxwell-ctl`) for the Audeze Maxwell 2 headset and USB dongle.
- Primary target: CachyOS / Arch Linux with PipeWire & WirePlumber.
- Language: Python 3 prototype (standard library direct sysfs/hidraw access, no external dependencies).

## Core Safety Principles
1. **Never send arbitrary or fuzzed bytes**: Only replay commands explicitly marked `CONFIRMED` in `docs/PROTOCOL.md` or manually approved.
2. **Explicit write controls & `--dry-run`**: Any action mutating hardware state must be clearly surfaced and support a dry-run flag.
3. **No firmware or bootloader modifications in Phases 1-3**: Firmware reading only.
4. **No unauthorized PipeWire / WirePlumber audio stack mutations**: Report diffs or suggestions without automated invasive changes.
5. **State backup**: Read and store state prior to writing any changes so they can be restored.

---

## Log of Findings and Decisions

### Phase 1: Discovery (Verified)
- **Status**: Completed device enumeration and descriptor parsing.
- **Hardware Found**:
  - Device: Audeze Maxwell 2 (Xbox Dongle variant)
  - USB VID:PID: `3329:4B28`
  - Linux Kernel Audio: `snd-usb-audio` (Interfaces 0, 1, 2, 3, 4)
  - Linux Kernel HID: `usbhid` (Interface 5) mapped to `/dev/hidraw1`
- **Verified HID Architecture**:
  - Report Descriptor length: 175 bytes
  - Vendor collection: Usage Page `0xFF13`
  - Command output: Report ID `0x06` (61 bytes payload -> 62 bytes total HID report)
  - Reply input: Report ID `0x07` (61 bytes payload -> 62 bytes total HID report)
  - Telemetry: Report ID `0x03` (13 bytes), `0x04` (1 byte), `0x0A` (16 bytes)
  - Telephony: Report ID `0x05` (Hook, Phone Mute)
  - Consumer: Report ID `0x0C` (Volume buttons)

### Phase 2: Protocol Analysis (Completed)
- **Status**: Completed protocol analysis, formal specification in `docs/PROTOCOL.md`, Python protocol layer, and offline unit tests.
- **Verified Protocol Standard**:
  - Transport: Fixed 62-byte HID reports with Report ID 0x06 (Host -> Device) and 0x07 (Device -> Host).
  - Routing: Dongle routing flag `0x80` at report offset 2; direct USB-C headset uses `0x00`.
  - Framing: Airoha RACE protocol (`0x05 0x5A <len_le16> <cmd_le16> <payload>`).
  - Active Preset: Read via `0x0901`, Sub `0x00` (`05 5A 04 00 01 09 00 00`). Write via `0x0900`, Sub `0x00`.
  - Battery Query: Opcode `0x0CD6` (`05 5A 03 00 D6 0C 00`), notified asynchronously via direction `0x5D`.
  - Firmware Query: Opcode `0x1C07` (`05 5A 03 00 07 1C 00`), notified asynchronously via direction `0x5D`.
  - Controls: Subcommands for Sidetone (`0x07`/`0x2C`), Chat Mix (`0x0B`), Limiter (`0x28`), AI Noise Suppression (`0x24`), Mic Mute (`0x22`).
  - Equalizer: NVDM storage keys `0xEF00..0xEF03` for 10-band slider records (185 bytes each, little-endian); keys `0xE42C..0xE42F` for 844-byte DSP biquads.

### Phase 3: Safe Client & CLI (Live Verified)
- **Status**: Live hardware communication verified without root via udev rules.
- **Key Breakthrough (HID Input Mechanism)**:
  - The Audeze dongle Interface 5 has only 1 USB endpoint (`0x82` Interrupt IN).
  - Responses to commands are retrieved via USB Control Endpoint 0 (`GET_REPORT`), which in Linux `hidraw` corresponds to `ioctl(fd, HIDIOCGINPUT(62), buf)`.
  - Standard `os.read(fd)` was timing out because the kernel only queues reports to `read()` when pushed autonomously over Interrupt IN.
  - Adding `HIDIOCGINPUT` polling resolved the timeout completely.
- **Live Hardware Telemetry Verified**:
  - Active Preset: `[4] Immersive`
  - Battery Level: `94%`
  - Firmware Version: `v1.0.5.7.4`
  - Hardware Mic Switch: `UNMUTED`
  - Sidetone: `OFF (Level 5/31)`
  - Game/Chat Balance: `50% Game / 50% Chat`
  - AI Noise Reduction: `Low`
  - Volume Limiter: `Enabled (85 dB)` (not trustworthy: over USB-C the Maxwell 2 answers `0x28` with a fixed `0x80`, see `PROTOCOL.md` 3.2 and issue #9)
  - Custom EQ Slots 1..4: All 10 bands successfully read from NVDM keys `0xEF00..0xEF03`.

### Interactive TUI & Project Renaming Update
- **Repo Renaming**: Project officially named `audezeforlinux` (executable `audezeforlinux` with `maxwell-ctl` backward compatibility alias; git remote updated to `https://github.com/junaydirfan/audezeforlinux.git`).
- **Terminal Scrolling & Escape Sequence Fix**:
  - Problem 1: `select.select()` 40ms inter-byte timeout in PTY/IDE terminals caused arrow escape sequences (`\x1b[A`) to be misparsed as raw `ESC`, killing the dashboard immediately.
  - Problem 2: `console.clear()` in non-curses mode repeatedly dumped 35+ lines into the terminal, pushing viewport content into scrollback history and causing continuous downward scrolling.
  - Problem 3: Mouse clicking on UI elements was unhandled.
  - Solution: Rebuilt `maxwell2/interactive.py` using Python standard library `curses` (`curses.wrapper`):
    - **Alternate Screen Buffer**: Uses `\x1b[?1049h`, completely disabling terminal scrollback and eliminating auto-scroll down.
    - **Native Keypad Handling**: Uses `stdscr.keypad(True)` with `ESCDELAY=25`, natively decoding `curses.KEY_UP`, `KEY_DOWN`, `KEY_LEFT`, `KEY_RIGHT` without escape sequence races.
    - **Mouse Click Support**: Integrated `curses.mousemask()` registering on-screen `ClickableRegion` boxes. Clicking view tabs, preset rows, sidetone toggles, sliders, EQ bands, and save buttons works natively.
    - **Studio Grayscale Theme**: Minimalist high-contrast monochrome design with 10-band spectrum analyzer and on-screen hardware save (`s`).
