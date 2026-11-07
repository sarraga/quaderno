"""Python API and command-line client for the Fujitsu QUADERNO (Sony DPT) over USB.

    import quaderno
    with quaderno.connect() as dev:          # find the device on USB and log in
        for e in dev.entries("Books"):
            print(e.path, e.size)
        dev.get("Books/x.pdf", "~/Downloads")
        dev.put("paper.pdf", "Books")
        dev.sync("~/QUADERNO")

Pair first with `quaderno pair` (or quaderno.pair(read_pin)). See device.Device
for all operations and errors.py for the exception hierarchy.
"""

from .device import Device, PutResult, connect
from .entry import Entry
from .errors import (AuthError, CertificateError, ConnectError, DeviceNotFoundError,
                     DeviceUnreachableError, ExistsError, LoginError, NotEmptyError,
                     NotFoundError, NotPairedError, PairingError, QuadernoError, RequestError,
                     SerialModeError, WrongPinError)
from .events import Event
from .pairing import PairResult, pair

__version__ = "0.3.0"
__all__ = ["connect", "pair", "Device", "Entry", "Event", "PutResult", "PairResult",
           "QuadernoError", "ConnectError", "DeviceNotFoundError", "SerialModeError",
           "DeviceUnreachableError", "AuthError", "NotPairedError", "LoginError",
           "CertificateError", "PairingError", "WrongPinError", "RequestError",
           "NotFoundError", "ExistsError", "NotEmptyError", "__version__"]
