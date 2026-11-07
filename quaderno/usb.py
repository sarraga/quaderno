"""Finding the device on USB and switching it to USB networking.

The device first appears as a CDC-ACM serial port (VID 0x04C5, PID 0x1656).
Writing SWITCH_TO_CDC_ECM to it makes it re-enumerate as CDC-ECM Ethernet
(PID 0x1657). Its IPv6 link-local address is derived from its serial number."""

import plistlib
import queue
import socket
import subprocess
import threading
import time
from typing import NamedTuple

from .errors import QuadernoError
from .util import HTTPS_PORT

VID = 0x04C5
PID_SERIAL = 0x1656
PID_ETHERNET = 0x1657
SWITCH_TO_CDC_ECM = bytes([0x01, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x01, 0x04])


class UsbDevice(NamedTuple):
    pid: int
    serial: str | None
    tty: str | None        # /dev/cu.usbmodem* while in serial mode


def ipv6_from_serial(serial):
    """The device's IPv6 link-local address, computed from its serial number."""
    v = (int("1" + str(serial)) * 0x9E37FFFFFFFC0001) & 0xFCFFFFFFFFFFFFFF
    h = format(v >> 16, "x").zfill(12)
    h = h[:6] + "fffe" + h[6:12]
    return "fe80::" + ":".join(h[i:i + 4] for i in range(0, 16, 4))


def _find_key(node, key):
    if key in node:
        return node[key]
    for child in node.get("IORegistryEntryChildren", []):
        found = _find_key(child, key)
        if found is not None:
            return found
    return None


def usb_devices():
    """QUADERNO devices attached over USB, read from the I/O Registry (`ioreg -a`)."""
    try:
        out = subprocess.run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"],
                             capture_output=True, timeout=15).stdout
        nodes = plistlib.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return []
    return [UsbDevice(n.get("idProduct"), n.get("USB Serial Number"), _find_key(n, "IOCalloutDevice"))
            for n in nodes if n.get("idVendor") == VID]


def switch_to_ethernet(tty):
    import termios
    import os
    fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B9600
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, SWITCH_TO_CDC_ECM)
        termios.tcdrain(fd)
    finally:
        os.close(fd)


def probe(addr, timeout=1.5):
    try:
        with socket.create_connection((addr, HTTPS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def first_reachable(cands, timeout=1.5):
    """Probe all candidates at once; return the first that accepts a connection.
    Daemon threads, so probes still waiting on other interfaces never delay exit
    (a thread pool would make the interpreter wait up to `timeout` at shutdown)."""
    results = queue.Queue()
    for a in cands:
        threading.Thread(target=lambda a=a: results.put((a, probe(a, timeout))),
                         daemon=True).start()
    for _ in cands:
        try:
            a, ok = results.get(timeout=timeout + 1)
        except queue.Empty:
            return None
        if ok:
            return a
    return None


def discover(serial_hint=None, on_switch=None):
    """Return the device's scoped IPv6 address ('fe80::...%en8'), switching it to
    USB networking first if it is in serial mode. `on_switch(tty, serial)` is
    called before each switch."""
    devs = usb_devices()
    serials = [d.serial for d in devs if d.serial] or ([serial_hint] if serial_hint else [])
    in_serial_mode = [d for d in devs if d.pid == PID_SERIAL and d.tty]
    for d in in_serial_mode:
        if on_switch:
            on_switch(d.tty, d.serial)
        switch_to_ethernet(d.tty)
    deadline = time.time() + (20 if in_serial_mode else 3)
    if not serials:
        raise QuadernoError("no QUADERNO found on USB (and no serial number to derive an address)")
    while True:
        ifaces = [n for _, n in socket.if_nameindex() if n.startswith("en")]
        found = first_reachable([f"{ipv6_from_serial(s)}%{i}" for s in serials for i in ifaces])
        if found:
            return found
        if time.time() > deadline:
            raise QuadernoError("QUADERNO is on USB but did not answer on port 8443 "
                                "(unlock/wake the device, or replug it)")
        time.sleep(1)
