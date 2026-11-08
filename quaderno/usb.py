"""Finding the device on USB and switching it to USB networking.

The device first appears as a CDC-ACM serial port (VID 0x04C5, PID 0x1656).
Writing SWITCH_TO_CDC_ECM to it makes it re-enumerate as CDC-ECM Ethernet
(PID 0x1657). macOS then gives it a network interface (e.g. en8); the I/O
Registry lists that interface and its link status under the USB device. The
device's IPv6 link-local address is derived from its serial number."""

import os
import plistlib
import re
import socket
import subprocess
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from .errors import ConnectError, DeviceNotFoundError, DeviceUnreachableError, SerialModeError
from .util import HTTPS_PORT

VID = 0x04C5
PID_SERIAL = 0x1656
PID_ETHERNET = 0x1657
SWITCH_TO_CDC_ECM = bytes([0x01, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x01, 0x04])
LINK_VALID, LINK_ACTIVE = 1, 2  # IOLinkStatus bits (IONetworkMedium.h)

RegistryNode = dict[str, Any]  # one entry of `ioreg -a` output, as plistlib decodes it
SwitchHandler = Callable[[str, "str | None"], None]  # on_switch(tty, serial)


class UsbDevice(NamedTuple):
    pid: int
    serial: str | None
    tty: str | None = None     # /dev/cu.usbmodem* while in serial mode
    iface: str | None = None   # its network interface, e.g. 'en8', while in network mode
    link: bool | None = None   # link up, per the registry (None: not reported). Only used to
                               # explain a failure: right after a plug-in it read 0 (not
                               # valid) while the device answered on that interface.


def ipv6_from_serial(serial: str) -> str:
    """The device's IPv6 link-local address, computed from its serial number."""
    v = (int("1" + str(serial)) * 0x9E37FFFFFFFC0001) & 0xFCFFFFFFFFFFFFFF
    h = format(v >> 16, "x").zfill(12)
    h = h[:6] + "fffe" + h[6:12]
    return "fe80::" + ":".join(h[i:i + 4] for i in range(0, 16, 4))


# --- the I/O Registry -------------------------------------------------------------

def _find_node(node: RegistryNode, key: str) -> RegistryNode | None:
    """The first node in this I/O Registry subtree that has `key`, or None."""
    if key in node:
        return node
    for child in node.get("IORegistryEntryChildren", []):
        found = _find_node(child, key)
        if found is not None:
            return found
    return None


def _str(node: RegistryNode, key: str) -> str | None:
    """The first string value of `key` in this subtree, or None."""
    found = _find_node(node, key)
    value = None if found is None else found[key]
    return value if isinstance(value, str) else None


def _network(node: RegistryNode) -> tuple[str | None, bool | None]:
    """(interface name, link up) of a device in network mode. The Ethernet
    controller node carries IOLinkStatus; the interface below it, its BSD name."""
    ctrl = _find_node(node, "IOLinkStatus")
    if ctrl is None:
        return _str(node, "BSD Name"), None
    status = ctrl["IOLinkStatus"]
    link = None
    if isinstance(status, int) and status & LINK_VALID:
        link = bool(status & LINK_ACTIVE)
    return _str(ctrl, "BSD Name") or _str(node, "BSD Name"), link


def usb_devices() -> list[UsbDevice]:
    """QUADERNO devices attached over USB, read from the I/O Registry (`ioreg -a`).
    Raises ConnectError if the registry can't be read, so that the failure isn't
    taken for "no device"."""
    try:
        out = subprocess.run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"],
                             capture_output=True, timeout=15, check=True).stdout
        nodes = plistlib.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError) as err:
        raise ConnectError(f"couldn't read the list of USB devices with ioreg: {err}") from err
    devs = []
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict) or n.get("idVendor") != VID:
            continue
        pid = n.get("idProduct")
        if not isinstance(pid, int):
            continue
        serial = n.get("USB Serial Number")
        iface, link = _network(n) if pid == PID_ETHERNET else (None, None)
        devs.append(UsbDevice(pid, serial if isinstance(serial, str) else None,
                              _str(n, "IOCalloutDevice"), iface, link))
    return devs


# --- switching and probing ----------------------------------------------------------

def switch_to_ethernet(tty: str) -> None:
    import termios
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


def probe(addr: str, timeout: float = 1.5) -> bool:
    """Whether the device's HTTPS port accepts a connection at `addr`."""
    try:
        with socket.create_connection((addr, HTTPS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _choose(devs: list[UsbDevice], serial_hint: str | None) -> tuple[UsbDevice, str]:
    """The device to use, and the serial its address is derived from. With a
    serial from pairing, only that device; without one, the only device."""
    listed = ", ".join(d.serial or "unknown" for d in devs)
    if serial_hint:
        same = [d for d in devs if d.serial == serial_hint]
        if same:
            return same[0], serial_hint
        if len(devs) == 1 and not devs[0].serial:  # reports no serial: take it to be the paired one
            return devs[0], serial_hint
        raise DeviceNotFoundError(
            f"the QUADERNO on USB (serial {listed}) isn't the one these credentials were paired "
            f"with ({serial_hint}): plug that one in, or pair with this one using --config DIR")
    if len(devs) > 1:
        raise DeviceNotFoundError(f"{len(devs)} QUADERNOs on USB (serials {listed}): "
                                  "connect only the one to use")
    if not devs[0].serial:
        raise DeviceNotFoundError("the QUADERNO on USB reports no serial number, so its "
                                  "address can't be derived")
    return devs[0], devs[0].serial


def _leave_serial_mode(dev: UsbDevice, on_switch: SwitchHandler | None, timeout: float,
                       resend_every: float) -> None:
    """Send the switch command until `dev` is no longer in serial mode, resending
    it: a device in standby may ignore the first ones."""
    deadline = time.time() + timeout
    next_send = 0.0
    while True:
        stuck = [d for d in usb_devices() if d.serial == dev.serial and d.pid == PID_SERIAL]
        if not stuck:
            return
        now = time.time()
        if now >= deadline:
            raise SerialModeError(
                "the QUADERNO is connected but stayed in USB serial mode: it ignored the "
                f"command that switches it to USB networking for {timeout:g} s. This happens "
                "when it is in standby, e.g. after the Mac slept. Wake it with its power "
                "button, or unplug and re-plug it, then try again.")
        if now >= next_send:
            for d in stuck:
                if d.tty:  # the tty node can lag behind enumeration
                    if on_switch:
                        on_switch(d.tty, d.serial)
                    try:
                        switch_to_ethernet(d.tty)
                    except OSError:
                        pass  # the port vanished mid-switch: it is re-enumerating
            next_send = now + resend_every
        time.sleep(0.5)


def _no_answer(dev: UsbDevice, ip: str) -> ConnectError:
    """The error for a device that didn't answer in time, by what the Mac sees."""
    if dev.pid == PID_SERIAL:
        return SerialModeError("the QUADERNO went back to USB serial mode: unplug and re-plug it")
    if not dev.iface:
        return DeviceUnreachableError(
            "the QUADERNO is in USB network mode, but macOS lists no network interface for it: "
            "unplug and re-plug it")
    if dev.link is False:
        return DeviceUnreachableError(
            f"the QUADERNO's USB network link ({dev.iface}) is down: wake or unlock it, "
            "or unplug and re-plug it")
    return DeviceUnreachableError(
        f"the QUADERNO is on {dev.iface} and its link is up, but it doesn't answer at "
        f"{ip} on port {HTTPS_PORT}: wake or unlock it, or unplug and re-plug it")


def discover(serial_hint: str | None = None, on_switch: SwitchHandler | None = None,
             switch_timeout: float = 15, answer_timeout: float | None = None) -> str:
    """Return the device's scoped IPv6 address ('fe80::...%en8'), switching it to
    USB networking first if it is in serial mode. `serial_hint` (the serial saved
    at pairing) selects the device; `on_switch(tty, serial)` is called before each
    switch command.

    Raises DeviceNotFoundError (not on USB, not the paired device, or no usable
    serial), SerialModeError (won't leave serial mode within `switch_timeout` s),
    DeviceUnreachableError (no network interface, link down, or not answering),
    or ConnectError (the USB device list can't be read)."""
    devs = usb_devices()
    if not devs:
        raise DeviceNotFoundError("no QUADERNO on USB: plug it in with a data cable "
                                  "(not a charge-only one) and wake it")
    dev, serial = _choose(devs, serial_hint)
    if not re.fullmatch(r"[0-9]+", serial):
        raise DeviceNotFoundError(f"serial {serial!r} isn't numeric, so the QUADERNO's "
                                  "address can't be derived")
    ip = ipv6_from_serial(serial)
    switched = dev.pid == PID_SERIAL
    if switched:
        _leave_serial_mode(dev, on_switch, switch_timeout, resend_every=3)
    if answer_timeout is None:
        answer_timeout = 15 if switched else 3  # a fresh network link needs a few seconds
    deadline = time.time() + answer_timeout
    while True:
        cur = next((d for d in usb_devices() if d.serial == dev.serial), None)
        if cur is None:
            raise DeviceNotFoundError("the QUADERNO was disconnected")
        if cur.iface and probe(f"{ip}%{cur.iface}"):
            return f"{ip}%{cur.iface}"
        if time.time() > deadline:
            raise _no_answer(cur, ip)
        time.sleep(1)
