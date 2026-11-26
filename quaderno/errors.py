"""Exceptions raised by the quaderno package.

    QuadernoError                    base: .status (HTTP status, when there is one)  [exit 1]
    ├── ConnectError                 can't reach the device, invalid transport,      [exit 3]
    │   │                            or OS USB-list failure
    │   ├── DeviceNotFoundError      no QUADERNO on USB or Wi-Fi, wrong serial,
    │   │                            or ambiguous multiple devices
    │   ├── SerialModeError          stuck in USB serial mode (ignored mode switch)
    │   └── DeviceUnreachableError   interface/link down, port 8443 refuses/fails
    │                                TLS pin during discovery, or connection dropped
    ├── AuthError                    credentials or device identity failure          [exit 4]
    │   ├── NotPairedError           no credentials found or incomplete pairing dir
    │   ├── LoginError               the device rejected this client's RSA signature
    │   └── CertificateError         TLS certificate differs from devicecert.bin
    │                                (or devicecert.bin is corrupt)
    ├── PairingError                 PIN pairing protocol or crypto check failed     [exit 4]
    │   └── WrongPinError            HTTP 403/40301: entered PIN did not match
    ├── RequestError                 HTTP 4xx/5xx error (.status, .code) or stalled  [exit 1]
    │   │                            chunked upload
    │   ├── LockedError              HTTP 423: resource is locked (e.g. open in viewer)
    │   └── StorageFullError         HTTP 507: insufficient storage on the device
    ├── ProtocolError                malformed, non-JSON, oversized, or out-of-range [exit 1]
    │                                device answer (also Entry.mtime bad date)
    ├── NotFoundError                remote path doesn't exist (HTTP 404) or pattern [exit 1]
    │                                matched nothing locally (not a RequestError
    │                                because pattern misses occur without an HTTP 404)
    ├── ExistsError                  target already exists (upload, mkdir, move, cp) [exit 1]
    └── NotEmptyError                folder not empty (rm without recursive)         [exit 1]

Local filesystem errors (missing local directory, permissions, disk full) are left
as standard Python `OSError`s.
"""


class QuadernoError(Exception):
    """Base exception for any failure talking to the device or carrying out an
    operation.

    Args:
        msg: Human-readable explanation suitable for stderr.
        status: HTTP status code returned by the device, or ``None``.
    """

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class ConnectError(QuadernoError):
    """The device cannot be reached, the requested transport is unknown, or the
    OS USB device list cannot be read (CLI exit code 3)."""


class DeviceNotFoundError(ConnectError):
    """No matching QUADERNO was found on USB or Wi-Fi (unplugged, charge-only
    cable, Wi-Fi off/unreachable, wrong serial, or multiple devices without
    ``--addr``)."""


class SerialModeError(ConnectError):
    """The device is on USB but stayed in CDC-ACM serial mode: it ignored the
    command that switches it to USB networking. Seen when it is in standby, e.g.
    after the computer slept. Waking it (power button) or re-plugging fixes it."""


class DeviceUnreachableError(ConnectError):
    """The device was found on USB or Wi-Fi, but has no network interface, its
    link is down, port 8443 refused (or failed TLS pin during discovery), or an
    active connection dropped mid-request."""


class AuthError(QuadernoError):
    """Credentials, private key, or device identity problems (CLI exit code 4)."""


class NotPairedError(AuthError):
    """No pairing credentials found, or a required file in the credentials
    directory is missing."""


class LoginError(AuthError):
    """The device rejected this client's key during ``PUT /auth``."""


class CertificateError(AuthError):
    """The device's TLS certificate differs from ``devicecert.bin``, or
    ``devicecert.bin`` itself cannot be parsed as a valid X.509 certificate."""


class PairingError(QuadernoError):
    """The PIN pairing protocol failed (HMAC, DH, decryption, timeout, or
    already paired without ``--force``; CLI exit code 4)."""


class WrongPinError(PairingError):
    """HTTP 403 (40301): the PIN entered doesn't match the one on the screen."""


class RequestError(QuadernoError):
    """The device answered with an HTTP error or a chunked upload stalled.

    Args:
        msg: Human-readable explanation.
        status: HTTP status code, or ``None`` for non-HTTP transfer stalls.
        code: Device JSON ``error_code`` string (e.g. ``'40006'``), or ``None``.
    """

    def __init__(self, msg: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(msg, status)
        self.code = code


class LockedError(RequestError):
    """HTTP 423 (42300): the resource is locked, e.g. currently open on the device."""


class StorageFullError(RequestError):
    """HTTP 507 (50700/50701): insufficient storage capacity on the device."""


class ProtocolError(QuadernoError):
    """The device answered with something this client cannot safely accept:
    non-JSON or oversized payloads, missing/non-string required fields,
    out-of-range upload offsets, or (from ``Entry.mtime``) an unreadable date.
    Cosmetic fields (display size, page count, download mtime) log a warning
    instead of aborting."""


class NotFoundError(QuadernoError):
    """A remote path does not exist (HTTP 404) or a glob/regex pattern matched
    no entries.

    Args:
        msg: Human-readable explanation.
        status: HTTP status code (defaults to ``404``).
        code: Device JSON ``error_code`` string, or ``None``.
    """

    def __init__(self, msg: str, status: int | None = 404, code: str | None = None) -> None:
        super().__init__(msg, status)
        self.code = code


class ExistsError(QuadernoError):
    """The target path already exists (upload, mkdir, move, or copy)."""


class NotEmptyError(QuadernoError):
    """A folder isn't empty when deleting without ``recursive=True``."""
