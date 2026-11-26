"""Unified device discovery across USB and Wi-Fi (mDNS)."""

from collections.abc import Callable
from typing import Literal

from . import mdns, usb
from .errors import ConnectError, DeviceNotFoundError

Transport = Literal["usb", "wifi", "auto"]
TRANSPORTS: tuple[Transport, ...] = ("usb", "wifi", "auto")


def parse_transport(value: str | None) -> Transport:
    """Normalize a transport name (CLI flag or $QUADERNO_TRANSPORT). Default: 'usb'."""
    if not value:
        return "usb"
    norm = value.strip().lower()
    if norm == "usb":
        return "usb"
    if norm == "wifi":
        return "wifi"
    if norm == "auto":
        return "auto"
    raise ConnectError(f"unknown transport {value!r}: expected usb, wifi, or auto")


def _try_cached_wifi(cached_addr: str, serial_hint: str | None) -> bool:
    """Fast check whether a cached Wi-Fi IP still points to the same QUADERNO."""
    if not usb.probe(cached_addr, timeout=1.0):
        return False
    ok, serial = mdns.fetch_serial(cached_addr, timeout=1.5)
    if not ok:
        return False
    return serial_hint is None or serial == serial_hint


def _discover_wifi(serial_hint: str | None, *, cached_addr: str | None,
                   mdns_timeout: float, on_wifi_addr: Callable[[str], None] | None) -> str:
    if cached_addr and _try_cached_wifi(cached_addr, serial_hint):
        return cached_addr
    addr = mdns.discover(serial_hint, timeout=mdns_timeout)
    if on_wifi_addr is not None:
        on_wifi_addr(addr)
    return addr


def discover(serial_hint: str | None = None, *, transport: Transport = "usb",
             on_switch: usb.SwitchHandler | None = None,
             cached_addr: str | None = None, mdns_timeout: float = 5.0,
             on_wifi_addr: Callable[[str], None] | None = None) -> str:
    """Find the QUADERNO and return its address.

    - `transport="usb"` (default): USB only (macOS `ioreg`, Linux `sysfs`).
    - `transport="wifi"`: opt-in Wi-Fi (cached IP probe, then mDNS `_dp_fujitsu._tcp`).
    - `transport="auto"`: USB first when supported on this OS; falls back to
      Wi-Fi only when no matching QUADERNO is attached on USB (USB errors such
      as SerialModeError or DeviceUnreachableError are raised immediately)."""
    if transport == "usb":
        return usb.discover(serial_hint, on_switch=on_switch)
    if transport == "wifi":
        return _discover_wifi(serial_hint, cached_addr=cached_addr,
                              mdns_timeout=mdns_timeout, on_wifi_addr=on_wifi_addr)
    if not usb.is_supported():
        return _discover_wifi(serial_hint, cached_addr=cached_addr,
                              mdns_timeout=mdns_timeout, on_wifi_addr=on_wifi_addr)
    try:
        return usb.discover(serial_hint, on_switch=on_switch)
    except DeviceNotFoundError as usb_err:
        try:
            return _discover_wifi(serial_hint, cached_addr=cached_addr,
                                  mdns_timeout=mdns_timeout, on_wifi_addr=on_wifi_addr)
        except DeviceNotFoundError as wifi_err:
            if "isn't the one these credentials" in str(wifi_err):
                raise
            if "isn't the one these credentials" in str(usb_err):
                raise usb_err from None
            if "QUADERNOs on Wi-Fi" in str(wifi_err):
                raise
            if "QUADERNOs on USB" in str(usb_err):
                raise usb_err from None
            raise DeviceNotFoundError(
                "no QUADERNO found on USB or Wi-Fi: plug it in with a data cable "
                "(not a charge-only one) and wake it, or turn on Wi-Fi on the same "
                "network (or pass --addr)") from None
