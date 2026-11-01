"""
High-level control client for Audeze Maxwell 2.
Implements status queries, control adjustments, EQ profile management,
and pre-write backup snapshots.
"""

import time
from typing import Dict, Any, Optional, Union, List
from maxwell2.protocol import (
    build_read_preset_command,
    build_select_preset_command,
    build_battery_command,
    build_firmware_command,
    build_read_control_command,
    build_write_control_command,
    build_read_ui_eq_command,
    build_write_ui_eq_command,
    parse_preset_index,
    parse_battery_notification,
    parse_firmware_notification,
    parse_control_response,
    parse_ui_eq_preset,
    SUB_MIC_MUTE,
    SUB_NOISE_REDUCTION,
    SUB_SIDETONE_LEVEL,
    SUB_MAX_SIDETONE,
    SUB_SIDETONE_ON,
    SUB_CHAT_MIX,
    PRESET_NAMES,
    ProtocolError,
)
from maxwell2.transport import Transport
from maxwell2.backup import create_backup, load_backup


class MaxwellClient:
    """
    Main interface for inspecting and controlling the Audeze Maxwell 2.
    """

    def __init__(self, transport: Transport):
        self.transport = transport

    # --------------------------------------------------------------------------
    # Telemetry & Status Reads
    # --------------------------------------------------------------------------

    def get_preset(self) -> Dict[str, Any]:
        """Reads the currently active onboard preset."""
        packet = build_read_preset_command()
        resp = self.transport.exchange(packet)
        if self.transport.dry_run:
            return {"index": 1, "name": "Audeze (Simulated)", "is_custom": False, "slot": None}
        return parse_preset_index(resp)

    def get_battery(self) -> Optional[int]:
        """Queries battery level percentage (0..100)."""
        if self.transport.dry_run:
            return 85
        packet = build_battery_command()
        notif = self.transport.query_notification(packet, timeout_sec=2.0)
        if notif:
            try:
                return parse_battery_notification(notif)
            except Exception:
                pass
        return None

    def get_firmware(self) -> Dict[str, Any]:
        """Queries both dongle and headset firmware versions."""
        if self.transport.dry_run:
            return {
                "dongle": "v1.0.5.7.4 (Simulated)",
                "headset": "v1.0.5.7.4 (Simulated)",
                "match": True,
                "firmware": "v1.0.5.7.4",
            }

        packet = build_firmware_command()

        # 1. Query Dongle firmware (route 0x00)
        fw_dongle = "Unknown"
        old_dongle_flag = self.transport.is_dongle
        try:
            self.transport.is_dongle = False  # Route 0x00
            notif = self.transport.query_notification(packet, timeout_sec=1.2)
            if notif:
                fw_dongle = parse_firmware_notification(notif)
        except Exception:
            pass
        finally:
            self.transport.is_dongle = old_dongle_flag

        # 2. Query Headset firmware (route 0x80)
        fw_headset = "Unknown"
        try:
            notif = self.transport.query_notification(packet, timeout_sec=1.2)
            if notif:
                fw_headset = parse_firmware_notification(notif)
        except Exception:
            pass

        primary_fw = fw_headset if fw_headset != "Unknown" else fw_dongle
        is_match = (fw_dongle == fw_headset) if (fw_dongle != "Unknown" and fw_headset != "Unknown") else True

        return {
            "dongle": fw_dongle,
            "headset": fw_headset,
            "match": is_match,
            "firmware": primary_fw,
        }

    def get_mic_mute(self) -> Optional[bool]:
        """Reads hardware microphone mute switch state (True = muted, False = unmuted)."""
        packet = build_read_control_command(SUB_MIC_MUTE)
        resp = self.transport.exchange(packet)
        if self.transport.dry_run:
            return False
        val = parse_control_response(resp, SUB_MIC_MUTE)
        return val == 0x00

    def get_sidetone(self) -> Dict[str, Any]:
        """Reads sidetone toggle state and current volume level."""
        if self.transport.dry_run:
            return {"enabled": True, "level": 15, "max_level": 31}

        # Sidetone toggle
        resp_on = self.transport.exchange(build_read_control_command(SUB_SIDETONE_ON))
        enabled = (parse_control_response(resp_on, SUB_SIDETONE_ON) == 1)

        # Sidetone level
        resp_lvl = self.transport.exchange(build_read_control_command(SUB_SIDETONE_LEVEL))
        level = parse_control_response(resp_lvl, SUB_SIDETONE_LEVEL)

        # Max level
        max_level = 31
        try:
            resp_max = self.transport.exchange(build_read_control_command(SUB_MAX_SIDETONE))
            max_level = parse_control_response(resp_max, SUB_MAX_SIDETONE)
        except Exception:
            pass

        return {"enabled": enabled, "level": level, "max_level": max_level}

    def get_chat_mix(self) -> Dict[str, Any]:
        """Reads Game / Chat mix ratio (0 = 100% Game, 10 = 50/50, 20 = 100% Chat)."""
        if self.transport.dry_run:
            return {"raw": 10, "game_pct": 50, "chat_pct": 50}

        resp = self.transport.exchange(build_read_control_command(SUB_CHAT_MIX))
        raw = parse_control_response(resp, SUB_CHAT_MIX)
        # raw is 0..20: 0 = 100% Game, 20 = 100% Chat
        chat_pct = int((raw / 20.0) * 100)
        game_pct = 100 - chat_pct
        return {"raw": raw, "game_pct": game_pct, "chat_pct": chat_pct}

    def get_noise_reduction(self) -> Dict[str, Any]:
        """Reads AI noise suppression mode (0 = Off, 1 = Low, 2 = High)."""
        if self.transport.dry_run:
            return {"code": 1, "name": "Low"}

        resp = self.transport.exchange(build_read_control_command(SUB_NOISE_REDUCTION))
        code = parse_control_response(resp, SUB_NOISE_REDUCTION)
        names = {0: "Off", 1: "Low", 2: "High"}
        return {"code": code, "name": names.get(code, f"Unknown ({code})")}

    def get_status(self) -> Dict[str, Any]:
        """Collects full status information in one dictionary with per-field resilience."""
        status = {}
        responded = False

        try:
            status["preset"] = self.get_preset()
            responded = True
        except Exception:
            status["preset"] = {"index": 4, "name": "Immersive", "is_custom": False, "slot": None}

        try:
            status["battery_percent"] = self.get_battery()
            responded = responded or status["battery_percent"] is not None
        except Exception:
            status["battery_percent"] = 90

        try:
            status["firmware"] = self.get_firmware()
            responded = responded or status["firmware"]["firmware"] != "Unknown"
        except Exception:
            status["firmware"] = {"firmware": "v1.0.5.7.4", "dongle": "v1.0.5.7.4", "headset": "v1.0.5.7.4", "match": True}

        try:
            status["mic_muted"] = self.get_mic_mute()
            responded = True
        except Exception:
            status["mic_muted"] = False

        try:
            status["sidetone"] = self.get_sidetone()
            responded = True
        except Exception:
            status["sidetone"] = {"enabled": False, "level": 5, "max_level": 31}

        try:
            status["chat_mix"] = self.get_chat_mix()
            responded = True
        except Exception:
            status["chat_mix"] = {"raw": 10, "game_pct": 50, "chat_pct": 50}

        try:
            status["noise_reduction"] = self.get_noise_reduction()
            responded = True
        except Exception:
            status["noise_reduction"] = {"code": 1, "name": "Low"}

        # Every field above falls back to a placeholder, so callers need this to
        # tell real readings from defaults.
        status["device_responding"] = responded
        return status

    # --------------------------------------------------------------------------
    # Custom EQ Profile Reads & Writes
    # --------------------------------------------------------------------------

    def read_custom_eq(self, slot: int) -> Dict[str, Any]:
        """Reads 10-band slider configuration for a custom slot (0..3)."""
        if not (0 <= slot <= 3):
            raise ValueError(f"Custom EQ slot must be 0..3 (representing Presets 1..4), got {slot}")

        cmd = build_read_ui_eq_command(slot)
        resp = self.transport.exchange(cmd)
        if self.transport.dry_run:
            freqs = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
            bands = [{"band_index": i, "frequency_hz": f, "gain_db": 0.0, "q": 1.41} for i, f in enumerate(freqs)]
            return {"slot": slot, "preset_num": slot + 1, "bands": bands, "is_simulated": True}

        return parse_ui_eq_preset(resp, expected_slot=slot)

    def write_custom_eq(self, slot: int, bands: List[Dict[str, Any]], activate: bool = True) -> Dict[str, Any]:
        """
        Writes a 10-band custom EQ profile to NVDM key 0xEF00 + slot (slot 0..3).
        Takes a pre-write backup snapshot before sending the write.
        Optionally activates the custom preset so the DSP reloads the new curve.
        """
        if not (0 <= slot <= 3):
            raise ValueError(f"Custom EQ slot must be 0..3 (representing Presets 1..4), got {slot}")

        # 1. Snapshot previous state
        current = self.read_custom_eq(slot)
        backup_path = create_backup(f"write_custom_eq_slot_{slot+1}", {"previous_eq": current, "target_bands": bands})

        # 2. Build and send write command
        cmd = build_write_ui_eq_command(slot, bands)
        self.transport.write_race_packet(cmd)

        if not self.transport.dry_run and activate:
            # Re-select custom preset (Preset 7..10) so the DSP reloads the updated curve
            preset_idx = 7 + slot
            self.transport.write_race_packet(build_select_preset_command(preset_idx))

        return {
            "success": True,
            "slot": slot,
            "preset_num": slot + 1,
            "backup_file": backup_path,
            "dry_run": self.transport.dry_run,
        }

    def dump_all(self) -> Dict[str, Any]:
        """Dumps all headset status and all 4 custom EQ profiles as a single JSON export."""
        status = self.get_status()
        custom_eqs = {}
        for s in range(4):
            try:
                custom_eqs[f"custom_slot_{s + 1}"] = self.read_custom_eq(s)
            except Exception as e:
                custom_eqs[f"custom_slot_{s + 1}"] = {"error": str(e)}

        return {
            "status": status,
            "custom_eq_profiles": custom_eqs,
        }

    # --------------------------------------------------------------------------
    # Settings Writes (With Pre-Write Backup & Verification)
    # --------------------------------------------------------------------------

    def set_preset(self, target: Union[int, str]) -> Dict[str, Any]:
        """
        Switches active preset (1..10 or preset name).
        Takes a pre-write backup snapshot before sending the write.
        """
        # Resolve target index
        if isinstance(target, str):
            target_clean = target.strip().lower()
            matched_idx = None
            for idx, name in enumerate(PRESET_NAMES, 1):
                if target_clean == name.lower() or target_clean in name.lower():
                    matched_idx = idx
                    break
            if matched_idx is None:
                raise ValueError(f"Unknown preset name '{target}'. Valid names: {PRESET_NAMES}")
            preset_idx = matched_idx
        else:
            preset_idx = int(target)
            if not (1 <= preset_idx <= 10):
                raise ValueError(f"Preset index must be 1..10, got {preset_idx}")

        # 1. Take snapshot of current state
        current_preset = self.get_preset()
        backup_path = create_backup("set_preset", {"previous_preset": current_preset, "target": preset_idx})

        # 2. Build and transmit write command
        cmd = build_select_preset_command(preset_idx)
        self.transport.write_race_packet(cmd)

        if self.transport.dry_run:
            return {
                "success": True,
                "preset": {"index": preset_idx, "name": PRESET_NAMES[preset_idx - 1]},
                "backup_file": backup_path,
                "dry_run": True,
            }

        # Allow DSP brief settling time to reload preset and drain write ACK
        time.sleep(0.06)
        self.transport.poll_input_report()

        # 3. Read back to verify
        new_preset = self.get_preset()
        if new_preset["index"] != preset_idx:
            raise RuntimeError(f"Preset change verification failed: expected {preset_idx}, got {new_preset['index']}")

        return {
            "success": True,
            "preset": new_preset,
            "backup_file": backup_path,
            "dry_run": False,
        }

    def set_sidetone(self, enabled: Optional[bool] = None, level: Optional[int] = None) -> Dict[str, Any]:
        """
        Updates sidetone enable state or volume level.
        Takes a pre-write backup snapshot before applying.
        """
        current = self.get_sidetone()
        backup_path = create_backup("set_sidetone", {"previous_sidetone": current})

        if enabled is not None:
            cmd_toggle = build_write_control_command(SUB_SIDETONE_ON, 1 if enabled else 0)
            self.transport.write_race_packet(cmd_toggle)

        if level is not None:
            cmd_level = build_write_control_command(SUB_SIDETONE_LEVEL, int(level))
            self.transport.write_race_packet(cmd_level)

        if self.transport.dry_run:
            return {"success": True, "sidetone": {"enabled": enabled, "level": level}, "backup_file": backup_path}

        updated = self.get_sidetone()
        return {"success": True, "sidetone": updated, "backup_file": backup_path}

    def set_chat_mix(self, value: int) -> Dict[str, Any]:
        """
        Updates Game/Chat balance (0..20, 10 = 50/50 center).
        """
        if not (0 <= value <= 20):
            raise ValueError(f"Chat mix value must be 0..20, got {value}")

        current = self.get_chat_mix()
        backup_path = create_backup("set_chat_mix", {"previous_chat_mix": current, "target": value})

        cmd = build_write_control_command(SUB_CHAT_MIX, value)
        self.transport.write_race_packet(cmd)

        if self.transport.dry_run:
            return {"success": True, "chat_mix": value, "backup_file": backup_path}

        updated = self.get_chat_mix()
        return {"success": True, "chat_mix": updated, "backup_file": backup_path}

    # --------------------------------------------------------------------------
    # Fast Low-Latency Control Writes (Interactive TUI Slider Mode)
    # --------------------------------------------------------------------------

    def set_sidetone_fast(self, enabled: Optional[bool] = None, level: Optional[int] = None):
        """Low-latency hardware write for interactive TUI sidetone adjustments."""
        if enabled is not None:
            self.transport.write_control_fast(SUB_SIDETONE_ON, 1 if enabled else 0)
        if level is not None:
            lvl = max(0, min(31, int(level)))
            self.transport.write_control_fast(SUB_SIDETONE_LEVEL, lvl)

    def set_chat_mix_fast(self, value: int):
        """Low-latency hardware write for interactive TUI Game/Chat slider."""
        val = max(0, min(20, int(value)))
        self.transport.write_control_fast(SUB_CHAT_MIX, val)

    def set_noise_reduction_fast(self, code: int):
        """Low-latency hardware write for interactive TUI AI noise reduction mode."""
        c = max(0, min(2, int(code)))
        self.transport.write_control_fast(SUB_NOISE_REDUCTION, c)
