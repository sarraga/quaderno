"""Exceptions raised by the quaderno package.

    QuadernoError                    base: .status (HTTP status, when there is one)
    ├── ConnectError                 can't reach the device, or can't read the USB device list
    │   ├── DeviceNotFoundError      no QUADERNO on USB, or not the paired one
    │   ├── SerialModeError          stuck in serial mode: ignored the switch to USB networking
    │   └── DeviceUnreachableError   network mode but no interface, link down or not answering;
    │                                or the connection dropped
    ├── AuthError
    │   ├── NotPairedError           no credentials
    │   ├── LoginError               the device rejected this client's key
    │   └── CertificateError         TLS certificate differs from the one saved at pairing
    ├── PairingError
    │   └── WrongPinError
    ├── RequestError                 the device answered with an HTTP error: .status, .code
    ├── ProtocolError                the device's answer is malformed (not JSON; Entry.mtime: bad date)
    ├── NotFoundError                path doesn't exist, or a pattern matched nothing
    ├── ExistsError                  target already exists
    └── NotEmptyError                folder not empty
"""


class QuadernoError(Exception):
    """Any failure talking to the device or carrying out an operation.
    `status` is the HTTP status the device answered with, when there was one."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class ConnectError(QuadernoError):
    """The device can't be reached."""


class DeviceNotFoundError(ConnectError):
    """No QUADERNO on USB (not plugged in, or a charge-only cable), not the one
    the credentials were paired with, or none with a usable serial number."""


class SerialModeError(ConnectError):
    """The device is on USB but stayed in serial mode: it ignored the command that
    switches it to USB networking. Seen when it is in standby, e.g. after the Mac
    slept. Waking it (power button) or re-plugging it fixes this."""


class DeviceUnreachableError(ConnectError):
    """The device is in USB network mode but has no network interface, its link is
    down, or it doesn't answer; or the connection broke."""


class AuthError(QuadernoError):
    """Credentials or device identity problems."""


class NotPairedError(AuthError):
    """No pairing credentials found."""


class LoginError(AuthError):
    """The device rejected this client's key."""


class CertificateError(AuthError):
    """The device's TLS certificate differs from the one saved at pairing."""


class PairingError(QuadernoError):
    """The PIN pairing failed."""


class WrongPinError(PairingError):
    """The PIN entered doesn't match the one on the device's screen."""


class RequestError(QuadernoError):
    """The device answered with an HTTP error. `code` is its error_code, e.g. '40006'."""

    def __init__(self, msg, status=None, code=None):
        super().__init__(msg, status)
        self.code = code


class ProtocolError(QuadernoError):
    """The device answered, but with something this client can't read: a body that
    isn't JSON, or (from Entry.mtime) a date in an unexpected format. Usually means
    a firmware change. Where only a displayed or cosmetic value is affected the
    operation goes on with a logged warning instead: an unreadable size or page
    count reads as missing, and download() keeps the file without its date."""


class NotFoundError(QuadernoError):
    """A path doesn't exist on the device, or a pattern matched nothing."""

    def __init__(self, msg, status=404, code=None):
        super().__init__(msg, status)
        self.code = code


class ExistsError(QuadernoError):
    """The target already exists (upload, mkdir, move)."""


class NotEmptyError(QuadernoError):
    """A folder isn't empty (delete without recursive)."""
