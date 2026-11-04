"""
Low-level Linux hidraw transport for Audeze Maxwell 2.
Uses Python standard library (os, fcntl, select, glob).
Supports both USB Control GET_REPORT (HIDIOCGINPUT) and Interrupt IN.
Supports --dry-run mode to inspect outgoing packets without transmitting.
"""

import os
import sys
import glob
import time
import errno
import fcntl
import struct
import select
import threading
from functools import wraps
from typing import Optional, List, Tuple

from maxwell2.protocol import (
    REPORT_LENGTH,
    REPORT_ID_OUTPUT,
    REPORT_ID_INPUT,
    InputStream,
    frame_output_reports,
    parse_race_packet,
    ProtocolError,
)

AUDEZE_VID = 0x3329
KNOWN_PIDS = [0x4B18, 0x4B19, 0x4B1A, 0x4B1E, 0x4B28, 0x4B29, 0x4B2A, 0x4B2E, 0x4BA8, 0x4BA9]
DONGLE_PIDS = [0x4B18, 0x4B19, 0x4B28, 0x4B29, 0x4BA8, 0x4BA9]
# Maxwell 2 ANC units. The Xbox dongle (4BA8) was reported in issue #3 and the PlayStation
# dongle (4BA9) in issue #4; the USB-C ID is still unknown.
ANC_PIDS = [0x4BA8, 0x4BA9]

# Linux HIDRAW ioctl definitions:
# HIDIOCGINPUT(len) = _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x0A, len)
IOC_READ_WRITE = 3
IOC_TYPE_H = 72  # ord('H')


def _HIDIOCGINPUT(length: int) -> int:
    return (IOC_READ_WRITE << 30) | (length << 16) | (IOC_TYPE_H << 8) | 0x0A


# Errors a write gets once the hidraw node has gone away. The headset and dongle both
# re-enumerate (with new /dev/hidrawN numbers) when the headset is switched on or off.
DEVICE_GONE_ERRNOS = (errno.ENODEV, errno.EIO, errno.EBADF, errno.ENOENT, errno.EPIPE, errno.ESHUTDOWN)


def _locked(method):
    """Serialises access to the device, so a background poll can't interleave with a request."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return guarded

class DeviceNotFoundError(Exception):
    """Raised when no compatible Audeze Maxwell device node is found."""
    pass


class DevicePermissionError(Exception):
    """Raised when the user does not have read/write access to /dev/hidraw*."""
    pass


def list_maxwell_devices() -> List[dict]:
    """
    Scans sysfs to locate all connected Audeze Maxwell hidraw nodes.
    Returns list of dicts: [{"path": "/dev/hidrawX", "pid": pid, "is_dongle": bool, "model": str}]
    """
    found = []
    pid_models = {
        0x4B28: "Maxwell 2 (Xbox Dongle)",
        0x4B29: "Maxwell 2 (PlayStation / PC Dongle)",
        0x4B2A: "Maxwell 2 (Direct USB)",
        0x4B2E: "Maxwell 2 (Xbox Direct USB)",
        0x4BA8: "Maxwell 2 ANC (Xbox Dongle)",
        0x4BA9: "Maxwell 2 ANC (PlayStation Dongle)",
        0x4B18: "Maxwell 1 (Xbox Dongle)",
        0x4B19: "Maxwell 1 (PlayStation Dongle)",
        0x4B1A: "Maxwell 1 (PlayStation USB)",
        0x4B1E: "Maxwell 1 (Xbox USB)",
    }
    for hidraw_dir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent_path = os.path.join(hidraw_dir, "device", "uevent")
        if not os.path.exists(uevent_path):
            continue
        try:
            with open(uevent_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            props = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
            hid_id = props.get("HID_ID", "")
            parts = hid_id.split(":")
            if len(parts) >= 3:
                vid = int(parts[1], 16)
                pid = int(parts[2], 16)
                if vid == AUDEZE_VID and pid in KNOWN_PIDS:
                    node_name = os.path.basename(hidraw_dir)
                    dev_path = f"/dev/{node_name}"
                    is_dongle = pid in DONGLE_PIDS
                    model = pid_models.get(pid, f"Audeze Device (PID 0x{pid:04X})")
                    found.append({
                        "path": dev_path,
                        "pid": pid,
                        "is_dongle": is_dongle,
                        "model": model,
                    })
        except Exception:
            continue
    return found


class Transport:
    """
    Handles bidirectional HID communication with the Audeze Maxwell over /dev/hidraw.
    """

    def __init__(self, hidraw_path: Optional[str] = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.hidraw_path = hidraw_path
        # A path given with --device is kept across reconnects while it still exists
        self.explicit_path = hidraw_path is not None
        self.lock = threading.RLock()
        self.fd: Optional[int] = None
        self.is_dongle = True
        self.product_id: Optional[int] = None
        self.input_stream = InputStream()
        self.pending_notifications: List[bytes] = []

    @classmethod
    def find_device(cls) -> Tuple[str, int, bool]:
        """
        Scans sysfs to locate the Audeze Maxwell hidraw node.
        Returns (hidraw_path, product_id, is_dongle).
        """
        devices = list_maxwell_devices()
        if devices:
            # A headset on USB-C can be reached without the wireless link, so prefer it.
            d = next((dev for dev in devices if not dev["is_dongle"]), devices[0])
            return d["path"], d["pid"], d["is_dongle"]

        raise DeviceNotFoundError(
            "No compatible Audeze Maxwell device found on /sys/class/hidraw/.\n"
            "Please ensure the USB dongle or headset is plugged in."
        )

    def open(self):
        """Opens the hidraw device node."""
        if self.dry_run:
            print("[DRY-RUN] Transport initialized in simulated dry-run mode (no hardware access).")
            return

        if not self.hidraw_path:
            self.hidraw_path, self.product_id, self.is_dongle = self.find_device()
        else:
            # An explicit --device still needs the right route byte for that hardware.
            for dev in list_maxwell_devices():
                if dev["path"] == self.hidraw_path:
                    self.product_id, self.is_dongle = dev["pid"], dev["is_dongle"]

        if not os.path.exists(self.hidraw_path):
            raise DeviceNotFoundError(f"Device node {self.hidraw_path} does not exist.")

        try:
            self.fd = os.open(self.hidraw_path, os.O_RDWR)
        except PermissionError:
            raise DevicePermissionError(
                f"Permission denied accessing {self.hidraw_path}.\n"
                f"To run without sudo, install the udev rule:\n"
                f"  sudo cp udev/70-audeze-maxwell.rules /etc/udev/rules.d/\n"
                f"  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                f"Then unplug and replug your Audeze dongle/headset."
            )

        # Drain any pending incoming reports
        self.drain()

    def close(self):
        """Closes the hidraw file descriptor."""
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    @_locked
    def reconnect(self):
        """Reopens the device after it re-enumerated, picking it up again under its new node."""
        self.close()
        # Node numbers get reused after re-enumeration, so only keep a path still held by a Maxwell
        still_maxwell = any(dev["path"] == self.hidraw_path for dev in list_maxwell_devices())
        if not (self.explicit_path and still_maxwell):
            self.hidraw_path = None
        self.open()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @_locked
    def drain(self, max_rounds: int = 64):
        """
        Drains stale input reports from both GET_REPORT and Interrupt IN buffers, until two
        rounds in a row come back empty. Late replies to earlier requests can pile up in the
        dongle, and a leftover battery notification would otherwise be read as the current level.
        """
        self.input_stream.clear()
        self.pending_notifications.clear()
        if self.dry_run or self.fd is None:
            return

        empty_count = 0
        for _ in range(max_rounds):
            got_data = False
            # Check HIDIOCGINPUT
            buf = bytearray(b"\x07" + b"\x00" * 61)
            try:
                fcntl.ioctl(self.fd, _HIDIOCGINPUT(REPORT_LENGTH), buf)
                if buf[1] > 0:
                    got_data = True
            except Exception:
                pass

            # Check Interrupt IN if select indicates readiness
            r, _, _ = select.select([self.fd], [], [], 0.002)
            if r:
                try:
                    os.read(self.fd, 64)
                    got_data = True
                except Exception:
                    pass

            if got_data:
                empty_count = 0
            else:
                empty_count += 1
                if empty_count >= 2:
                    break
            time.sleep(0.005)

    @_locked
    def write_control_fast(self, subcommand: int, value: int):
        """
        Fast low-latency write for interactive sliders (sidetone, chatmix, noise reduction).
        Transmits CMD_SETTING_WRITE and quickly drains the 0x5B ACK without blocking the UI.
        """
        from maxwell2.protocol import build_write_control_command
        cmd = build_write_control_command(subcommand, value)
        self.write_race_packet(cmd)
        if not self.dry_run:
            time.sleep(0.012)
            self.poll_input_report()

    @_locked
    def write_race_packet(self, race_packet: bytes, is_dongle: Optional[bool] = None,
                          reconnect: bool = True) -> List[bytes]:
        """
        Frames and transmits a RACE packet over HID output reports (Report ID 0x06).
        is_dongle overrides the route for this packet only (True = 0x80 relay, False = 0x00).
        If the device node has disappeared (headset switched on/off, cable replugged), the
        device is reopened once and the packet sent again, unless reconnect is False: a firmware
        transfer must stop rather than carry on against a device that has just rebooted.
        In dry-run mode, prints the outgoing report bytes and returns them.
        """
        if self.dry_run:
            reports = frame_output_reports(race_packet, is_dongle=self.is_dongle if is_dongle is None else is_dongle)
            print(f"[DRY-RUN] Sending RACE packet ({len(race_packet)} bytes):")
            print(f"  RACE Raw: {race_packet.hex(' ')}")
            for idx, rep in enumerate(reports):
                print(f"  HID Report #{idx + 1} (Report ID 0x{rep[0]:02X}, len {len(rep)}): {rep.hex(' ')}")
            return reports

        if self.fd is None:
            raise RuntimeError("Transport is not open.")

        try:
            return self._write_reports(race_packet, is_dongle)
        except OSError as e:
            if not reconnect or e.errno not in DEVICE_GONE_ERRNOS:
                raise
        # The route is re-read after reconnecting: the headset may now be on USB-C or the dongle.
        # A packet split over several reports may have been half sent, so it is resent whole.
        self.reconnect()
        return self._write_reports(race_packet, is_dongle)

    def _write_reports(self, race_packet: bytes, is_dongle: Optional[bool]) -> List[bytes]:
        route_flag = self.is_dongle if is_dongle is None else is_dongle
        reports = frame_output_reports(race_packet, is_dongle=route_flag)
        for rep in reports:
            written = os.write(self.fd, rep)
            if written != len(rep):
                raise IOError(f"Incomplete HID write: {written}/{len(rep)} bytes")
        return reports

    @_locked
    def poll_input_report(self) -> Optional[bytes]:
        """Polls one input report via HIDIOCGINPUT or Interrupt IN."""
        if self.dry_run or self.fd is None:
            return None

        # 1. Primary: HIDIOCGINPUT (Control GET_REPORT)
        buf = bytearray(b"\x07" + b"\x00" * 61)
        try:
            fcntl.ioctl(self.fd, _HIDIOCGINPUT(REPORT_LENGTH), buf)
            if buf[1] > 0:
                return bytes(buf)
        except Exception:
            pass

        # 2. Secondary: Interrupt IN
        r, _, _ = select.select([self.fd], [], [], 0.005)
        if r:
            try:
                data = os.read(self.fd, 64)
                if data and len(data) >= 3 and data[0] == REPORT_ID_INPUT and data[1] > 0:
                    return data
            except Exception:
                pass

        return None

    @_locked
    def read_race_packets(self, timeout_sec: float = 1.0) -> List[bytes]:
        """
        Polls incoming HID reports and returns all reassembled complete RACE packets.
        """
        if self.dry_run:
            return []

        packets = []
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                if new_packets:
                    packets.extend(new_packets)
                    break
            time.sleep(0.02)

        return packets

    @_locked
    def exchange(self, race_packet: bytes, timeout_sec: float = 1.5) -> Optional[bytes]:
        """
        Transmits a RACE packet and synchronously waits for its matching response.
        Any asynchronous notifications (0x5D) received along the way are cached in pending_notifications.
        """
        self.drain()
        self.write_race_packet(race_packet)
        if self.dry_run:
            return None

        expected_cmd = struct.unpack_from("<H", race_packet, 4)[0]
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                for p in new_packets:
                    if len(p) >= 6:
                        direction = p[1]
                        cmd = struct.unpack_from("<H", p, 4)[0]
                        if direction == 0x5B and cmd == expected_cmd:
                            return p
                        elif direction == 0x5D:
                            self.pending_notifications.append(p)
            time.sleep(0.02)

        raise TimeoutError(f"Timed out waiting for device response to command 0x{expected_cmd:04X} ({race_packet.hex(' ')})")

    @_locked
    def query_notification(self, query_packet: bytes, timeout_sec: float = 2.0) -> Optional[bytes]:
        """
        Sends a query (e.g. Battery 0x0CD6, Firmware 0x1C07) and waits for its 0x5D notification.
        Returns None on timeout, or straight away if the device's ACK reports an error status
        (no notification follows one), instead of waiting out the whole timeout.
        """
        # drain() also discards pending_notifications, which are all stale by now: an older
        # notification for this command would report an old value as the current one.
        self.drain()
        self.write_race_packet(query_packet)
        if self.dry_run:
            return None

        expected_cmd = struct.unpack_from("<H", query_packet, 4)[0]
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            report = self.poll_input_report()
            if report:
                new_packets = self.input_stream.feed(report)
                for p in new_packets:
                    if len(p) >= 6:
                        direction = p[1]
                        cmd = struct.unpack_from("<H", p, 4)[0]
                        if direction == 0x5D and cmd == expected_cmd:
                            return p
                        elif direction == 0x5B and cmd == expected_cmd and len(p) >= 7 and p[6] != 0:
                            return None
                        elif direction == 0x5D:
                            self.pending_notifications.append(p)
            time.sleep(0.02)

        return None
