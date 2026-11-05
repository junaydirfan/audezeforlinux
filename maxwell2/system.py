"""
audezeforlinux: System diagnostics, distro compatibility, and udev management.
Provides tools to verify Linux audio environments, configure udev hardware permissions,
and install desktop launcher entries across any Linux distribution.
"""

import os
import sys
import shutil
import subprocess
from typing import Dict, Any, List, Tuple

UDEV_RULE_CONTENT = """# Audeze Maxwell 1 & 2 Wireless Headset and USB Dongle udev rules
# Grants non-root user permissions to communicate with the headset's HID interface.

# Maxwell 2 USB Wireless Dongle (Xbox variant: 3329:4B28, PlayStation variant: 3329:4B29)
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b28", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b29", TAG+="uaccess", MODE="0666"

# Maxwell 2 Direct USB-C Cable (3329:4B2A)
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b2a", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b2e", TAG+="uaccess", MODE="0666"

# Maxwell 2 ANC USB Wireless Dongle (Xbox variant: 3329:4BA8, PlayStation variant: 3329:4BA9)
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4ba8", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4ba9", TAG+="uaccess", MODE="0666"

# Maxwell 1 legacy VID:PIDs
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b18", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b19", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b1a", TAG+="uaccess", MODE="0666"
KERNEL=="hidraw*", ATTRS{idVendor}=="3329", ATTRS{idProduct}=="4b1e", TAG+="uaccess", MODE="0666"
"""

UDEV_DEST_PATH = "/etc/udev/rules.d/70-audeze-maxwell.rules"


def run_doctor() -> Dict[str, Any]:
    """Runs comprehensive Linux environment diagnostics."""
    results = {}

    # 1. Python version
    py_ver = sys.version_info
    py_ok = py_ver >= (3, 8)
    results["python"] = {
        "status": py_ok,
        "detail": f"Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}",
        "advice": "Python 3.8+ required" if not py_ok else "Compatible",
    }

    # 2. Kernel hidraw support
    try:
        hidraw_nodes = [os.path.join("/dev", f) for f in os.listdir("/dev") if f.startswith("hidraw")]
    except Exception:
        hidraw_nodes = []
    results["kernel_hidraw"] = {
        "status": len(hidraw_nodes) > 0,
        "detail": f"{len(hidraw_nodes)} hidraw nodes found in /dev",
        "advice": "Kernel module 'hidraw' active",
    }

    # 3. Udev rule installation
    rule_installed = os.path.exists(UDEV_DEST_PATH)
    results["udev_rule"] = {
        "status": rule_installed,
        "detail": UDEV_DEST_PATH if rule_installed else "Not installed in /etc/udev/rules.d/",
        "advice": "Run './audezeforlinux setup-udev' to configure automatically" if not rule_installed else "Active",
    }

    # 4. Device detection & permissions
    from maxwell2.transport import list_maxwell_devices
    from maxwell2.diagnose import container_environment, diagnose
    container = container_environment()
    devices = list_maxwell_devices()
    has_device = len(devices) > 0

    denied = [d["path"] for d in devices if not os.access(d["path"], os.R_OK | os.W_OK)]
    perm_ok = has_device and not denied

    results["hardware_device"] = {
        "status": has_device,
        "detail": (f"{len(devices)} found: " + ", ".join(f"{d['path']} {d['model']}" for d in devices))
        if has_device else "No Audeze Maxwell USB device found",
        "advice": "Connect your Audeze USB dongle or headset" if not has_device else "Connected",
    }

    if not has_device:
        perm_advice = "Connect the device first"
    elif denied and container:
        perm_advice = (f"Running inside {container}: install the udev rule on the HOST, not in the container "
                       "(copy udev/70-audeze-maxwell.rules to the host's /etc/udev/rules.d/, reload udev, replug)")
    elif denied:
        perm_advice = "Run './audezeforlinux setup-udev' to grant user permissions, then replug the device"
    else:
        perm_advice = "Ready without sudo"
    results["permissions"] = {
        "status": perm_ok,
        "detail": ("Read/write access: " + ("PASS" if perm_ok else "DENIED on " + ", ".join(denied)))
        if has_device else "No device to check",
        "advice": perm_advice,
    }

    results["environment"] = {
        "status": True,  # informational: a container is fine once the host has the udev rule
        "detail": f"Inside {container}" if container else "Host system",
        "advice": "udev rules must be installed on the host" if container else "",
    }

    # 4b. Live, read-only probe: does the dongle answer, and does the headset answer through it?
    if has_device:
        live = diagnose()
        first_issue = next((n for n in live["nodes"] if n["verdict"] != "ok"), None)
        results["connection"] = {
            "status": live["ok"],
            "detail": "; ".join(f"{n['path']}: {n['verdict']}" for n in live["nodes"]) or "no control interface found",
            "advice": " ".join(first_issue["advice"]) if first_issue and not live["ok"] else "",
            "nodes": live["nodes"],
        }

    # 5. Audio server (PipeWire / WirePlumber)
    audio_server = "Linux Audio"
    pipewire_active = False
    try:
        p = subprocess.run(["pgrep", "-x", "pipewire"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            pipewire_active = True
            audio_server = "PipeWire (Running)"
    except Exception:
        pass

    results["audio_server"] = {
        "status": pipewire_active,
        "detail": audio_server,
        "advice": "PipeWire / WirePlumber audio stack active",
    }

    return results


def format_doctor_report(diag: Dict[str, Any]) -> str:
    """Formats environment diagnostics report in plain ASCII."""
    lines = []
    lines.append("┌─ AUDEZE LINUX COMPATIBILITY & ENVIRONMENT DOCTOR ────────────────────┐")
    lines.append("│                                                                      │")

    items = [
        ("Python Environment", diag.get("python", {})),
        ("Linux HID Subsystem", diag.get("kernel_hidraw", {})),
        ("Hardware Detection", diag.get("hardware_device", {})),
        ("Udev Permissions", diag.get("permissions", {})),
        ("Udev Configuration", diag.get("udev_rule", {})),
        ("Environment", diag.get("environment", {})),
        ("Headset Connection", diag.get("connection", {})),
        ("Audio Server", diag.get("audio_server", {})),
    ]
    items = [(title, info) for title, info in items if info]

    for title, info in items:
        status_sym = "✔ PASS" if info.get("status") else "✖ FAIL"
        detail = info.get("detail", "")
        lines.append(f"│  {title:<22} : [{status_sym:<6}] {detail}")

    nodes = diag.get("connection", {}).get("nodes") or []
    if nodes:
        lines.append("│                                                                      │")
        lines.append("├─ LIVE CONNECTION PROBE (read-only queries) ──────────────────────────┤")
        for n in nodes:
            lines.append(f"│  {n['path']}  {n.get('model')}  [USB interface {n.get('interface') or '?'}]")
            labels = {
                "node_firmware": "dongle itself" if "Dongle" in (n.get("model") or "") else "headset (USB)",
                "headset_preset": "headset preset",
                "headset_battery": "headset battery",
            }
            for name, q in (n.get("queries") or {}).items():
                got = "answered" if (q.get("result") or q.get("ack")) else ("error: " + q["error"] if q.get("error") else "no reply")
                lines.append(f"│     {labels.get(name, name):<16} {got:<10} ({q.get('packets', 0)} packets received)")
            if n.get("holders"):
                lines.append(f"│     also open in : {', '.join(n['holders'])}")
            for advice in n.get("advice") or []:
                lines.append(f"│     → {advice}")

    lines.append("│                                                                      │")
    lines.append("├─ RECOMMENDED ACTIONS ────────────────────────────────────────────────┤")

    all_ok = all(v.get("status", False) for v in diag.values())
    if all_ok:
        lines.append("│  ✔ All compatibility checks passed. System is fully operational.      │")
        lines.append("│    Run './audezeforlinux dashboard' to launch the Studio TUI.        │")
    else:
        lines.append("│  Issues detected:                                                    │")
        for title, info in items:
            if not info.get("status"):
                lines.append(f"│  • {title}: {info.get('advice', '')}")

    lines.append("└──────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)


def setup_udev() -> Tuple[bool, str]:
    """Installs udev rules to grant user access to Audeze Maxwell devices."""
    if os.geteuid() == 0:
        try:
            with open(UDEV_DEST_PATH, "w") as f:
                f.write(UDEV_RULE_CONTENT)
            subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
            subprocess.run(["udevadm", "trigger"], check=True)
            return True, f"✔ Udev rules successfully installed to {UDEV_DEST_PATH} and reloaded."
        except Exception as e:
            return False, f"Failed to write udev rules: {e}"
    else:
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(UDEV_RULE_CONTENT)
            tmp_path = tf.name

        cmd = f"sudo cp {tmp_path} {UDEV_DEST_PATH} && sudo udevadm control --reload-rules && sudo udevadm trigger"
        print(f"[*] Root permission required to write {UDEV_DEST_PATH}.")
        ret = os.system(cmd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if ret == 0:
            return True, f"✔ Udev rules successfully installed to {UDEV_DEST_PATH} and reloaded."
        else:
            return False, "Failed to install udev rules (command returned error or cancelled)."


def install_desktop_integration(repo_dir: str) -> Tuple[bool, str]:
    """Creates a desktop launcher entry and user PATH symlink."""
    local_bin = os.path.expanduser("~/.local/bin")
    os.makedirs(local_bin, exist_ok=True)
    symlink_path = os.path.join(local_bin, "audezeforlinux")

    source_exec = os.path.join(repo_dir, "audezeforlinux")
    if os.path.exists(symlink_path) or os.path.islink(symlink_path):
        try:
            os.unlink(symlink_path)
        except Exception:
            pass

    try:
        os.symlink(source_exec, symlink_path)
    except Exception as e:
        return False, f"Failed to create symlink: {e}"

    apps_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps_dir, exist_ok=True)
    desktop_file = os.path.join(apps_dir, "audezeforlinux.desktop")

    desktop_entry = f"""[Desktop Entry]
Name=Audeze for Linux
Comment=Studio control dashboard and firmware manager for Audeze Maxwell headsets
Exec=sh -c "{symlink_path} dashboard"
Terminal=true
Type=Application
Categories=AudioVideo;Audio;AudioVideoEditing;
Keywords=audeze;maxwell;headset;audio;eq;
Icon=audio-headphones
"""
    with open(desktop_file, "w") as f:
        f.write(desktop_entry)

    return True, f"✔ Installed desktop launcher to {desktop_file} and symlink to {symlink_path}."
