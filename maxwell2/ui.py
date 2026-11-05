"""
Immersive Studio Grayscale / Monochrome terminal UI components for Audeze Maxwell 2.
Designed with a sleek, minimalist, high-contrast aesthetic.
"""

from __future__ import annotations

import sys
from typing import Dict, Any, List, Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def create_console() -> Optional["Console"]:
    if RICH_AVAILABLE:
        return Console()
    return None


def render_banner() -> str:
    return (
        "[bold white]    ___   __  ______  _______ ____ ____ [/bold white]\n"
        "[bold white]   / _ | / / / / _  \\/ __/_ // __//_  / [/bold white]\n"
        "[bold white]  / __ |/ /_/ / // // _/  / // _/  / /_ [/bold white]\n"
        "[bold white] /_/ |_|\\____/____/___/  /___/___//___/ [/bold white]\n"
        "[dim white]   M A X W E L L  2   •   L I N U X   C O N T R O L[/dim white]"
    )


def render_battery_bar(percent: Optional[int], width: int = 18, charge_state: Optional[str] = None) -> str:
    """Renders a sleek monochrome visual battery gauge, with the charging state when known."""
    if charge_state == "charging":
        charge_tag = " [bold black on white] ⚡ CHARGING (USB-C) [/]"
    elif charge_state == "charged":
        charge_tag = " [bold black on white] ✔ FULLY CHARGED (USB-C) [/]"
    else:
        charge_tag = ""

    if percent is None:
        return "[dim][ ?????????????????? ] Unknown (headset off or out of range)[/dim]" + charge_tag

    filled_len = int((percent / 100.0) * width)
    unfilled_len = width - filled_len

    if percent >= 50:
        bar = "[bold white]" + "█" * filled_len + "[/]" + "[dim]░[/dim]" * unfilled_len
        state = "High"
    elif percent >= 20:
        bar = "[white]" + "█" * filled_len + "[/]" + "[dim]░[/dim]" * unfilled_len
        state = "Good"
    else:
        bar = "[bold reverse white]" + "█" * filled_len + "[/]" + "[dim]░[/dim]" * unfilled_len
        state = "LOW"

    return f"[bold white]{percent}%[/] [{bar}] [dim]({state})[/dim]{charge_tag}"


def render_slider_bar(value: int, max_val: int = 20, width: int = 22, left_label: str = "Game", right_label: str = "Chat") -> str:
    """Renders a sleek center-notched monochrome crossfader."""
    ratio = max(0.0, min(1.0, value / float(max_val)))
    pos = int(ratio * width)

    bar_chars = []
    mid = width // 2
    for i in range(width + 1):
        if i == pos:
            bar_chars.append("[bold white]◆[/bold white]")
        elif i == mid:
            bar_chars.append("[bold white]│[/bold white]")
        elif i < pos:
            bar_chars.append("[white]━[/white]")
        else:
            bar_chars.append("[dim]─[/dim]")

    slider_str = "".join(bar_chars)
    left_pct = int((1.0 - ratio) * 100)
    right_pct = int(ratio * 100)
    return f"[bold white]{left_label}[/] {left_pct}% [{slider_str}] {right_pct}% [bold white]{right_label}[/]"


def render_sidetone_bar(level: int, max_level: int = 31, enabled: bool = True, width: int = 16) -> str:
    """Renders monochrome sidetone volume bar."""
    if not enabled:
        return "[dim]● OFF (Muted)[/dim]"
    ratio = max(0.0, min(1.0, level / float(max_level)))
    filled = int(ratio * width)
    unfilled = width - filled
    bar = "[bold white]" + "■" * filled + "[/]" + "[dim]░[/dim]" * unfilled
    return f"[bold white]● ON[/] [{bar}] [bold white]{level}[/][dim]/{max_level}[/dim]"


def render_eq_spectrum(bands: List[Dict[str, Any]], selected_band_idx: Optional[int] = None) -> Table:
    """
    Renders an ASCII/Unicode graphic equalizer spectrum analyzer in sleek studio monochrome.
    """
    table = Table(
        box=box.ROUNDED,
        title="[bold white]10-BAND GRAPHIC EQUALIZER SPECTRUM[/bold white]",
        title_justify="center",
        border_style="white",
        header_style="bold white",
    )

    table.add_column("Level", justify="right", style="dim", no_wrap=True, width=8)
    labels = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

    for i, lbl in enumerate(labels):
        if selected_band_idx is not None and selected_band_idx == i:
            header_str = f"[bold black on white] {lbl} [/]"
        else:
            header_str = lbl
        table.add_column(header_str, justify="center", width=6)

    db_steps = [12.0, 9.0, 6.0, 3.0, 0.0, -3.0, -6.0, -9.0, -12.0]
    gains = [b.get("gain_db", 0.0) for b in bands]
    while len(gains) < 10:
        gains.append(0.0)

    for db_level in db_steps:
        row = [f"{db_level:+2.0f}dB" if db_level != 0 else " 0dB"]
        for i, g in enumerate(gains):
            is_selected = (selected_band_idx is not None and selected_band_idx == i)
            if db_level == 0.0:
                if g == 0.0:
                    symbol = "[bold white]─┼─[/bold white]" if is_selected else "[white]─┼─[/white]"
                else:
                    symbol = "[bold white]│█│[/bold white]"
            elif db_level > 0:
                if g >= db_level:
                    symbol = "[bold white] █ [/bold white]" if is_selected else "[white] █ [/white]"
                elif g >= db_level - 1.5:
                    symbol = "[bold white] ▄ [/bold white]" if is_selected else "[dim white] ▄ [/dim white]"
                else:
                    symbol = "[dim] · [/dim]"
            else:
                if g <= db_level:
                    symbol = "[bold white] █ [/bold white]" if is_selected else "[white] █ [/white]"
                elif g <= db_level + 1.5:
                    symbol = "[bold white] ▀ [/bold white]" if is_selected else "[dim white] ▀ [/dim white]"
                else:
                    symbol = "[dim] · [/dim]"
            row.append(symbol)
        table.add_row(*row)

    num_row = ["[bold white]Gain[/]"]
    for i, g in enumerate(gains):
        if selected_band_idx is not None and selected_band_idx == i:
            num_row.append(f"[bold black on white]{g:+4.1f}[/]")
        else:
            style = "bold white" if g != 0 else "dim"
            num_row.append(f"[{style}]{g:+4.1f}[/]")
    table.add_row(*num_row)

    return table


def print_rich_status(status: Dict[str, Any]):
    """Renders the complete rich device dashboard view in sleek studio monochrome."""
    if not RICH_AVAILABLE:
        return

    console = Console()

    banner_text = render_banner()
    console.print()
    console.print(Align.center(banner_text))
    console.print()

    # Fields that didn't answer hold placeholders: show them as unknown, never as readings
    unread = set(status.get("unreadable") or [])
    no_reply = "[dim]Unknown (no reply)[/dim]"

    # Active preset badge (Clean inverted high-contrast pill)
    preset = status.get("preset", {})
    p_name = preset.get("name", "Unknown")
    p_idx = preset.get("index", 1)
    is_custom = preset.get("is_custom", False)

    if "preset" in unread:
        preset_badge = "[bold black on white]  ● ACTIVE PRESET: UNKNOWN (NO REPLY)  [/]"
    elif is_custom:
        slot_num = preset.get("slot", 0) + 1
        preset_badge = f"[bold black on white]  ● ACTIVE PRESET: [{p_idx}] {p_name.upper()} (CUSTOM SLOT {slot_num})  [/]"
    else:
        preset_badge = f"[bold black on white]  ● ACTIVE PRESET: [{p_idx}] {p_name.upper()}  [/]"

    console.print(Align.center(preset_badge))
    console.print()

    status_table = Table(box=box.ROUNDED, expand=True, border_style="grey70")
    status_table.add_column("Category", style="bold white", width=22)
    status_table.add_column("Hardware Status & Value", style="white")

    # Battery
    batt = status.get("battery_percent")
    status_table.add_row("Battery Gauge", render_battery_bar(batt, charge_state=status.get("battery_state")))

    # Mic Mute
    mute = status.get("mic_muted")
    if mute is True:
        mic_str = "[bold black on white] MUTED [/] [dim](Boom mic hardware switch is off)[/dim]"
    elif mute is False:
        mic_str = "[bold white]● LIVE / UNMUTED[/] [dim](Microphone transmitting)[/dim]"
    else:
        mic_str = "[dim]Unknown[/dim]"
    status_table.add_row("Microphone", mic_str)

    # Sidetone
    st = status.get("sidetone", {})
    st_str = no_reply if "sidetone" in unread else \
        render_sidetone_bar(st.get("level", 0), st.get("max_level", 31), st.get("enabled", False))
    status_table.add_row("Sidetone Monitor", st_str)

    # Game / Chat Mix
    cm = status.get("chat_mix", {})
    cm_str = no_reply if "chat_mix" in unread else render_slider_bar(cm.get("raw", 10), max_val=20)
    status_table.add_row("Game / Chat Balance", cm_str)

    # Noise Reduction
    nr = status.get("noise_reduction", {}).get("name", "Unknown")
    status_table.add_row("AI Noise Reduction", no_reply if "noise_reduction" in unread else f"[bold white]{nr}[/]")

    # Firmware & Dongle Info
    fw = status.get("firmware", {}).get("firmware", "Unknown")
    fw_str = "Unknown (no reply)" if "firmware" in unread else fw
    model = status.get("model", "Audeze Maxwell")
    status_table.add_row("Hardware Info", f"[white]{model}[/] • Firmware: [bold white]{fw_str}[/]")

    console.print(Panel(status_table, title="[bold white]HARDWARE TELEMETRY & CONTROLS[/bold white]", border_style="white", box=box.ROUNDED))
    console.print()


def print_rich_preset_success(preset: Dict[str, Any], backup_file: str, dry_run: bool = False):
    """Renders a stylish monochrome toast panel when a preset changes."""
    if not RICH_AVAILABLE:
        return

    console = Console()
    idx = preset.get("index", "?")
    name = preset.get("name", "Unknown")
    dry = " [bold dim](DRY-RUN - SIMULATED)[/]" if dry_run else ""

    text = Text()
    text.append("✔ ", style="bold white")
    text.append(f"Activated preset [{idx}] ", style="bold white")
    text.append(f"{name.upper()}{dry}\n", style="bold underline")
    text.append(f"  Safety rollback snapshot saved to: {backup_file}", style="dim")

    console.print(Panel(text, border_style="white", box=box.ROUNDED))
