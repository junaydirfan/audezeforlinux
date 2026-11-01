#!/usr/bin/env python3
"""
Hardware discovery and HID report descriptor parser for Audeze Maxwell devices.
Uses standard library only (inspects Linux sysfs and /dev/hidraw).
Safe and strictly read-only: does not send any data to the device.
"""

import os
import sys
import glob
import struct
from typing import Dict, List, Optional, Tuple, Any

AUDEZE_VID = 0x3329

# Known product IDs
KNOWN_PRODUCTS = {
    0x4B18: "Maxwell 1 (Xbox Dongle)",
    0x4B19: "Maxwell 1 (PlayStation Dongle)",
    0x4B1A: "Maxwell 1 (PlayStation USB-C)",
    0x4B1E: "Maxwell 1 (Xbox USB-C)",
    0x4B28: "Maxwell 2 (Xbox Dongle)",
    0x4B29: "Maxwell 2 (PlayStation / PC Dongle)",
    0x4B2A: "Maxwell 2 (USB-C Direct)",
    0x4B2E: "Maxwell 2 (Xbox USB-C Direct)",
    0x4BA8: "Maxwell 2 ANC (Xbox Dongle)",
    0x4BA9: "Maxwell 2 ANC (PlayStation Dongle)",
}

USAGE_PAGE_NAMES = {
    0x01: "Generic Desktop",
    0x08: "LEDs",
    0x0B: "Telephony",
    0x0C: "Consumer Devices",
    0xFF13: "Vendor-Defined (Audeze / Airoha RACE Control)",
}

USB_INTERFACE_CLASSES = {
    0x01: "Audio (UAC)",
    0x02: "Communications",
    0x03: "Human Interface Device (HID)",
}


class HIDReportParser:
    """Parses raw USB HID report descriptors into structured collections and report definitions."""

    def __init__(self, raw_bytes: bytes):
        self.raw = raw_bytes
        self.reports: Dict[int, Dict[str, Any]] = {}  # report_id -> info
        self.collections: List[Dict[str, Any]] = []

    def parse(self) -> Dict[str, Any]:
        i = 0
        usage_page = 0
        report_size = 0
        report_count = 0
        report_id = 0
        usages: List[int] = []
        collection_stack: List[str] = []

        while i < len(self.raw):
            prefix = self.raw[i]
            if prefix == 0xFE:  # Long item
                size = self.raw[i + 1] if i + 1 < len(self.raw) else 0
                i += 3 + size
                continue

            size_code = prefix & 0x03
            size = [0, 1, 2, 4][size_code]
            data_bytes = self.raw[i + 1 : i + 1 + size]
            data = 0
            if size == 1:
                data = data_bytes[0]
            elif size == 2:
                data = struct.unpack("<H", data_bytes)[0]
            elif size == 4:
                data = struct.unpack("<I", data_bytes)[0]

            item_type = (prefix >> 2) & 0x03
            item_tag = (prefix >> 4) & 0x0F
            i += 1 + size

            if item_type == 1:  # Global
                if item_tag == 0x0:
                    usage_page = data
                elif item_tag == 0x7:
                    report_size = data
                elif item_tag == 0x8:
                    report_id = data
                    if report_id not in self.reports:
                        self.reports[report_id] = {
                            "inputs": [],
                            "outputs": [],
                            "features": [],
                            "usage_page": usage_page,
                        }
                elif item_tag == 0x9:
                    report_count = data

            elif item_type == 2:  # Local
                if item_tag == 0x0:
                    usages.append(data)

            elif item_type == 0:  # Main
                if item_tag == 0x8:  # Input
                    entry = {
                        "usage_page": usage_page,
                        "usages": list(usages),
                        "bits": report_size * report_count,
                        "bytes": (report_size * report_count + 7) // 8,
                        "flags": data,
                    }
                    if report_id not in self.reports:
                        self.reports[report_id] = {"inputs": [], "outputs": [], "features": [], "usage_page": usage_page}
                    self.reports[report_id]["inputs"].append(entry)
                    usages.clear()
                elif item_tag == 0x9:  # Output
                    entry = {
                        "usage_page": usage_page,
                        "usages": list(usages),
                        "bits": report_size * report_count,
                        "bytes": (report_size * report_count + 7) // 8,
                        "flags": data,
                    }
                    if report_id not in self.reports:
                        self.reports[report_id] = {"inputs": [], "outputs": [], "features": [], "usage_page": usage_page}
                    self.reports[report_id]["outputs"].append(entry)
                    usages.clear()
                elif item_tag == 0xB:  # Feature
                    entry = {
                        "usage_page": usage_page,
                        "usages": list(usages),
                        "bits": report_size * report_count,
                        "bytes": (report_size * report_count + 7) // 8,
                        "flags": data,
                    }
                    if report_id not in self.reports:
                        self.reports[report_id] = {"inputs": [], "outputs": [], "features": [], "usage_page": usage_page}
                    self.reports[report_id]["features"].append(entry)
                    usages.clear()
                elif item_tag == 0xA:  # Collection
                    coll_type = "Application" if data == 0x01 else f"Type {data}"
                    collection_stack.append(coll_type)
                    self.collections.append({
                        "usage_page": usage_page,
                        "usages": list(usages),
                        "type": coll_type,
                    })
                    usages.clear()
                elif item_tag == 0xC:  # End Collection
                    if collection_stack:
                        collection_stack.pop()

        return {"reports": self.reports, "collections": self.collections}


def scan_usb_devices() -> List[Dict[str, Any]]:
    """Scans sysfs for USB devices matching Audeze VID."""
    found = []
    for uevent_path in glob.glob("/sys/bus/usb/devices/[0-9]*/uevent"):
        # Ignore interface directories like 1-12.2.1:1.0
        parent_name = os.path.basename(os.path.dirname(uevent_path))
        if ":" in parent_name:
            continue
        try:
            with open(uevent_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            props = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
            product_str = props.get("PRODUCT", "")
            if product_str:
                parts = product_str.split("/")
                vid = int(parts[0], 16)
                pid = int(parts[1], 16)
                if vid == AUDEZE_VID:
                    dev_dir = os.path.dirname(uevent_path)
                    found.append({
                        "dev_dir": dev_dir,
                        "vid": vid,
                        "pid": pid,
                        "product_name": KNOWN_PRODUCTS.get(pid, f"Unknown Audeze (0x{pid:04X})"),
                    })
        except Exception:
            continue
    return found


def scan_hidraw_devices() -> List[Dict[str, Any]]:
    """Scans /sys/class/hidraw for Audeze nodes."""
    nodes = []
    for hidraw_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent_file = os.path.join(hidraw_path, "device", "uevent")
        if not os.path.exists(uevent_file):
            continue
        try:
            with open(uevent_file, "r", encoding="utf-8", errors="ignore") as f:
                props = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)

            hid_id = props.get("HID_ID", "")
            # Format: 0003:00003329:00004B28
            parts = hid_id.split(":")
            if len(parts) >= 3:
                vid = int(parts[1], 16)
                pid = int(parts[2], 16)
                if vid == AUDEZE_VID:
                    rdesc_path = os.path.join(hidraw_path, "device", "report_descriptor")
                    rdesc = b""
                    if os.path.exists(rdesc_path):
                        with open(rdesc_path, "rb") as rf:
                            rdesc = rf.read()

                    nodes.append({
                        "node": os.path.basename(hidraw_path),
                        "dev_node": f"/dev/{os.path.basename(hidraw_path)}",
                        "vid": vid,
                        "pid": pid,
                        "name": props.get("HID_NAME", "Unknown"),
                        "phys": props.get("HID_PHYS", ""),
                        "report_descriptor": rdesc,
                    })
        except Exception:
            continue
    return nodes


def inspect_interfaces(usb_dev_dir: str) -> List[Dict[str, Any]]:
    """Finds all USB interfaces on the device and describes whether they are Audio or HID."""
    interfaces = []
    for intf_dir in sorted(glob.glob(os.path.join(usb_dev_dir, "*:*"))):
        if not os.path.isdir(intf_dir):
            continue
        try:
            bIntfNum = 0
            bIntfClass = 0
            bIntfSubClass = 0
            bIntfProtocol = 0

            with open(os.path.join(intf_dir, "bInterfaceNumber"), "r") as f:
                bIntfNum = int(f.read().strip(), 16)
            with open(os.path.join(intf_dir, "bInterfaceClass"), "r") as f:
                bIntfClass = int(f.read().strip(), 16)
            with open(os.path.join(intf_dir, "bInterfaceSubClass"), "r") as f:
                bIntfSubClass = int(f.read().strip(), 16)
            with open(os.path.join(intf_dir, "bInterfaceProtocol"), "r") as f:
                bIntfProtocol = int(f.read().strip(), 16)

            driver_link = os.path.join(intf_dir, "driver")
            driver = os.path.basename(os.readlink(driver_link)) if os.path.islink(driver_link) else "none"

            class_desc = USB_INTERFACE_CLASSES.get(bIntfClass, f"Class 0x{bIntfClass:02X}")
            subclass_desc = ""
            if bIntfClass == 0x01:
                subclass_desc = "Audio Control" if bIntfSubClass == 1 else "Audio Streaming"

            interfaces.append({
                "interface_num": bIntfNum,
                "class_id": bIntfClass,
                "class_desc": class_desc,
                "subclass_id": bIntfSubClass,
                "subclass_desc": subclass_desc,
                "protocol": bIntfProtocol,
                "driver": driver,
            })
        except Exception:
            continue
    return interfaces


def main():
    print("=" * 70)
    print("  AUDEZE MAXWELL HARDWARE DISCOVERY & ENUMERATION TOOL (READ-ONLY)")
    print("=" * 70)

    devices = scan_usb_devices()
    if not devices:
        print("[-] No Audeze USB devices found (VID 0x3329).")
        sys.exit(1)

    for dev in devices:
        vid, pid = dev["vid"], dev["pid"]
        print(f"\n[+] Found Device: {dev['product_name']}")
        print(f"    USB VID:PID  : 0x{vid:04X}:0x{pid:04X}")
        print(f"    Sysfs Path   : {dev['dev_dir']}")

        # Enumerate interfaces
        interfaces = inspect_interfaces(dev["dev_dir"])
        print("\n    USB Interfaces:")
        for intf in interfaces:
            sub = f" ({intf['subclass_desc']})" if intf['subclass_desc'] else ""
            print(f"      - Interface #{intf['interface_num']}: {intf['class_desc']}{sub} [Driver: {intf['driver']}]")

    hidraw_nodes = scan_hidraw_devices()
    print("\n[+] HIDraw Nodes:")
    if not hidraw_nodes:
        print("    [-] No matching hidraw nodes found.")
    for h in hidraw_nodes:
        print(f"    Node: {h['dev_node']} ({h['name']})")
        print(f"      Phys: {h['phys']}")
        rdesc = h["report_descriptor"]
        print(f"      Report Descriptor Size: {len(rdesc)} bytes")

        if rdesc:
            parser = HIDReportParser(rdesc)
            parsed = parser.parse()
            print("\n      Parsed Collections:")
            for c in parsed["collections"]:
                up_name = USAGE_PAGE_NAMES.get(c["usage_page"], f"0x{c['usage_page']:04X}")
                print(f"        - {c['type']} on Usage Page: {up_name}")

            print("\n      Parsed Reports:")
            for r_id, r_info in sorted(parsed["reports"].items()):
                up_name = USAGE_PAGE_NAMES.get(r_info["usage_page"], f"0x{r_info['usage_page']:04X}")
                ins = sum(item["bytes"] for item in r_info["inputs"])
                outs = sum(item["bytes"] for item in r_info["outputs"])
                feats = sum(item["bytes"] for item in r_info["features"])

                types_summary = []
                if ins:
                    types_summary.append(f"Input ({ins} bytes payload)")
                if outs:
                    types_summary.append(f"Output ({outs} bytes payload)")
                if feats:
                    types_summary.append(f"Feature ({feats} bytes payload)")

                summary = ", ".join(types_summary)
                print(f"        * Report ID 0x{r_id:02X} ({r_id:2d}): UsagePage {up_name} -> {summary}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
