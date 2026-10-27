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

## Suggested issue reply (copy/paste)

> Thanks for the detailed report and for running `firmware inspect` on the official packages, that's really helpful. Merged your version-detection PR.
>
> To answer your questions:
> 1. No, the flash path has only been built against the AB1568 and hasn't been tested or captured on a Maxwell 2. So the preflight refusal is doing its job for now.
> 2. Yes, I'd welcome a PR mapping the AB1565_ULL2 / AB1585 chip ids, but I'd like to confirm the FOTA framing first, since a wrong packet layout could brick a device. Your USB captures of the official app updating a Maxwell 2 would be exactly what's needed. Please attach them here when you have a Windows machine.
>
> In the meantime I've made the tool recognise the AB1565_ULL2_DONGLE / AB1585_Headset ids: `firmware inspect` now reports them, and `firmware flash --dry-run` works on Maxwell 2 packages. Live flashing of those chips is refused with a clear message until the framing is confirmed.
