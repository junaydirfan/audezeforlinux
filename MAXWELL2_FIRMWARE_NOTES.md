# Maxwell 2 firmware: issue + PR progress notes

_Last updated: 2026-10-26_

Handoff notes so this work can be picked up in a new conversation.
To resume, open a new chat and say: "Read MAXWELL2_FIRMWARE_NOTES.md and let's continue."

## Background

An outside contributor (Maxwell 2 owner, PlayStation/PC dongle `3329:4B29`,
dongle + headset on v1.0.2.60.2, CachyOS) opened one issue and one PR.

### The PR: Maxwell 2 detected by version tuple

- Commit `91f53ba` by scotthowson, file `maxwell2/firmware.py`, in `check_available_updates()`.
- Change: `is_v2 = "1.0.5" in current_version` →
  `is_v2 = parse_version_tuple(current_version) >= (1, 0, 2)`.
- Bug it fixes: every Maxwell 2 unit on a 1.0.2.x build (all of them are in our own
  `FIRMWARE_CATALOG["maxwell2"]`) was labelled "Audeze Maxwell 1 (Original)" and shown
  the Maxwell 1 release history.

**Review result: correct and safe, recommended to merge.** Checked against every catalog version:

| Version | Old check | New check |
|---|---|---|
| v1.0.5.7.4 | M2 | M2 |
| v1.0.2.60.2 / .59 / .54 / .49 / .53_A | M1 (wrong) | M2 |
| v1.0.1.74 / .63 / .61 / .56 | M1 | M1 |
| "Unknown" / "" | M1 | M1 (unchanged fallback) |

Optional follow-ups (not blocking):
- Add a test for `check_available_updates("v1.0.2.60.2")` in `tests/test_protocol.py`.
- A sturdier approach: pick the catalog by USB PID (`0x4B28`/`0x4B29` = Maxwell 2;
  see `maxwell2/transport.py`) instead of by version string. That needs a signature change.

### The issue: Maxwell 2 packages use different chips

Official Maxwell 2 packages (from Audeze's list at
`https://audeze-maxwell2.s3.us-east-1.amazonaws.com/maxwell-2-fw.json`, used by the
Windows "Audeze" app 1.2.48) all pass `firmware inspect`, but report these TLV 0x0020 chip ids:

| Package | Chip id |
|---|---|
| `Maxwell2_PS_dongle_v1.0.5.7.4.bin`, `Maxwell2_XBOX_dongle_v1.0.5.7.4.bin` | `AB1565_ULL2_DONGLE` |
| `Maxwell2_PS_headset_v1.0.5.7.4.bin`, `Maxwell2_XBOX_headset_v1.0.5.7.4.bin` | `AB1585_Headset` |
| rollback v1.0.2.60.2 (PS dongle/headset) | same ids |

`FotaEngine.preflight_check()` (`maxwell2/firmware.py`, around line 523) hard-codes
`AB1568_Dongle` / `AB1568_Headset`, so flashing a Maxwell 2 is refused with "Target mismatch".

Their questions:
1. Is the RACE 0x15 FOTA framing the same on AB1565_ULL2 / AB1585? Has it been tested on a Maxwell 2?
2. Would we accept a PR that maps the Maxwell 2 chip ids per target?

They offered USB captures of the official app updating a Maxwell 2 once they have a Windows machine.

**Assessment:**
- The report is credible (the data comes from our own `inspect` command).
- The FOTA path in `FotaEngine.execute()` streams 56-byte chunks of the raw `.bin` as
  `0x15 0x5A <len> 0x1C03 <data>` packets. This has **never been verified** on AB1565/AB1585,
  and there's no evidence it matches what the official app does.
- The preflight mismatch is currently the only thing preventing unverified packets from being
  sent to a different chip. **Do not just add the new ids to the allowed list**, because that
  risks bricking devices.
- `docs/FIRMWARE.md` and the docstrings say the whole Maxwell family uses the AB1568. That wording
  should be limited to the Maxwell 1.

## Local changes made (2026-10-26, uncommitted)

Safe groundwork for the issue. Live Maxwell 2 flashing is still blocked:

- `maxwell2/firmware.py`: new `KNOWN_CHIP_IDS` map (chip id → target, live-flash allowed).
  AB1568 ids stay allowed as before. `AB1565_ULL2_DONGLE` / `AB1585_Headset` are recognised and
  checked against the target, but live flashing them raises a clear "not supported yet" error.
  Dry runs work.
- Tightened preflight: unrecognised chip ids are refused, and packages with no chip TLV can only
  be dry-run. (Before, "Unknown" passed straight through to a live flash.)
- `to_dict()` gains `chip_target` / `live_flash_supported`, and `firmware inspect` shows a
  "Live Flash Support" line.
- `docs/FIRMWARE.md`: chip table plus a note that the framing is unverified on Maxwell 2 chips.
- `tests/test_protocol.py`: new `test_fota_preflight_chip_gating` (11 cases). Also fixed the
  existing firmware test: its version TLV length was wrong, so the TLV scan stopped early and the
  partition table was never parsed (the test passed on an empty partition list).
- Did NOT touch the `is_v2` line in `check_available_updates()`, to avoid conflicting with the PR.
- All 15 tests pass (`python -m unittest tests.test_protocol`).

Open question: the AB1568 framing also seems never to have been verified against the official
app (no captures in the repo). Consider whether AB1568 live flashing should also be disabled.

## Maxwell 2 ANC findings (2026-10-26)

Audeze publishes a separate ANC firmware track:
- `maxwell-2-anc-rollback.json` → v1.0.5.6 → `mw2-anc/1.0.5.6/maxwell-2-anc-fw-5.6.json`
  (`Maxwell2ANC_{PS,XBOX}_{dongle,headset}_v1.0.5.6.bin`, dated 20 Aug 2026).
- Regular track: `maxwell-2-fw.json` (v1.0.5.7.4), `maxwell-2-rollback.json` (v1.0.2.60.2).
- The name of the *current* (non-rollback) ANC list is unknown; it's not `maxwell-2-anc-fw.json`.

Inspected the PS ANC dongle and headset packages with `firmware inspect`. Both pass the outer SHA-256
and all partition hashes.

| Package | Chip id | Design | Partitions | Size |
|---|---|---|---|---|
| ANC PS dongle v1.0.5.6 | `AB1565_ULL2_DONGLE` | dongle_ref_design | 2 (flash 0x13000, 0xE2000) | 1,198,488 |
| ANC PS headset v1.0.5.6 | `AB1585_Headset` | headset_ref_design | 6 (0x15000 … 0x7E0000) | 2,625,444 |
| Regular PS dongle v1.0.5.7.4 | `AB1565_ULL2_DONGLE` (per issue) | ? | ? | 1,199,163 |
| Regular PS headset v1.0.5.7.4 | `AB1585_Headset` (per issue) | ? | ? | 2,316,701 |

What this means:
1. **ANC and regular Maxwell 2 use the same chip ids**, so TLV 0x0020 can't tell them apart. The
   design name doesn't either. If Maxwell 2 live flashing is ever enabled, preflight would let a
   regular package go onto an ANC headset (the headset images differ by ~300 KB) or the other way
   round. A track check based on the device identity (USB PID or a firmware query) is needed first.
2. **Wrong update advice:** an ANC unit on v1.0.5.6 is compared against the regular catalog and told
   v1.0.5.7.4 is "available". That's regular, non-ANC firmware. Can't fix this properly without knowing
   how to detect an ANC unit.
3. ANC USB product IDs: issue #3 (2026-10-28) reported the ANC Xbox dongle as `3329:4BA8`
   (product string is the same "Audeze Maxwell XBOX Dongle" as 4B28). Added to `transport.py`
   (`ANC_PIDS`), udev, discover.py; FOTA preflight uses it as the device track. PS ANC dongle and
   USB-C IDs still unknown (4BA9/4BAA would follow the pattern but are unconfirmed, so not added).
4. `docs/FIRMWARE.md` says packages have a fixed 4-partition layout at 0x080xxxxx with the chip TLV at
   0x01EE. These packages have 2 or 6 partitions at different addresses and different TLV offsets.
   The parser copes because it scans TLVs, but the docs (and the "all 4 partitions" wording in
   preflight) are AB1568-specific.

Downloaded copies (scratch, not in repo): `/tmp/claude-1000/-home-junaids--claude-spares-spare-m0lk1A/504fc147-20b4-4c3e-abac-0455e7bd5ab7/scratchpad/anc_bins/`
- dongle sha256 `b16ecc9793d6ab9530dc45805cc486061f302fa389e9215f66d81fb587571a39`
- headset sha256 `2d5149fc5859c8c437b83fe2d4b83b8068ab9d9038ae61704c73640be90c6f9a`

## Flash hardening (2026-10-26)

Found during review:
- The live write path had never worked. `execute()` called `write_race_packet(..., target_route=...)`,
  a parameter that doesn't exist, so it crashed with a TypeError before the first chunk. Nothing was
  ever written to a device.
- The route was inverted: `--target dongle` mapped to 0x80, which is the headset relay. Correct
  mapping: through a dongle, 0x00 = dongle and 0x80 = headset; over USB-C, 0x00 = headset.

Changes:
- `KNOWN_CHIP_IDS`: live flashing is now blocked for **all** chips, AB1568 included.
- `Transport.write_race_packet(packet, is_dongle=None)` takes a per-packet route override.
- `FotaEngine` fails closed on live runs. It refuses if any of these can't be confirmed: connection,
  route (dongle target over USB-C), installed version (read per target), strict device track,
  package track match, downgrade (needs `allow_downgrade`), headset battery (None, error or <50%).
- Ctrl-C → `FotaInterruptedError`, write error → `FotaTransferError`. Both record `chunks_sent` and
  say not to unplug. A cancel before the first chunk says nothing was written.
- Post-flash readback polls the target version: `confirmed`, `version_mismatch` or `no_response`.
  `success` is only true when confirmed.
- CLI: preflight runs before the CONFIRM prompt; new `--allow-downgrade` flag plus a typed
  `DOWNGRADE` prompt; clean errors; exit codes 1 = refused, 3 = transfer stopped, 4 = not verified.
- Tests: `FakeFotaTransport` / `FakeFotaClient` harness, `TestFotaLiveHarness` (live path enabled via
  `mock.patch.dict` inside each test) and `TestFotaBlockedByDefault`. 24 tests pass. Putting the old
  `target_route` call back breaks 5 tests.
- Not done on purpose: no start/commit commands and no ACK reading. Those come from USB captures.
- Not fixed: the interactive dashboard's `c` key still shows a hard-coded "up to date with v1.0.5.7.4"
  message, which is wrong for ANC and older units.
- Note: the user's Maxwell 2 Xbox dongle (3329:4B28) was connected while testing. One `--confirm-risk`
  CLI run opened it but was refused at the first live check, before any read or write.

## Plan / status

- [ ] **Merge the version-tuple PR** (recommended; not done yet as of these notes).
- [ ] Review and commit the local changes above.
- [ ] **Reply to the issue** with the text below and leave it open.
- [ ] Wait for the contributor's USB captures of the official Windows app updating a Maxwell 2.
- [ ] When the captures arrive, compare the official packet layout (commands, chunk size,
      start/erase/commit steps, routing) against `FotaEngine.execute()`.
- [ ] Only after the framing is confirmed: accept or write a PR mapping
      dongle → `AB1565_ULL2_DONGLE`, headset → `AB1585_Headset`.
- [ ] Meanwhile (optional): make preflight *recognise* Maxwell 2 chip ids and give a clear
      "Maxwell 2 flashing not yet supported" error instead of a generic "Target mismatch".
- [ ] Update `docs/FIRMWARE.md` to cover the Maxwell 2 chips.
- [x] ANC catalog track + regular-vs-ANC firmware-track check (done 2026-10-26, see below).
- [x] Corrected the fixed-layout claims in `docs/FIRMWARE.md`.
- [ ] ANC: get `lsusb` output from an ANC owner (product IDs unknown), and find the current
      (non-rollback) ANC release so the ANC catalog can be marked complete.
- [ ] Add the regular Maxwell 2 package hashes to `KNOWN_PACKAGE_TRACKS` (only the two PS ANC
      hashes are in it so far).

## ANC track work (2026-10-26)

- `FIRMWARE_CATALOG["maxwell2_anc"]`: v1.0.5.6, `baseline_complete: False`. `firmware check` reports
  "LATEST RELEASE UNKNOWN" instead of "up to date".
- `detect_device_track(version, strict=False)`: maxwell1 / maxwell2 / maxwell2_anc from the installed
  version. Non-strict mode falls back to regular Maxwell 2 for unknown versions (update advice only).
  Strict mode (the flash path) returns None unless the version is known on exactly one track.
- `FirmwarePackage.firmware_track`: known official sha256 (`KNOWN_PACKAGE_TRACKS`), then the
  `Maxwell2ANC` file name prefix, then the chip id. Shown in `firmware inspect`.
- `check_available_updates(..., package_path)` reports `WRONG FIRMWARE TRACK` when the package track
  differs from the device track.
- `FotaEngine(device_track=...)`: preflight refuses cross-track packages. A live flash of a Maxwell 2
  chip also requires a known device track. The `firmware flash` CLI reads the device version (strict)
  before live flashes.
- Tests: `test_firmware_track_detection`, `test_fota_preflight_track_gating`; 17 tests pass.
- Checked against the real ANC packages: ANC package on v1.0.5.6 → matches; on v1.0.5.7.4 → WRONG
  FIRMWARE TRACK; a renamed copy is still identified as ANC by its hash.

## Suggested issue reply (copy/paste)

> Thanks for the detailed report and for running `firmware inspect` on the official packages, that's really helpful. Merged your version-detection PR.
>
> To answer your questions:
> 1. No, the flash path has only been built against the AB1568 and hasn't been tested or captured on a Maxwell 2. So the preflight refusal is doing its job for now.
> 2. Yes, I'd welcome a PR mapping the AB1565_ULL2 / AB1585 chip ids, but I'd like to confirm the FOTA framing first, since a wrong packet layout could brick a device. Your USB captures of the official app updating a Maxwell 2 would be exactly what's needed. Please attach them here when you have a Windows machine.
>
> In the meantime I've made the tool recognise the AB1565_ULL2_DONGLE / AB1585_Headset ids: `firmware inspect` now reports them, and `firmware flash --dry-run` works on Maxwell 2 packages. Live flashing of those chips is refused with a clear message until the framing is confirmed.
