#!/usr/bin/env python3
"""
maxwell-ctl: Linux CLI control tool for Audeze Maxwell 2 headset and USB dongle.
Supports device status, preset switching, custom 10-band EQ import/export,
sidetone, chat mix, noise reduction, state backups, and interactive dashboard.
"""

import sys
import os
import json
import argparse
from typing import Optional

from maxwell2.transport import Transport, DeviceNotFoundError, DevicePermissionError
from maxwell2.client import MaxwellClient
from maxwell2.protocol import PRESET_NAMES
from maxwell2.backup import list_backups, load_backup

# Rich UI integration
try:
    from maxwell2.ui import (
        print_rich_status,
        render_eq_spectrum,
        print_rich_preset_success,
        create_console,
        RICH_AVAILABLE,
    )
except ImportError:
    RICH_AVAILABLE = False


def format_plain_status(status: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("  AUDEZE MAXWELL 2 - DEVICE STATUS")
    lines.append("=" * 60)

    preset = status.get("preset", {})
    p_name = preset.get("name", "Unknown")
    p_idx = preset.get("index", "?")
    p_custom = " (Custom Slot " + str(preset.get("slot") + 1) + ")" if preset.get("is_custom") else ""
    lines.append(f"  Active Preset      : [{p_idx}] {p_name}{p_custom}")

    batt = status.get("battery_percent")
    batt_str = f"{batt}%" if batt is not None else "Unknown / Disconnected"
    lines.append(f"  Battery Level      : {batt_str}")

    fw = status.get("firmware", {}).get("firmware", "Unknown")
    lines.append(f"  Firmware Version   : {fw}")

    mute = status.get("mic_muted")
    mute_str = "MUTED" if mute is True else ("UNMUTED" if mute is False else "Unknown")
    lines.append(f"  Hardware Mic Switch: {mute_str}")

    st = status.get("sidetone", {})
    st_state = "ON" if st.get("enabled") else "OFF"
    st_lvl = st.get("level", 0)
    st_max = st.get("max_level", 31)
    lines.append(f"  Sidetone           : {st_state} (Level {st_lvl}/{st_max})")

    cm = status.get("chat_mix", {})
    game_pct = cm.get("game_pct", 50)
    chat_pct = cm.get("chat_pct", 50)
    lines.append(f"  Game / Chat Mix    : Game {game_pct}% | Chat {chat_pct}%")

    nr = status.get("noise_reduction", {}).get("name", "Unknown")
    lines.append(f"  AI Noise Reduction : {nr}")
    lines.append("=" * 60)
    return "\n".join(lines)


def format_plain_eq(eq_data: dict) -> str:
    lines = []
    slot = eq_data.get("slot", 0)
    lines.append("=" * 60)
    lines.append(f"  CUSTOM EQ PROFILE (Preset {slot + 1} / Slot {slot})")
    lines.append("=" * 60)
    lines.append(f"  {'Band':<6} | {'Frequency':<12} | {'Gain (dB)':<12} | {'Q':<8}")
    lines.append("  " + "-" * 46)

    for idx, band in enumerate(eq_data.get("bands", []), 1):
        freq = band.get("frequency_hz", 0)
        freq_str = f"{freq:.0f} Hz" if freq < 1000 else f"{freq/1000:.1f} kHz"
        gain = band.get("gain_db", 0.0)
        gain_str = f"{gain:+.1f} dB"
        q = band.get("q", 1.41)
        lines.append(f"  {idx:<6} | {freq_str:<12} | {gain_str:<12} | {q:<8.2f}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    prog_name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "audezeforlinux"
    parser = argparse.ArgumentParser(
        prog=prog_name,
        description="audezeforlinux: Safe Linux control tool for Audeze Maxwell 2 headset and USB dongle.",
    )
    parser.add_argument("--device", help="Direct path to /dev/hidraw node (auto-detected by default)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate commands and print bytes without sending")
    parser.add_argument("--json", action="store_true", help="Format CLI output as JSON")
    parser.add_argument("--plain", action="store_true", help="Disable rich terminal colors and formatting")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. status
    subparsers.add_parser("status", help="Read headset status (battery, preset, mic, controls)")

    # 2. dashboard / interactive
    subparsers.add_parser("dashboard", help="Open live interactive keyboard-controlled dashboard")
    subparsers.add_parser("interactive", help="Alias for dashboard")

    # 3. eq
    eq_parser = subparsers.add_parser("eq", help="Equalizer controls")
    eq_sub = eq_parser.add_subparsers(dest="eq_action", required=True)

    # eq preset <name_or_number>
    eq_preset = eq_sub.add_parser("preset", help="Select active preset")
    eq_preset.add_argument("name", help="Preset name or index (1..10, e.g. 'Audeze', 'Bass Boost', '1', '7')")

    # eq list
    eq_sub.add_parser("list", help="List all available preset names")

    # eq custom <slot> [--file profile.json] [--export out.json]
    eq_custom = eq_sub.add_parser("custom", help="View, import, or export custom 10-band EQ")
    eq_custom.add_argument("slot", type=int, choices=[1, 2, 3, 4], help="Custom preset slot number (1..4)")
    eq_custom.add_argument("--file", help="JSON profile to write to the slot")
    eq_custom.add_argument("--export", help="Save the slot's current EQ profile to a JSON file")

    # 4. sidetone
    st_parser = subparsers.add_parser("sidetone", help="Control sidetone")
    st_parser.add_argument("value", help="Level (0..31), 'on', or 'off'")

    # 5. chatmix
    cm_parser = subparsers.add_parser("chatmix", help="Adjust Game/Chat balance (0..100 percent Chat)")
    cm_parser.add_argument("value", type=int, help="Chat percentage (0 = 100 percent Game, 50 = 50/50, 100 = 100 percent Chat)")

    # 6. dump
    dump_parser = subparsers.add_parser("dump", help="Dump complete state and all 4 custom EQ slots as JSON")
    dump_parser.add_argument("--out", help="Save dump to specified JSON file")

    # 7. backup
    bk_parser = subparsers.add_parser("backup", help="Manage automated pre-write configuration backups")
    bk_sub = bk_parser.add_subparsers(dest="backup_action", required=True)
    bk_sub.add_parser("list", help="List all available backups")
    bk_show = bk_sub.add_parser("show", help="Display details of a backup file")
    bk_show.add_argument("file", help="Filename or path of backup")

    # 8. firmware
    fw_parser = subparsers.add_parser("firmware", help="Firmware diagnostics, parity verification, and package inspection")
    fw_sub = fw_parser.add_subparsers(dest="firmware_action")
    fw_sub.add_parser("status", help="Read dual dongle & headset firmware status, parity, and safety readiness")
    fw_check = fw_sub.add_parser("check", help="Check official firmware catalog and changelog against installed version")
    fw_check.add_argument("--package", "-p", default=None, help="Optional path to a .bin package to verify against installed version")
    fw_inspect = fw_sub.add_parser("inspect", help="Offline inspection and validation of a firmware .bin package")
    fw_inspect.add_argument("file", help="Path to firmware .bin file to inspect")
    fw_unpack = fw_sub.add_parser("unpack", help="Decompress and extract firmware partitions to directory")
    fw_unpack.add_argument("file", help="Path to firmware .bin file to unpack")
    fw_flash = fw_sub.add_parser("flash", help="Simulate a firmware update (live flashing is refused until the FOTA protocol is verified)")
    fw_flash.add_argument("file", help="Path to firmware .bin file to flash")
    fw_flash.add_argument("--target", choices=["dongle", "headset"], default="dongle", help="Target node")
    fw_flash.add_argument("--confirm-risk", action="store_true", help="Explicit confirmation gate for real hardware flashing")
    fw_flash.add_argument("--dry-run", action="store_true", help="Simulate FOTA flash without transmitting live hardware writes")
    fw_flash.add_argument("--allow-downgrade", action="store_true", help="Permit flashing a package older than the installed firmware")

    # 9. system utilities (doctor, setup-udev, install-system)
    subparsers.add_parser("doctor", help="Run comprehensive Linux audio environment and permissions diagnostics")
    subparsers.add_parser("setup-udev", help="Install /etc/udev/rules.d rules for non-root hardware access")
    subparsers.add_parser("install-system", help="Install desktop application entry and user PATH symlink to ~/.local/bin")

    args = parser.parse_args()

    # Handle system tools (doctor, setup-udev, install-system)
    if args.command == "doctor":
        from maxwell2.system import run_doctor, format_doctor_report
        report = run_doctor()
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        else:
            print(format_doctor_report(report))
        return

    if args.command == "setup-udev":
        from maxwell2.system import setup_udev
        ok, msg = setup_udev()
        print(msg)
        return

    if args.command == "install-system":
        from maxwell2.system import install_desktop_integration
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        ok, msg = install_desktop_integration(repo_dir)
        print(msg)
        return

    # Handle offline commands first (backups, eq list, firmware inspect/unpack/check)
    if args.command == "eq" and args.eq_action == "list":
        if args.json:
            print(json.dumps([{"index": i, "name": n} for i, n in enumerate(PRESET_NAMES, 1)], indent=2))
        else:
            print("\nAvailable Onboard Presets:")
            for idx, name in enumerate(PRESET_NAMES, 1):
                custom_tag = " [Custom Slot " + str(idx - 6) + "]" if idx >= 7 else ""
                print(f"  {idx:2d}. {name}{custom_tag}")
            print()
        return

    if args.command == "backup":
        if args.backup_action == "list":
            backups = list_backups()
            if args.json:
                print(json.dumps(backups, indent=2))
            else:
                if not backups:
                    print("No backups found in ~/.config/maxwell2/backups/.")
                else:
                    print(f"\nAvailable Backups ({len(backups)}):")
                    for b in backups:
                        print(f"  - {b['filename']} (Action: {b['action']}, Time: {b['timestamp']})")
                    print()
            return
        elif args.backup_action == "show":
            data = load_backup(args.file)
            print(json.dumps(data, indent=2))
            return

    if args.command == "firmware":
        if args.firmware_action == "inspect":
            from maxwell2.firmware import FirmwarePackage, format_firmware_inspect_plain
            pkg = FirmwarePackage(args.file)
            pkg.verify_partitions()
            info = pkg.to_dict()
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(format_firmware_inspect_plain(info))
            return

        elif args.firmware_action == "unpack":
            from maxwell2.firmware import FirmwarePackage
            pkg = FirmwarePackage(args.file)
            out_files = pkg.unpack_to_directory(args.out)
            print(f"[+] Decompressed and extracted {len(out_files)} partitions to {args.out}:")
            for f in out_files:
                print(f"  - {f} ({os.path.getsize(f)} bytes)")
            return

        elif args.firmware_action == "check":
            from maxwell2.firmware import check_available_updates, format_firmware_check_plain
            # Try to query device version, or fallback to status
            current_ver = "v1.0.5.7.4"
            try:
                transport = Transport(hidraw_path=args.device, dry_run=args.dry_run)
                with transport:
                    client = MaxwellClient(transport)
                    fw = client.get_firmware()
                    current_ver = fw.get("firmware", "v1.0.5.7.4")
            except Exception:
                pass
            res = check_available_updates(current_ver, package_path=args.package)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(format_firmware_check_plain(res))
            return

        elif args.firmware_action == "flash":
            from maxwell2.firmware import FirmwarePackage, FotaEngine, FotaSafetyError, FotaTransferError
            pkg = FirmwarePackage(args.file)
            is_dry = args.dry_run or not args.confirm_risk

            try:
                transport = Transport(hidraw_path=args.device, dry_run=is_dry)
                with transport:
                    client = MaxwellClient(transport)
                    engine = FotaEngine(
                        pkg, target=args.target, client=client, dry_run=is_dry, allow_downgrade=args.allow_downgrade
                    )

                    # Run every safety check before asking for confirmation.
                    pre = engine.preflight_check()

                    if not is_dry:
                        print("⚠ WARNING: You are about to flash physical hardware.")
                        print("           Do NOT unplug the USB cable or power off the headset during flashing.")
                        print(f"           {args.target.upper()}: {pre['installed_version']} -> {pkg.version_string}")
                        word = "CONFIRM"
                        if pre["is_downgrade"]:
                            print("⚠ This is a DOWNGRADE to an older firmware version.")
                            word = "DOWNGRADE"
                        try:
                            confirm = input(f"Type '{word}' to proceed: ").strip()
                        except (KeyboardInterrupt, EOFError):
                            confirm = ""
                        if confirm != word:
                            print("\n[-] Flash aborted by user. Nothing was written.")
                            return

                    print(f"[*] Starting FOTA Flash {'(DRY RUN)' if is_dry else '(LIVE HARDWARE)'}:")
                    print(f"    File: {pkg.filename} (Version: {pkg.version_string})")
                    print(f"    Target Node: {args.target.upper()} • Total Chunks: {engine.total_chunks}")

                    def progress(p):
                        pct = p['percent']
                        bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                        sys.stdout.write(f"\r    [{bar}] {pct:3d}% ({p['current_chunk']}/{p['total_chunks']}) - {p['speed_kb_s']} KB/s - ETA: {p['eta_sec']}s  ")
                        sys.stdout.flush()

                    res = engine.execute(progress_callback=progress)
            except FotaTransferError as e:
                print(f"\n✖ {e}")
                sys.exit(3)
            except FotaSafetyError as e:
                print(f"[-] Flash refused: {e}")
                sys.exit(1)
            except (DeviceNotFoundError, DevicePermissionError) as e:
                print(f"[-] {e}")
                sys.exit(1)

            print(f"\n✔ FOTA transfer finished in {res['duration_sec']}s ({res['chunks_sent']} chunks transferred).")
            if is_dry:
                print("  Note: Dry-run simulation completed. No hardware flash bytes were modified.")
            elif res["verification"] == "confirmed":
                print(f"  ✔ {args.target.capitalize()} now reports {res['reported_version']}.")
            else:
                print(f"  ✖ Could not confirm the update: {args.target} reports {res['reported_version']} "
                      f"(expected {pkg.version_string}). Run 'firmware status' once the device has reconnected.")
                sys.exit(4)
            return

    # Initialize transport and client
    try:
        transport = Transport(hidraw_path=args.device, dry_run=args.dry_run)
        with transport:
            client = MaxwellClient(transport)

            if args.command in ("dashboard", "interactive"):
                from maxwell2.interactive import run_interactive_dashboard
                run_interactive_dashboard(client)
                return

            if args.command == "status":
                status = client.get_status()
                if not status["device_responding"]:
                    print("[!] No response from the headset: the values below are defaults, not readings.\n"
                          "    Check that it is powered on and linked to the dongle.", file=sys.stderr)
                if args.json:
                    print(json.dumps(status, indent=2))
                elif RICH_AVAILABLE and not args.plain and sys.stdout.isatty():
                    print_rich_status(status)
                else:
                    print(format_plain_status(status))

            elif args.command == "eq" and args.eq_action == "preset":
                res = client.set_preset(args.name)
                if args.json:
                    print(json.dumps(res, indent=2))
                elif RICH_AVAILABLE and not args.plain and sys.stdout.isatty():
                    print_rich_preset_success(res["preset"], res["backup_file"], res.get("dry_run"))
                else:
                    p = res["preset"]
                    dry = " [DRY-RUN]" if res.get("dry_run") else ""
                    print(f"[+] Preset switched successfully to: [{p['index']}] {p['name']}{dry}")
                    print(f"    Backup saved to: {res['backup_file']}")

            elif args.command == "eq" and args.eq_action == "custom":
                slot_idx = args.slot - 1  # 0..3
                eq_data = client.read_custom_eq(slot_idx)

                if args.export:
                    with open(args.export, "w", encoding="utf-8") as f:
                        json.dump(eq_data, f, indent=2)
                    print(f"[+] Exported Custom Preset {args.slot} to {args.export}")
                elif args.json:
                    print(json.dumps(eq_data, indent=2))
                elif RICH_AVAILABLE and not args.plain and sys.stdout.isatty():
                    console = create_console()
                    console.print()
                    console.print(render_eq_spectrum(eq_data.get("bands", [])))
                    console.print()
                else:
                    print(format_plain_eq(eq_data))

            elif args.command == "sidetone":
                val_str = args.value.strip().lower()
                if val_str in ("on", "enable", "true"):
                    res = client.set_sidetone(enabled=True)
                    print("[+] Sidetone enabled.")
                elif val_str in ("off", "disable", "false"):
                    res = client.set_sidetone(enabled=False)
                    print("[+] Sidetone disabled.")
                else:
                    try:
                        lvl = int(val_str)
                        res = client.set_sidetone(level=lvl)
                        print(f"[+] Sidetone level set to {lvl}.")
                    except ValueError:
                        print(f"[-] Invalid sidetone value '{args.value}'. Use 'on', 'off', or a number.")
                        sys.exit(1)

            elif args.command == "chatmix":
                raw_val = int((args.value / 100.0) * 20)
                raw_val = max(0, min(20, raw_val))
                res = client.set_chat_mix(raw_val)
                print(f"[+] Game/Chat mix adjusted to {args.value}% Chat (Raw: {raw_val}/20).")

            elif args.command == "dump":
                full_dump = client.dump_all()
                if args.out:
                    with open(args.out, "w", encoding="utf-8") as f:
                        json.dump(full_dump, f, indent=2)
                    print(f"[+] Complete configuration dumped to {args.out}")
                else:
                    print(json.dumps(full_dump, indent=2))

            elif args.command == "firmware":
                from maxwell2.firmware import get_firmware_diagnostics, format_firmware_status_plain
                diag = get_firmware_diagnostics(client)
                if args.json:
                    print(json.dumps(diag, indent=2))
                else:
                    print(format_firmware_status_plain(diag))

    except DeviceNotFoundError as e:
        print(f"[-] {e}")
        sys.exit(1)
    except DevicePermissionError as e:
        print(f"[-] {e}")
        sys.exit(2)
    except Exception as e:
        print(f"[-] Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
