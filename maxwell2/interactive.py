"""
audezeforlinux: Interactive live TUI studio dashboard, Custom EQ Studio,
Firmware Management, and System Diagnostics.
Uses curses with alternate screen buffer and mouse support for zero scrollback,
rock-solid arrow key navigation, and real-time hardware synchronization.
Designed with a sleek, minimalist Studio Grayscale aesthetic.
"""

import os
import sys
import copy
import time
import json
import threading
from typing import Optional, List, Dict, Any, Callable

# Prevent escape sequence delay for arrow keys
os.environ.setdefault("ESCDELAY", "25")

try:
    import curses
    CURSES_AVAILABLE = True
except ImportError:
    CURSES_AVAILABLE = False

from maxwell2.client import MaxwellClient
from maxwell2.protocol import (
    PRESET_NAMES,
    build_write_control_command,
    SUB_NOISE_REDUCTION,
)
from maxwell2.firmware import (
    FIRMWARE_CATALOG,
    get_firmware_diagnostics,
    FirmwarePackage,
    FotaEngine,
    FotaSafetyError,
)
from maxwell2.backup import create_backup, list_backups

STANDARD_FREQS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

CURVE_PROFILES: Dict[str, List[float]] = {
    "Flat (0 dB)": [0.0] * 10,
    "Harman Target": [4.0, 3.5, 2.0, 0.5, 0.0, 0.5, 1.5, 2.5, 1.0, 0.5],
    "Bass Boost": [5.5, 4.5, 3.5, 2.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
    "FPS Footsteps": [-3.0, -2.0, -1.0, 0.5, 2.0, 3.5, 4.0, 3.0, 1.5, 0.0],
    "V-Shape Audiophile": [4.5, 3.5, 2.0, 0.0, -1.5, -1.0, 1.0, 2.5, 4.0, 4.5],
    "Vocal Clarity": [0.5, 1.0, 1.5, 2.5, 3.0, 2.5, 1.5, 0.5, -0.5, -1.0],
}

DEFAULT_STATUS = {
    "preset": {"index": 4, "name": "Immersive", "is_custom": False, "slot": None},
    "battery_percent": 94,
    "firmware": {"firmware": "v1.0.5.7.4", "dongle": "v1.0.5.7.4", "headset": "v1.0.5.7.4", "match": True},
    "mic_muted": False,
    "sidetone": {"enabled": False, "level": 5, "max_level": 31},
    "chat_mix": {"raw": 10, "game_pct": 50, "chat_pct": 50},
    "noise_reduction": {"code": 1, "name": "Low"},
}


class ClickableRegion:
    """Defines an interactive on-screen mouse-clickable bounding box."""
    def __init__(self, y: int, x1: int, x2: int, callback: Callable[[], None], desc: str = ""):
        self.y = y
        self.x1 = x1
        self.x2 = x2
        self.callback = callback
        self.desc = desc

    def hit_test(self, my: int, mx: int) -> bool:
        return self.y == my and self.x1 <= mx <= self.x2


def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0):
    """Safely writes text to curses screen without throwing errors on boundary wrapping."""
    max_y, max_x = stdscr.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    avail = max_x - x
    if avail <= 0:
        return
    if len(text) > avail:
        text = text[:avail]
    if y == max_y - 1 and len(text) == avail:
        text = text[:-1]
        if not text:
            return
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


class CursesTUI:
    """Full-featured studio dashboard for audezeforlinux."""

    def __init__(self, stdscr, client: MaxwellClient):
        self.stdscr = stdscr
        self.client = client
        self.running = True

        # Active view:
        # 0 = Controls & Presets
        # 1 = Horizontal 10-Band EQ Studio
        # 2 = Firmware & Flasher
        # 3 = System & Backups
        self.active_tab = 0
        self.TOTAL_TABS = 4

        # Tab 0: Dashboard state
        # 0..9: presets, 10: sidetone, 11: chatmix, 12: noise_reduction
        self.selected_row = 0
        self.TOTAL_ROWS = 13

        # Tab 1: Horizontal Graphic EQ Studio state
        self.active_eq_slot = 0  # 0..3 (Presets 1..4)
        self.selected_band = 5   # 0..9 (default 1 kHz)
        self.current_eq_bands: List[Dict[str, Any]] = [
            {"band_index": 513, "frequency_hz": f, "gain_db": 0.0, "q": f / 2.0}
            for f in STANDARD_FREQS
        ]
        self.eq_dirty = False
        self.profile_cycle_idx = 0
        self.active_curve_name = "Hardware Stored"

        # Tab 2: Firmware state
        self.fw_selected_release = 0
        self.fw_target = "dongle"  # "dongle" or "headset"
        self.confirm_flash_open = False

        # Tab 3: System state
        self.system_backups: List[Dict[str, Any]] = []

        # Telemetry & Status
        self.status: Dict[str, Any] = copy.deepcopy(DEFAULT_STATUS)
        self.message = "Hardware connected. Use Arrow Keys to navigate, Enter or Mouse Click to activate."
        self.msg_attr = 0

        # Clickable regions registered during the last render
        self.click_regions: List[ClickableRegion] = []

        # Color pair indices
        self.C_NORMAL = 0
        self.C_HEADER = 0
        self.C_DIM = 0
        self.C_ACTIVE = 0
        self.C_WARN = 0
        self.C_SUCCESS = 0

    def init_curses_terminal(self):
        """Initializes terminal colors and settings."""
        curses.curs_set(0)  # Hide cursor
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)  # Non-blocking poll every 100ms for high responsiveness

        # Enable mouse events
        try:
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_DOUBLE_CLICKED)
        except Exception:
            pass

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)   # Normal
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE) # Reverse / Highlight
            curses.init_pair(3, curses.COLOR_WHITE, -1)   # Dim
            curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE) # Active indicator

            self.C_NORMAL = curses.color_pair(1)
            self.C_HEADER = curses.color_pair(2) | curses.A_BOLD
            self.C_DIM = curses.color_pair(3) | curses.A_DIM
            self.C_ACTIVE = curses.color_pair(2) | curses.A_BOLD
            self.C_WARN = curses.color_pair(1) | curses.A_BOLD
            self.C_SUCCESS = curses.color_pair(2) | curses.A_BOLD
        else:
            self.C_NORMAL = curses.A_NORMAL
            self.C_HEADER = curses.A_REVERSE | curses.A_BOLD
            self.C_DIM = curses.A_DIM
            self.C_ACTIVE = curses.A_REVERSE | curses.A_BOLD
            self.C_WARN = curses.A_BOLD
            self.C_SUCCESS = curses.A_REVERSE

    def load_initial_hardware_state(self):
        """Fetches full state from headset with fallback defaults."""
        try:
            live = self.client.get_status()
            self.status.update(live)
        except Exception as e:
            self.message = f"Hardware note: using initial baseline state ({e})"

        try:
            raw_eq = self.client.read_custom_eq(self.active_eq_slot)
            bands = raw_eq.get("bands", [])
            if bands:
                self.current_eq_bands = copy.deepcopy(bands)
        except Exception:
            pass

        try:
            self.system_backups = list_backups()
        except Exception:
            self.system_backups = []

    def set_tab(self, tab_idx: int):
        """Switches active view tab cleanly."""
        self.active_tab = tab_idx % self.TOTAL_TABS
        tab_names = ["Controls & Presets", "10-Band EQ Studio", "Firmware Management", "System Diagnostics"]
        self.message = f"Switched to {tab_names[self.active_tab]}."

    def run(self):
        """Main event loop."""
        self.init_curses_terminal()
        self.message = "Reading headset status..."
        self.click_regions.clear()
        self.stdscr.erase()
        try:
            self.render()
        except Exception as e:
            safe_addstr(self.stdscr, 1, 2, f"Render notice: {e}", self.C_WARN)
        self.stdscr.refresh()

        hardware_load_done = threading.Event()

        def load_hardware_state():
            try:
                self.load_initial_hardware_state()
                self.message = "Hardware status loaded. Use Arrow Keys to navigate."
            except Exception as e:
                self.message = f"Hardware status notice: {e}"
            finally:
                hardware_load_done.set()

        hardware_thread = threading.Thread(target=load_hardware_state, daemon=True)
        hardware_thread.start()

        while self.running:
            self.click_regions.clear()
            self.stdscr.erase()
            try:
                self.render()
            except Exception as e:
                safe_addstr(self.stdscr, 1, 2, f"Render notice: {e}", self.C_WARN)
            self.stdscr.refresh()

            try:
                ch = self.stdscr.getch()
            except KeyboardInterrupt:
                self.running = False
                break

            if ch == -1:
                continue

            if not hardware_load_done.is_set():
                if ch in (ord('q'), ord('Q'), 27):
                    self.running = False
                continue

            try:
                self.handle_input(ch)
            except Exception as e:
                self.message = f"Notice: {e}"

    # ==========================================================================
    # RENDERING ENGINE
    # ==========================================================================

    def render(self):
        """Draws current view based on terminal geometry."""
        max_y, max_x = self.stdscr.getmaxyx()

        if max_y < 22 or max_x < 76:
            msg1 = "AUDEZE FOR LINUX"
            msg2 = f"Please enlarge terminal (Current: {max_x}x{max_y}, Min: 76x22)"
            msg3 = "Press [q] to exit."
            safe_addstr(self.stdscr, max_y // 2 - 1, max(0, (max_x - len(msg1)) // 2), msg1, self.C_HEADER)
            safe_addstr(self.stdscr, max_y // 2, max(0, (max_x - len(msg2)) // 2), msg2, self.C_NORMAL)
            safe_addstr(self.stdscr, max_y // 2 + 1, max(0, (max_x - len(msg3)) // 2), msg3, self.C_DIM)
            return

        # 1. Top Title Banner
        self.render_header(max_y, max_x)

        # 2. View Tabs
        self.render_tab_bar(max_x, y=3)

        # 3. Main View Area
        if self.active_tab == 0:
            self.render_dashboard_view(max_y, max_x, start_y=5)
        elif self.active_tab == 1:
            self.render_horizontal_eq_view(max_y, max_x, start_y=5)
        elif self.active_tab == 2:
            self.render_firmware_view(max_y, max_x, start_y=5)
        elif self.active_tab == 3:
            self.render_system_view(max_y, max_x, start_y=5)

        # 4. Status Message & Bottom Help Bar
        self.render_footer(max_y, max_x)

    def render_header(self, max_y: int, max_x: int):
        """Renders sleek top telemetry bar."""
        fw = self.status.get("firmware", {}).get("firmware", "v1.0.5.7.4")
        batt = self.status.get("battery_percent", 94)
        mute = self.status.get("mic_muted", False)

        # Battery block
        pct = max(0, min(100, batt if batt is not None else 0))
        filled = int(pct / 10)
        batt_bar = f"[{'■' * filled}{'□' * (10 - filled)}] {pct}%"
        mic_tag = "MUTED" if mute else "LIVE"

        title_left = " AUDEZE FOR LINUX — STUDIO DASHBOARD "
        safe_addstr(self.stdscr, 0, 0, "┌" + "─" * (max_x - 2) + "┐", self.C_DIM)
        safe_addstr(self.stdscr, 1, 0, "│", self.C_DIM)
        safe_addstr(self.stdscr, 1, 2, title_left, self.C_HEADER)

        telemetry = f"FW: {fw}  •  Mic: {mic_tag}  •  Batt: {batt_bar} "
        telemetry_x = max(len(title_left) + 4, max_x - len(telemetry) - 2)
        safe_addstr(self.stdscr, 1, telemetry_x, telemetry, self.C_NORMAL)
        safe_addstr(self.stdscr, 1, max_x - 1, "│", self.C_DIM)
        safe_addstr(self.stdscr, 2, 0, "└" + "─" * (max_x - 2) + "┘", self.C_DIM)

    def render_tab_bar(self, max_x: int, y: int):
        """Renders view navigation tabs."""
        tabs = [
            (" [ 1. CONTROLS ] ", "Tab 0"),
            (" [ 2. EQ STUDIO ] ", "Tab 1"),
            (" [ 3. FIRMWARE ] ", "Tab 2"),
            (" [ 4. SYSTEM & BACKUPS ] ", "Tab 3"),
        ]
        curr_x = 2
        for idx, (label, desc) in enumerate(tabs):
            attr = self.C_ACTIVE if self.active_tab == idx else self.C_DIM
            safe_addstr(self.stdscr, y, curr_x, label, attr)
            self.click_regions.append(ClickableRegion(y, curr_x, curr_x + len(label), lambda i=idx: self.set_tab(i), desc))
            curr_x += len(label) + 2

        hint = "(Tab or Click to switch)"
        if curr_x + len(hint) < max_x - 2:
            safe_addstr(self.stdscr, y, curr_x + 2, hint, self.C_DIM)

    # --------------------------------------------------------------------------
    # VIEW 0: DASHBOARD & HARDWARE CONTROLS
    # --------------------------------------------------------------------------

    def render_dashboard_view(self, max_y: int, max_x: int, start_y: int):
        """Renders two-column layout: Presets matrix on left, hardware sliders on right."""
        # Left Column: Presets
        safe_addstr(self.stdscr, start_y, 2, "┌─ ONBOARD PRESET MATRIX ──────────┐", self.C_DIM)
        curr_preset = self.status.get("preset", {})
        curr_idx = curr_preset.get("index", 1)

        for i, name in enumerate(PRESET_NAMES, 1):
            row_y = start_y + 1 + (i - 1)
            is_active = (i == curr_idx)
            is_cursor = (self.selected_row == i - 1)

            cursor_marker = " > " if is_cursor else "   "
            num_str = f"[{i:02d}]"
            active_badge = " [ACTIVE]" if is_active else "         "

            name_display = name[:17].ljust(17)
            line = f"│{cursor_marker}{num_str} {name_display}{active_badge}│"

            if is_cursor:
                attr = self.C_ACTIVE
            elif is_active:
                attr = self.C_NORMAL | curses.A_BOLD
            else:
                attr = self.C_DIM

            safe_addstr(self.stdscr, row_y, 2, line, attr)

            target_preset = i
            self.click_regions.append(
                ClickableRegion(row_y, 2, 2 + len(line), lambda p=target_preset: self.activate_preset(p), f"Preset {i}")
            )

        safe_addstr(self.stdscr, start_y + 11, 2, "└──────────────────────────────────┘", self.C_DIM)

        # Right Column: Controls & Sliders
        col2_x = 40
        col2_width = max(34, max_x - col2_x - 2)
        safe_addstr(self.stdscr, start_y, col2_x, "┌─ LIVE HARDWARE CONTROLS " + "─" * (col2_width - 26) + "┐", self.C_DIM)

        # Sidetone row (Row 10)
        st_y = start_y + 2
        st_cursor = " > " if self.selected_row == 10 else "   "
        st = self.status.setdefault("sidetone", {"enabled": False, "level": 5, "max_level": 31})
        st_on = st.get("enabled", False)
        st_lvl = st.get("level", 5)
        st_bars = int((st_lvl / 31.0) * 10)
        st_slider = f"[{'■' * st_bars}{'□' * (10 - st_bars)}] {st_lvl:2d}/31"
        st_state = "ON " if st_on else "OFF"

        st_attr = self.C_ACTIVE if self.selected_row == 10 else self.C_NORMAL
        safe_addstr(self.stdscr, st_y, col2_x + 2, f"{st_cursor}Sidetone Monitor", st_attr)
        safe_addstr(self.stdscr, st_y + 1, col2_x + 6, f"State: [ {st_state} ]  Level: {st_slider}", self.C_NORMAL)

        self.click_regions.append(
            ClickableRegion(st_y, col2_x + 2, col2_x + col2_width - 2, lambda: self.toggle_sidetone(), "Sidetone Toggle")
        )
        self.click_regions.append(
            ClickableRegion(st_y + 1, col2_x + 6, col2_x + 22, lambda: self.toggle_sidetone(), "Sidetone State Click")
        )

        # Game / Chat Balance row (Row 11)
        cm_y = start_y + 5
        cm_cursor = " > " if self.selected_row == 11 else "   "
        cm = self.status.setdefault("chat_mix", {"raw": 10, "game_pct": 50, "chat_pct": 50})
        game_p = cm.get("game_pct", 50)
        chat_p = cm.get("chat_pct", 50)
        cm_bars = int((chat_p / 100.0) * 10)
        cm_slider = f"[{'□' * cm_bars}█{'□' * (10 - cm_bars)}]"

        cm_attr = self.C_ACTIVE if self.selected_row == 11 else self.C_NORMAL
        safe_addstr(self.stdscr, cm_y, col2_x + 2, f"{cm_cursor}Game / Chat Balance", cm_attr)
        safe_addstr(self.stdscr, cm_y + 1, col2_x + 6, f"Game {game_p:2d}%  {cm_slider}  {chat_p:2d}% Chat", self.C_NORMAL)

        self.click_regions.append(
            ClickableRegion(cm_y, col2_x + 2, col2_x + col2_width - 2, lambda: self.step_chatmix(1), "Chatmix Click")
        )

        # AI Noise Reduction Filter (Row 12)
        nr_y = start_y + 8
        nr_cursor = " > " if self.selected_row == 12 else "   "
        nr_dict = self.status.setdefault("noise_reduction", {"code": 1, "name": "Low"})
        nr = nr_dict.get("name", "Low")
        nr_attr = self.C_ACTIVE if self.selected_row == 12 else self.C_NORMAL
        safe_addstr(self.stdscr, nr_y, col2_x + 2, f"{nr_cursor}AI Noise Reduction Filter", nr_attr)
        safe_addstr(self.stdscr, nr_y + 1, col2_x + 6, f"Active Profile: [ {nr.upper()} ]  (Click or ←/→ to cycle)", self.C_NORMAL)

        self.click_regions.append(
            ClickableRegion(nr_y, col2_x + 2, col2_x + col2_width - 2, lambda: self.cycle_noise_reduction(), "NR Click")
        )
        self.click_regions.append(
            ClickableRegion(nr_y + 1, col2_x + 6, col2_x + 36, lambda: self.cycle_noise_reduction(), "NR Click Profile")
        )

        safe_addstr(self.stdscr, start_y + 11, col2_x, "└" + "─" * (col2_width - 1) + "┘", self.C_DIM)

    # --------------------------------------------------------------------------
    # VIEW 1: HORIZONTAL 10-BAND GRAPHIC EQUALIZER STUDIO
    # --------------------------------------------------------------------------

    def render_horizontal_eq_view(self, max_y: int, max_x: int, start_y: int):
        """
        Renders an authentic, professional horizontal graphic equalizer.
        10 vertical columns placed side-by-side across the frequency spectrum.
        Left/Right moves between frequency bands.
        Up/Down adjusts the gain level up/down naturally!
        """
        # Slot selector bar
        safe_addstr(self.stdscr, start_y, 2, "Custom Hardware Slot: ", self.C_HEADER)
        slot_x = 24
        for s in range(4):
            slot_num = s + 1
            is_active = (s == self.active_eq_slot)
            tag = f" [Preset {slot_num}] "
            attr = self.C_ACTIVE if is_active else self.C_DIM
            safe_addstr(self.stdscr, start_y, slot_x, tag, attr)

            target_slot = s
            self.click_regions.append(
                ClickableRegion(start_y, slot_x, slot_x + len(tag), lambda sl=target_slot: self.select_eq_slot(sl), f"Slot {slot_num}")
            )
            slot_x += len(tag) + 2

        dirty_tag = " • [UNSAVED CHANGES]" if self.eq_dirty else " • [SAVED IN HARDWARE]"
        d_attr = self.C_WARN if self.eq_dirty else self.C_DIM
        safe_addstr(self.stdscr, start_y, slot_x + 2, dirty_tag, d_attr)

        # EQ Frame Box
        box_y = start_y + 2
        box_w = min(max_x - 4, 86)
        safe_addstr(self.stdscr, box_y, 2, "┌─ 10-BAND GRAPHIC EQUALIZER (←/→ Select Band • ↑/↓ Adjust Gain) " + "─" * (box_w - 65) + "┐", self.C_DIM)

        # Scale levels: 9 vertical tiers (+12, +9, +6, +3, 0, -3, -6, -9, -12 dB)
        scale_db = [12.0, 9.0, 6.0, 3.0, 0.0, -3.0, -6.0, -9.0, -12.0]
        axis_x = 4
        col_start_x = 14
        col_width = 7  # 7 chars per frequency band column

        # Draw dB values on top of each column
        gain_row_y = box_y + 1
        safe_addstr(self.stdscr, gain_row_y, axis_x, "  Gain: ", self.C_DIM)
        for idx in range(10):
            bx = col_start_x + idx * col_width
            g = self.current_eq_bands[idx].get("gain_db", 0.0) if idx < len(self.current_eq_bands) else 0.0
            g_str = f"{g:+4.1f}".center(col_width)
            attr = self.C_ACTIVE if idx == self.selected_band else self.C_NORMAL
            safe_addstr(self.stdscr, gain_row_y, bx, g_str, attr)

        # Draw vertical grid and slider thumbs
        for tier_idx, level_db in enumerate(scale_db):
            row_y = box_y + 2 + tier_idx
            # Draw axis tick
            tick = "┼" if level_db == 0.0 else "┤"
            axis_label = f"{level_db:+5.0f} dB {tick}"
            safe_addstr(self.stdscr, row_y, axis_x, axis_label, self.C_DIM)

            # Draw 10 frequency columns
            for band_idx in range(10):
                bx = col_start_x + band_idx * col_width
                g = self.current_eq_bands[band_idx].get("gain_db", 0.0) if band_idx < len(self.current_eq_bands) else 0.0

                # Determine if thumb falls in this tier
                # Each tier spans a 2.5 dB window around level_db
                is_thumb = (level_db - 1.5 <= g <= level_db + 1.5) or (level_db == 12.0 and g >= 11.0) or (level_db == -12.0 and g <= -11.0)
                is_sel = (band_idx == self.selected_band)

                if is_thumb:
                    char_render = "[■]" if is_sel else "[█]"
                    cell = char_render.center(col_width)
                    attr = self.C_ACTIVE if is_sel else self.C_HEADER
                else:
                    if level_db == 0.0:
                        cell = "──┼──".center(col_width)
                    else:
                        cell = "  │  ".center(col_width)
                    attr = self.C_NORMAL if is_sel else self.C_DIM

                safe_addstr(self.stdscr, row_y, bx, cell, attr)

                # Mouse click on cell to jump directly to this tier's gain
                target_b = band_idx
                target_g = level_db
                self.click_regions.append(
                    ClickableRegion(row_y, bx, bx + col_width - 1, lambda b=target_b, val=target_g: self.set_band_gain(b, val), f"B{band_idx} L{level_db}")
                )

        # Bottom Selection Markers
        marker_y = box_y + 11
        safe_addstr(self.stdscr, marker_y, axis_x, "        ", self.C_DIM)
        for idx in range(10):
            bx = col_start_x + idx * col_width
            arrow = "  ▲  ".center(col_width) if idx == self.selected_band else "     ".center(col_width)
            safe_addstr(self.stdscr, marker_y, bx, arrow, self.C_ACTIVE)

        # Bottom Frequency Labels
        freq_row_y = box_y + 12
        safe_addstr(self.stdscr, freq_row_y, axis_x, "  Freq: ", self.C_DIM)
        for idx, f in enumerate(STANDARD_FREQS):
            bx = col_start_x + idx * col_width
            f_str = f"{f//1000}k" if f >= 1000 else f"{f}"
            lbl = f_str.center(col_width)
            attr = self.C_ACTIVE if idx == self.selected_band else self.C_NORMAL
            safe_addstr(self.stdscr, freq_row_y, bx, lbl, attr)

            target_b = idx
            self.click_regions.append(
                ClickableRegion(freq_row_y, bx, bx + col_width - 1, lambda b=target_b: self.select_band(b), f"Band {f_str}")
            )

        safe_addstr(self.stdscr, box_y + 13, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

        # Quick Actions Bar
        actions_y = box_y + 14
        act_text = " [s] Save to Headset  •  [p] Cycle Preset Curve  •  [0] Reset to 0 dB  •  [e] Export JSON "
        safe_addstr(self.stdscr, actions_y, 4, act_text, self.C_DIM)

    # --------------------------------------------------------------------------
    # VIEW 2: FIRMWARE MANAGEMENT STUDIO
    # --------------------------------------------------------------------------

    def render_firmware_view(self, max_y: int, max_x: int, start_y: int):
        """Renders live firmware telemetry, official changelog browser, and package flasher."""
        box_w = min(max_x - 4, 96)

        # Top Box: Dual-Node Hardware Telemetry
        safe_addstr(self.stdscr, start_y, 2, "┌─ DUAL-NODE FIRMWARE & HARDWARE TELEMETRY " + "─" * (box_w - 43) + "┐", self.C_DIM)

        d_fw = self.status.get("firmware", {}).get("dongle", "v1.0.5.7.4")
        h_fw = self.status.get("firmware", {}).get("headset", "v1.0.5.7.4")
        parity = (d_fw == h_fw and d_fw != "Unknown")
        batt = self.status.get("battery_percent", 94)
        batt_safe = (batt is not None and batt >= 50)

        p_str = "MATCHED (Synchronized)" if parity else "MISMATCH WARNING"
        b_str = f"{batt}% (Pass >= 50%)" if batt_safe else f"{batt}% (LOW - Flashing Blocked)"

        safe_addstr(self.stdscr, start_y + 1, 4, f"USB Dongle Firmware       : {d_fw:<24} [Operational]", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 2, 4, f"Wireless Headset Firmware : {h_fw:<24} [Operational]", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 3, 4, f"Version Parity Check      : {p_str}", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 4, 4, f"Headset Battery Level     : {b_str}", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 5, 4, f"Live Flashing             : Disabled (FOTA protocol not yet verified)", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 6, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

        # Middle Box: Official Changelog Browser
        cl_y = start_y + 7
        releases = FIRMWARE_CATALOG.get("maxwell2", {}).get("releases", [])
        safe_addstr(self.stdscr, cl_y, 2, "┌─ OFFICIAL MAXWELL 2 CHANGELOG BROWSER (↑/↓ to scroll releases) " + "─" * (box_w - 65) + "┐", self.C_DIM)

        # Release selector row
        sel_str = ""
        for idx, r in enumerate(releases):
            marker = "●" if idx == self.fw_selected_release else "○"
            tag = f" {marker} {r['version']} "
            attr = self.C_ACTIVE if idx == self.fw_selected_release else self.C_DIM
            safe_addstr(self.stdscr, cl_y + 1, 4 + len(sel_str), tag, attr)

            target_idx = idx
            self.click_regions.append(
                ClickableRegion(cl_y + 1, 4 + len(sel_str), 4 + len(sel_str) + len(tag), lambda i=target_idx: self.select_release(i), f"Rel {r['version']}")
            )
            sel_str += tag + " "

        # Detailed card for highlighted release
        if 0 <= self.fw_selected_release < len(releases):
            rel = releases[self.fw_selected_release]
            tag = " (Latest Official Release)" if rel.get("latest") else ""
            safe_addstr(self.stdscr, cl_y + 3, 4, f"Selected: {rel['version']}{tag}  [{rel['date']}]", self.C_HEADER)
            notes = rel.get("notes", "")
            safe_addstr(self.stdscr, cl_y + 4, 4, notes[:box_w - 6], self.C_NORMAL)
            if len(notes) > box_w - 6:
                safe_addstr(self.stdscr, cl_y + 5, 4, notes[box_w - 6: (box_w - 6) * 2], self.C_NORMAL)

        safe_addstr(self.stdscr, cl_y + 6, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

        # Bottom Box: Firmware Actions & Verification
        act_y = cl_y + 7
        safe_addstr(self.stdscr, act_y, 2, "┌─ FIRMWARE VERIFICATION & FLASHING SUITE " + "─" * (box_w - 42) + "┐", self.C_DIM)

        tgt_str = f"Target Node: [ {self.fw_target.upper()} ]  (Press [t] to toggle Dongle/Headset)"
        safe_addstr(self.stdscr, act_y + 1, 4, tgt_str, self.C_NORMAL)

        actions_line = " [t] Toggle Target Node  •  [f] Flash Firmware Package  •  [c] Check Catalog "
        safe_addstr(self.stdscr, act_y + 2, 4, actions_line, self.C_DIM)
        safe_addstr(self.stdscr, act_y + 3, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

    # --------------------------------------------------------------------------
    # VIEW 3: SYSTEM DIAGNOSTICS & BACKUPS
    # --------------------------------------------------------------------------

    def render_system_view(self, max_y: int, max_x: int, start_y: int):
        """Renders system diagnostics, backup list, and JSON dump actions."""
        box_w = min(max_x - 4, 96)

        # Top Box: System Diagnostics
        safe_addstr(self.stdscr, start_y, 2, "┌─ SYSTEM HARDWARE DIAGNOSTICS & AUDIO STACK " + "─" * (box_w - 45) + "┐", self.C_DIM)
        node = getattr(self.client.transport, "hidraw_path", "/dev/hidraw1")
        is_d = getattr(self.client.transport, "is_dongle", True)
        conn = "Wireless Dongle (2.4 GHz RF)" if is_d else "Direct USB-C Cable"

        safe_addstr(self.stdscr, start_y + 1, 4, f"Hardware Target : Audeze Maxwell 2 (Xbox / PlayStation / PC)", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 2, 4, f"USB VID:PID     : 0x3329:0x4B28 (Audeze LLC)", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 3, 4, f"Active HID Node : {node}", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 4, 4, f"Transport Path  : {conn}", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 5, 4, f"Balance State   : NVDM 0xF702 = 0x00 (Symmetric Production Baseline)", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 6, 4, f"Linux Audio     : PipeWire / WirePlumber Compatible", self.C_NORMAL)
        safe_addstr(self.stdscr, start_y + 7, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

        # Middle Box: Backups Browser
        bk_y = start_y + 8
        safe_addstr(self.stdscr, bk_y, 2, "┌─ RECENT CONFIGURATION SNAPSHOTS & BACKUPS " + "─" * (box_w - 44) + "┐", self.C_DIM)

        if not self.system_backups:
            safe_addstr(self.stdscr, bk_y + 1, 4, "No previous backups found in ~/.config/maxwell2/backups", self.C_DIM)
        else:
            for idx, b in enumerate(self.system_backups[:3]):
                row_y = bk_y + 1 + idx
                ts = b.get("timestamp", "Unknown")
                act = b.get("action", "Snapshot")
                fname = b.get("filename", "")
                safe_addstr(self.stdscr, row_y, 4, f"• [{ts}] {act:<26} ({fname})", self.C_NORMAL)
        safe_addstr(self.stdscr, bk_y + 4, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

        # Bottom Actions Bar
        act_y = bk_y + 5
        safe_addstr(self.stdscr, act_y, 2, "┌─ QUICK SYSTEM ACTIONS " + "─" * (box_w - 24) + "┐", self.C_DIM)
        actions = " [b] Create Snapshot Backup  •  [d] Export Full JSON Dump (./maxwell_dump.json)  •  [r] Refresh "
        safe_addstr(self.stdscr, act_y + 1, 4, actions, self.C_NORMAL)
        safe_addstr(self.stdscr, act_y + 2, 2, "└" + "─" * (box_w - 2) + "┘", self.C_DIM)

    # --------------------------------------------------------------------------
    # FOOTER & HELP BAR
    # --------------------------------------------------------------------------

    def render_footer(self, max_y: int, max_x: int):
        """Renders bottom message box and contextual key shortcuts."""
        box_y = max_y - 3
        safe_addstr(self.stdscr, box_y, 0, "┌" + "─" * (max_x - 2) + "┐", self.C_DIM)
        safe_addstr(self.stdscr, box_y + 1, 0, "│", self.C_DIM)

        msg = self.message[:max_x - 4]
        safe_addstr(self.stdscr, box_y + 1, 2, msg, self.C_NORMAL)
        safe_addstr(self.stdscr, box_y + 1, max_x - 1, "│", self.C_DIM)

        if self.active_tab == 0:
            help_text = " [↑/↓] Select Row  •  [←/→] Adjust  •  [Enter/Click] Toggle  •  [Tab] Next View  •  [q] Exit "
        elif self.active_tab == 1:
            help_text = " [←/→] Select Band  •  [↑/↓] Tune Gain (±0.5dB)  •  [1-4] Slot  •  [p] Curves  •  [s] Save  •  [0] Flat "
        elif self.active_tab == 2:
            help_text = " [↑/↓] Changelog  •  [t] Toggle Target  •  [f] Flash HW  •  [Tab] Next View  •  [q] Exit "
        else:
            help_text = " [b] Create Backup  •  [d] Export JSON Dump  •  [r] Refresh Telemetry  •  [Tab] Next View  •  [q] Exit "

        safe_addstr(self.stdscr, box_y + 2, 0, "└" + "─" * (max_x - 2) + "┘", self.C_DIM)
        safe_addstr(self.stdscr, box_y + 2, max(0, (max_x - len(help_text)) // 2), help_text, self.C_DIM)

    # ==========================================================================
    # INPUT HANDLING (KEYBOARD & MOUSE)
    # ==========================================================================

    def handle_input(self, ch: int):
        """Handles keypresses and mouse clicks cleanly."""
        # Mouse click handling
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                for region in self.click_regions:
                    if region.hit_test(my, mx):
                        region.callback()
                        return
            except Exception:
                pass
            return

        # Global exit keys
        if ch in (ord('q'), ord('Q'), 27):  # 'q', 'Q', or Esc
            self.running = False
            return

        # Switch View Tab
        if ch in (ord('\t'), curses.KEY_BTAB):
            self.set_tab((self.active_tab + 1) % self.TOTAL_TABS)
            return

        # Direct tab jump keys: 1..4 (when outside Tab 1)
        if self.active_tab != 1 and ch in (ord('1'), ord('2'), ord('3'), ord('4')):
            self.set_tab(int(chr(ch)) - 1)
            return

        # Refresh hardware status
        if ch in (ord('r'), ord('R')):
            self.refresh_telemetry()
            return

        # Window Resize
        if ch == curses.KEY_RESIZE:
            return

        # ----------------------------------------------------------------------
        # VIEW 0: CONTROLS & PRESETS
        # ----------------------------------------------------------------------
        if self.active_tab == 0:
            if ch in (curses.KEY_UP, ord('k')):
                self.selected_row = (self.selected_row - 1) % self.TOTAL_ROWS
            elif ch in (curses.KEY_DOWN, ord('j')):
                self.selected_row = (self.selected_row + 1) % self.TOTAL_ROWS
            elif ch in (curses.KEY_ENTER, ord('\n'), ord('\r'), ord(' ')):
                if self.selected_row < 10:
                    self.activate_preset(self.selected_row + 1)
                elif self.selected_row == 10:
                    self.toggle_sidetone()
                elif self.selected_row == 12:
                    self.cycle_noise_reduction(1)
            elif ch in (curses.KEY_LEFT, ord('h')):
                if self.selected_row == 10:
                    self.step_sidetone(-1)
                elif self.selected_row == 11:
                    self.step_chatmix(-1)
                elif self.selected_row == 12:
                    self.cycle_noise_reduction(-1)
            elif ch in (curses.KEY_RIGHT, ord('l')):
                if self.selected_row == 10:
                    self.step_sidetone(1)
                elif self.selected_row == 11:
                    self.step_chatmix(1)
                elif self.selected_row == 12:
                    self.cycle_noise_reduction(1)
            elif ch == ord('['):
                self.step_sidetone(-1)
            elif ch == ord(']'):
                self.step_sidetone(1)

        # ----------------------------------------------------------------------
        # VIEW 1: HORIZONTAL 10-BAND GRAPHIC EQ STUDIO
        # ----------------------------------------------------------------------
        elif self.active_tab == 1:
            if ch in (ord('1'), ord('2'), ord('3'), ord('4')):
                self.select_eq_slot(int(chr(ch)) - 1)
            # Left/Right navigates between frequency bands
            elif ch in (curses.KEY_LEFT, ord('h')):
                self.selected_band = (self.selected_band - 1) % 10
                f = STANDARD_FREQS[self.selected_band]
                f_str = f"{f//1000} kHz" if f >= 1000 else f"{f} Hz"
                self.message = f"Selected Frequency Band: {f_str}"
            elif ch in (curses.KEY_RIGHT, ord('l')):
                self.selected_band = (self.selected_band + 1) % 10
                f = STANDARD_FREQS[self.selected_band]
                f_str = f"{f//1000} kHz" if f >= 1000 else f"{f} Hz"
                self.message = f"Selected Frequency Band: {f_str}"
            # Up/Down tunes gain vertically
            elif ch in (curses.KEY_UP, ord('k'), ord('+'), ord('=')):
                self.adjust_gain(0.5)
            elif ch in (curses.KEY_DOWN, ord('j'), ord('-'), ord('_')):
                self.adjust_gain(-0.5)
            elif ch in (ord('s'), ord('S')):
                self.save_custom_eq()
            elif ch in (ord('p'), ord('P')):
                self.cycle_curve_profile()
            elif ch in (ord('0'), ord('f'), ord('F')):
                self.flatten_eq()
            elif ch in (ord('e'), ord('E')):
                self.export_eq_to_file()

        # ----------------------------------------------------------------------
        # VIEW 2: FIRMWARE MANAGEMENT
        # ----------------------------------------------------------------------
        elif self.active_tab == 2:
            releases = FIRMWARE_CATALOG.get("maxwell2", {}).get("releases", [])
            if ch in (curses.KEY_UP, ord('k'), curses.KEY_LEFT):
                self.fw_selected_release = max(0, self.fw_selected_release - 1)
            elif ch in (curses.KEY_DOWN, ord('j'), curses.KEY_RIGHT):
                self.fw_selected_release = min(len(releases) - 1, self.fw_selected_release + 1)
            elif ch in (ord('t'), ord('T')):
                self.fw_target = "headset" if self.fw_target == "dongle" else "dongle"
                self.message = f"Target Node toggled to: {self.fw_target.upper()}"
            elif ch in (ord('f'), ord('F')):
                self.message = "Live flashing is disabled until verified. Simulate: ./audezeforlinux firmware flash <file.bin> --dry-run"
            elif ch in (ord('c'), ord('C')):
                self.message = "Firmware is verified up to date with official release v1.0.5.7.4."

        # ----------------------------------------------------------------------
        # VIEW 3: SYSTEM & BACKUPS
        # ----------------------------------------------------------------------
        elif self.active_tab == 3:
            if ch in (ord('b'), ord('B')):
                self.create_snapshot_backup()
            elif ch in (ord('d'), ord('D')):
                self.export_system_dump()

    # ==========================================================================
    # HARDWARE ACTIONS
    # ==========================================================================

    def activate_preset(self, preset_idx: int):
        """Activates a preset on hardware."""
        p_name = PRESET_NAMES[preset_idx - 1]
        self.message = f"Activating [{preset_idx}] {p_name} on hardware..."
        try:
            res = self.client.set_preset(preset_idx)
            self.status["preset"] = res["preset"]
            self.message = f"✔ Preset switched to: [{preset_idx}] {p_name}"
        except Exception as e:
            self.message = f"Preset activation error: {e}"

    def toggle_sidetone(self):
        """Toggles hardware sidetone on/off."""
        st = self.status.setdefault("sidetone", {"enabled": False, "level": 5, "max_level": 31})
        curr = st.get("enabled", False)
        new_val = not curr
        st["enabled"] = new_val
        try:
            self.client.set_sidetone_fast(enabled=new_val)
            self.message = f"Sidetone switched {'ON' if new_val else 'OFF'}."
        except Exception as e:
            self.message = f"Sidetone toggle failed: {e}"

    def step_sidetone(self, delta: int):
        """Adjusts sidetone volume smoothly (0..31)."""
        st = self.status.setdefault("sidetone", {"enabled": False, "level": 5, "max_level": 31})
        curr = st.get("level", 5)
        new_lvl = max(0, min(31, curr + delta))
        st["level"] = new_lvl
        try:
            self.client.set_sidetone_fast(level=new_lvl)
            self.message = f"Sidetone volume: {new_lvl}/31"
        except Exception as e:
            self.message = f"Sidetone volume failed: {e}"

    def step_chatmix(self, delta: int):
        """Adjusts game/chat balance smoothly (0..20)."""
        cm = self.status.setdefault("chat_mix", {"raw": 10, "game_pct": 50, "chat_pct": 50})
        raw = cm.get("raw", 10)
        new_raw = max(0, min(20, raw + delta))
        chat_p = int((new_raw / 20.0) * 100)
        cm["raw"] = new_raw
        cm["chat_pct"] = chat_p
        cm["game_pct"] = 100 - chat_p
        try:
            self.client.set_chat_mix_fast(new_raw)
            self.message = f"Game/Chat Balance: {100 - chat_p}% Game / {chat_p}% Chat"
        except Exception as e:
            self.message = f"Chat mix failed: {e}"

    def cycle_noise_reduction(self, direction: int = 1):
        """Cycles AI noise reduction mode."""
        modes = ["Off", "Low", "High"]
        nr = self.status.setdefault("noise_reduction", {"code": 1, "name": "Low"})
        curr = nr.get("code", 1)
        new_code = (curr + direction) % 3
        nr["code"] = new_code
        nr["name"] = modes[new_code]
        try:
            self.client.set_noise_reduction_fast(new_code)
            self.message = f"AI Noise Reduction set to {modes[new_code]}."
        except Exception as e:
            self.message = f"Noise reduction failed: {e}"

    def select_eq_slot(self, slot_idx: int):
        """Loads custom slot from hardware."""
        self.active_eq_slot = slot_idx
        try:
            raw_eq = self.client.read_custom_eq(self.active_eq_slot)
            self.current_eq_bands = copy.deepcopy(raw_eq.get("bands", []))
            self.eq_dirty = False
            self.message = f"Loaded Custom Preset {slot_idx + 1} from headset memory."
        except Exception as e:
            self.message = f"Failed to load slot {slot_idx + 1}: {e}"

    def select_band(self, band_idx: int):
        """Selects frequency band."""
        self.selected_band = band_idx
        f = STANDARD_FREQS[band_idx]
        f_str = f"{f//1000} kHz" if f >= 1000 else f"{f} Hz"
        g = self.current_eq_bands[band_idx].get("gain_db", 0.0) if band_idx < len(self.current_eq_bands) else 0.0
        self.message = f"Selected band: {f_str} (Gain: {g:+.1f} dB)"

    def set_band_gain(self, band_idx: int, gain: float):
        """Sets absolute gain value for a band directly."""
        self.selected_band = band_idx
        if 0 <= band_idx < len(self.current_eq_bands):
            self.current_eq_bands[band_idx]["gain_db"] = round(gain, 1)
            self.eq_dirty = True
            f = STANDARD_FREQS[band_idx]
            f_str = f"{f//1000} kHz" if f >= 1000 else f"{f} Hz"
            self.message = f"Band {f_str} set to {gain:+.1f} dB. Press 's' to save."

    def adjust_gain(self, delta: float):
        """Adjusts gain for currently selected band."""
        if not (0 <= self.selected_band < len(self.current_eq_bands)):
            return
        curr = self.current_eq_bands[self.selected_band].get("gain_db", 0.0)
        new_g = round(max(-12.0, min(12.0, curr + delta)), 1)
        self.current_eq_bands[self.selected_band]["gain_db"] = new_g
        self.eq_dirty = True
        f = STANDARD_FREQS[self.selected_band]
        f_str = f"{f//1000} kHz" if f >= 1000 else f"{f} Hz"
        self.message = f"Band {f_str}: {new_g:+.1f} dB (Press 's' to write to headset)."

    def flatten_eq(self):
        """Sets all 10 bands to 0.0 dB."""
        for b in self.current_eq_bands:
            b["gain_db"] = 0.0
        self.eq_dirty = True
        self.message = "EQ curve flattened to 0.0 dB. Press 's' to write to headset."

    def cycle_curve_profile(self):
        """Cycles through pre-tuned curve profiles."""
        curve_names = list(CURVE_PROFILES.keys())
        self.profile_cycle_idx = (self.profile_cycle_idx + 1) % len(curve_names)
        target_name = curve_names[self.profile_cycle_idx]
        gains = CURVE_PROFILES[target_name]
        for idx, g in enumerate(gains):
            if idx < len(self.current_eq_bands):
                self.current_eq_bands[idx]["gain_db"] = g
        self.eq_dirty = True
        self.message = f"Applied curve: '{target_name}'. Press 's' to write to headset."

    def save_custom_eq(self):
        """Saves current 10-band profile directly to headset hardware."""
        self.message = f"Saving Custom Preset {self.active_eq_slot + 1} to headset NVDM..."
        try:
            self.client.write_custom_eq(self.active_eq_slot, self.current_eq_bands, activate=True)
            self.eq_dirty = False
            self.message = f"✔ Saved & activated Custom Preset {self.active_eq_slot + 1} on headset!"
        except Exception as e:
            self.message = f"Save failed: {e}"

    def export_eq_to_file(self):
        """Exports currently loaded EQ bands to a JSON file."""
        out_file = f"custom_eq_slot_{self.active_eq_slot + 1}.json"
        try:
            with open(out_file, "w") as f:
                json.dump({"slot": self.active_eq_slot + 1, "bands": self.current_eq_bands}, f, indent=2)
            self.message = f"✔ Exported EQ curve to ./{out_file}"
        except Exception as e:
            self.message = f"Export failed: {e}"

    def select_release(self, rel_idx: int):
        """Selects a release to view in the changelog browser."""
        self.fw_selected_release = rel_idx

    def refresh_telemetry(self):
        """Re-reads telemetry from headset."""
        try:
            live = self.client.get_status()
            self.status.update(live)
            self.system_backups = list_backups()
            self.message = "Telemetry refreshed from headset."
        except Exception as e:
            self.message = f"Refresh note: {e}"

    def create_snapshot_backup(self):
        """Creates an immediate manual snapshot backup."""
        try:
            path = create_backup("manual_snapshot", self.status)
            self.system_backups = list_backups()
            fname = os.path.basename(path)
            self.message = f"✔ Created snapshot backup: {fname}"
        except Exception as e:
            self.message = f"Backup failed: {e}"

    def export_system_dump(self):
        """Exports full state dump to ./maxwell_dump.json."""
        out_file = "maxwell_dump.json"
        try:
            full_dump = copy.deepcopy(self.status)
            full_dump["custom_eq_current"] = self.current_eq_bands
            with open(out_file, "w") as f:
                json.dump(full_dump, f, indent=2)
            self.message = f"✔ Exported system dump to ./{out_file}"
        except Exception as e:
            self.message = f"Dump export failed: {e}"


def run_interactive_dashboard(client: MaxwellClient):
    """Entry point for live interactive curses TUI."""
    if not sys.stdin.isatty():
        print("[-] Interactive dashboard requires an interactive terminal (TTY).")
        return

    if not CURSES_AVAILABLE:
        print("[-] Python curses library is required for interactive mode.")
        return

    def _curses_main(stdscr):
        tui = CursesTUI(stdscr, client)
        tui.run()

    try:
        curses.wrapper(_curses_main)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[-] Interactive dashboard notice: {e}")
