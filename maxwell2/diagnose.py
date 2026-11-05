"""
Live, read-only connection diagnosis for `doctor`.

Sends a few read-only queries to every Maxwell node and works out why data isn't coming back.
The causes it tells apart, from field reports and our own hardware:

- The dongle answers, but the headset doesn't: the headset is off or not linked to the dongle.
- Nothing answers at all, not even the dongle itself: the dongle can get stuck in a state where it
  enumerates, plays audio and even applies setting writes, yet answers every query with an empty
  report until it is unplugged and plugged back in. Reported for the original Maxwell and the
  Maxwell 2 alike (HeadsetControl issues #460 and #573).
- The node can't be opened: missing udev rule, or the rule was installed inside a container
  (distrobox / toolbox, common on Bazzite and other immutable distros) instead of on the host.
- Other programs hold the node open and may consume the replies.

Only read requests are sent: firmware version (0x1C07), battery (0x0CD6), active preset (0x0901).
"""

import os
import struct
import time
from typing import Any, Dict, List, Optional

from maxwell2.protocol import (
    InputStream,
    ProtocolError,
    build_battery_command,
    build_firmware_command,
    build_read_preset_command,
)
from maxwell2.transport import (
    DeviceBusyError,
    DeviceNotFoundError,
    DevicePermissionError,
    Transport,
    list_maxwell_devices,
)

QUERY_TIMEOUT_SEC = 1.5

VERDICT_OK = "ok"
VERDICT_HEADSET_SILENT = "headset_silent"
VERDICT_NO_REPLIES = "no_replies"
VERDICT_UNEXPECTED = "unexpected_replies"
VERDICT_PERMISSION = "permission_denied"
VERDICT_BUSY = "busy"
VERDICT_OPEN_FAILED = "open_failed"


def container_environment() -> Optional[str]:
    """Name of the container this runs in (distrobox, toolbox, flatpak, docker...), or None on the host."""
    if os.path.exists("/.flatpak-info"):
        return "flatpak"
    if os.environ.get("DISTROBOX_ENTER_PATH") or os.environ.get("CONTAINER_ID") and os.path.exists("/run/.containerenv"):
        return "distrobox"
    if os.environ.get("TOOLBOX_PATH") or os.path.exists("/run/.toolboxenv"):
        return "toolbox"
    if os.path.exists("/run/.containerenv") or os.environ.get("container"):
        return os.environ.get("container") or "container"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return None


def node_holders(path: str) -> List[str]:
    """Other processes (of this user) that have the device node open, as 'name (pid)'."""
    holders = []
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return holders
    for pid in pids:
        if int(pid) == me:
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # another user's process, or gone
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) == path:
                    try:
                        with open(f"/proc/{pid}/comm") as f:
                            name = f.read().strip()
                    except OSError:
                        name = "?"
                    holders.append(f"{name} ({pid})")
                    break
            except OSError:
                continue
    return holders


def _query(transport: Transport, packet: bytes, route: bool, timeout: Optional[float] = None) -> Dict[str, Any]:
    """
    Sends one read request and records what comes back: the number of RACE packets received
    (any command), and the reply to this request (a 0x5B acknowledgement and/or a 0x5D result).
    """
    cmd = struct.unpack_from("<H", packet, 4)[0]
    stats: Dict[str, Any] = {"packets": 0, "ack": None, "result": None, "error": None}
    try:
        transport.drain()
        transport.write_race_packet(packet, is_dongle=route, reconnect=False)
    except (OSError, RuntimeError) as e:
        stats["error"] = str(e)
        return stats
    stream = InputStream()
    deadline = time.time() + (QUERY_TIMEOUT_SEC if timeout is None else timeout)
    while time.time() < deadline:
        report = transport.poll_input_report()
        if not report:
            time.sleep(0.02)
            continue
        try:
            packets = stream.feed(report)
        except ProtocolError:
            continue
        for p in packets:
            stats["packets"] += 1
            if len(p) < 6 or struct.unpack_from("<H", p, 4)[0] != cmd:
                continue
            if p[1] == 0x5B and stats["ack"] is None:
                stats["ack"] = p.hex(" ")
                # A read setting reply carries its value in the ack; a failed ack ends the wait
                if cmd == 0x0901 or (len(p) > 6 and p[6] != 0):
                    return stats
            elif p[1] == 0x5D:
                stats["result"] = p.hex(" ")
                return stats
    return stats


def _answered(stats: Dict[str, Any]) -> bool:
    if stats["result"]:
        return True
    ack = stats["ack"]
    if not ack:
        return False
    ack_bytes = bytes.fromhex(ack)
    cmd = struct.unpack_from("<H", ack_bytes, 4)[0]
    # 0x0901 replies: 05 5B 06 00 01 09 <key> 00 <status> <value>
    return cmd == 0x0901 and len(ack_bytes) >= 10 and ack_bytes[8] == 0


def probe_node(dev: Dict[str, Any]) -> Dict[str, Any]:
    """Probes one hidraw node with read-only queries and classifies the result."""
    path = dev["path"]
    result: Dict[str, Any] = {
        "path": path,
        "model": dev.get("model"),
        "pid": f"{dev['pid']:04X}",
        "interface": dev.get("interface"),
        "has_control": dev.get("has_control"),
        "holders": node_holders(path),
        "queries": {},
    }
    transport = Transport(hidraw_path=path)
    try:
        transport.open()
    except DevicePermissionError:
        result["verdict"] = VERDICT_PERMISSION
        return result
    except DeviceBusyError as e:
        result["verdict"] = VERDICT_BUSY
        result["detail"] = str(e)
        return result
    except (DeviceNotFoundError, OSError) as e:
        result["verdict"] = VERDICT_OPEN_FAILED
        result["detail"] = str(e)
        return result

    try:
        dongle = bool(dev.get("is_dongle"))
        # Route 0x00 reaches the node itself: the dongle, or the headset on its own USB node
        result["queries"]["node_firmware"] = _query(transport, build_firmware_command(), route=False)
        headset_route = dongle  # 0x80 relays to the headset through a dongle
        result["queries"]["headset_preset"] = _query(transport, build_read_preset_command(), route=headset_route)
        result["queries"]["headset_battery"] = _query(transport, build_battery_command(), route=headset_route)
    finally:
        transport.close()

    q = result["queries"]
    errors = [s["error"] for s in q.values() if s["error"]]
    total_packets = sum(s["packets"] for s in q.values())
    headset_ok = _answered(q["headset_preset"]) or _answered(q["headset_battery"])
    node_alive = q["node_firmware"]["packets"] > 0
    if errors and not total_packets:
        result["verdict"] = VERDICT_OPEN_FAILED
        result["detail"] = errors[0]
    elif headset_ok:
        result["verdict"] = VERDICT_OK
    elif not total_packets:
        result["verdict"] = VERDICT_NO_REPLIES
    elif dongle and node_alive:
        result["verdict"] = VERDICT_HEADSET_SILENT
    else:
        result["verdict"] = VERDICT_UNEXPECTED
    return result


def advice_for(result: Dict[str, Any], container: Optional[str]) -> List[str]:
    """Plain-language advice for one probed node."""
    verdict = result["verdict"]
    dongle = "Dongle" in (result.get("model") or "")
    lines: List[str] = []
    if verdict == VERDICT_OK:
        lines.append("Headset answers normally.")
    elif verdict == VERDICT_HEADSET_SILENT:
        lines.append("The dongle answers, but the headset doesn't. Switch the headset on and check it's linked "
                     "to the dongle (its LED shows a solid connection). Re-pair it if it stays silent.")
    elif verdict == VERDICT_NO_REPLIES:
        if dongle:
            lines.append("Nothing answers, not even the dongle itself. Unplug the dongle, wait 5 seconds and plug it "
                         "back in: Maxwell dongles can get stuck answering every query with an empty report until "
                         "they're replugged, even though audio keeps working.")
        else:
            lines.append("The headset's USB connection doesn't answer. Unplug the USB-C cable, wait 5 seconds and "
                         "plug it back in, or try another cable or port.")
    elif verdict == VERDICT_UNEXPECTED:
        lines.append("The device sent data, but not the expected replies. Please report this with the output of "
                     "'audezeforlinux doctor --json'.")
    elif verdict == VERDICT_PERMISSION:
        if container:
            lines.append(f"Permission denied, and this is running inside {container}. udev rules have to be installed "
                         "on the host, not in the container: on the host, copy udev/70-audeze-maxwell.rules to "
                         "/etc/udev/rules.d/, run 'sudo udevadm control --reload-rules && sudo udevadm trigger', "
                         "then replug the device.")
        else:
            lines.append("Permission denied. Run 'audezeforlinux setup-udev', then unplug and replug the device.")
    elif verdict == VERDICT_BUSY:
        lines.append(result.get("detail") or "Another audezeforlinux instance has the device open.")
    else:
        lines.append(f"Could not open the device: {result.get('detail', 'unknown error')}")
    if result.get("holders") and verdict != VERDICT_OK:
        lines.append("Other programs have this device open and may take its replies: "
                     f"{', '.join(result['holders'])}. Close them and retry.")
    return lines


def diagnose() -> Dict[str, Any]:
    """Probes every Maxwell node. Read-only."""
    container = container_environment()
    all_nodes = list_maxwell_devices(include_all=True)
    control_nodes = [d for d in all_nodes if d.get("has_control") is not False]
    results = []
    for dev in control_nodes:
        r = probe_node(dev)
        r["advice"] = advice_for(r, container)
        results.append(r)
    return {
        "container": container,
        "nodes": results,
        "skipped_nodes": [{"path": d["path"], "interface": d.get("interface")} for d in all_nodes
                          if d.get("has_control") is False],
        # Every node must answer: the headset reports over its own USB-C node even when switched off,
        # so one healthy node doesn't mean the wireless path works.
        "ok": bool(results) and all(r["verdict"] == VERDICT_OK for r in results),
    }
