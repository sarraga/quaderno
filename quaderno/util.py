"""Path, name and formatting helpers shared by the API and the CLI."""

import base64
import binascii
import datetime
import fnmatch
import re
import sys
import unicodedata
from collections.abc import Callable
from typing import Any

from .errors import ProtocolError, QuadernoError

ROOT = "Document"  # the device's single top-level folder
GLOB_CHARS = frozenset("*?[")
HTTPS_PORT = 8443  # Web API (TLS, self-signed certificate)
REG_PORT = 8080    # pairing ("registration"), plain HTTP
_WIN_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})

JsonObject = dict[str, Any]  # a decoded JSON object from the device: not validated


def json_str(obj: JsonObject, key: str) -> str:
    """obj[key], which the device must have sent as a string."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise ProtocolError(f"the device's answer has no text {key!r}: {str(obj)[:200]}")
    return value


def json_int(obj: JsonObject, key: str) -> int:
    """obj[key], which the device must have sent as a whole number (or digits)."""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProtocolError(f"the device's answer has no number {key!r}: {str(obj)[:200]}")
    try:
        return int(value)
    except ValueError as err:
        raise ProtocolError(f"the device's answer has no number {key!r}: {str(obj)[:200]}") from err


def norm(path: str | None) -> str:
    """Accept 'foo.pdf', '/foo.pdf' or 'Document/foo.pdf'; return 'Document/...'.
    Only slashes are trimmed: device names may begin or end with spaces."""
    p = (path or "").strip("/")
    if p == ROOT or p.startswith(ROOT + "/"):
        return p
    return f"{ROOT}/{p}" if p else ROOT


def nfc(name: str) -> str:
    """Normalize ``name`` to Unicode NFC so macOS NFD and device NFC names compare equal."""
    return unicodedata.normalize("NFC", name)


def safe_local_name(name: str) -> str:
    """Validate a single path component from the device before using it locally.

    Args:
        name: Single filename or folder name (not a multi-segment path).

    Returns:
        The validated ``name`` unchanged.

    Raises:
        QuadernoError: If ``name`` is empty, ``.``, ``..``, contains ``/``, ``\\``,
            or NUL, or violates Windows reserved/drive-letter rules on ``win32``.
    """
    if name in ("", ".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise QuadernoError(f"refusing unsafe name from device: {name!r}")
    if sys.platform == "win32":
        stem = name.rstrip(" .").split(".", 1)[0].lower()
        if ":" in name or name.endswith((" ", ".")) or stem in _WIN_RESERVED:
            raise QuadernoError(f"refusing unsafe Windows name from device: {name!r}")
    return name


def sanitize_terminal(text: str) -> str:
    """Escape C0/C1 control characters (including ANSI ESC) for safe TTY output.

    Args:
        text: Untrusted string from the device or network.

    Returns:
        Copy of ``text`` with control bytes (except ``\\n`` and ``\\t``) replaced by
        ``\\xNN`` escapes.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n" or ch == "\t":
            out.append(ch)
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def decode_ssid(value: str) -> str:
    """Decode a Base64-encoded Wi-Fi SSID from the device API to UTF-8 text.
    Returns `value` unchanged if it isn't canonical printable Base64 (e.g. plain text)."""
    if not value or len(value) % 4 != 0:
        return value
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
        if not raw or base64.b64encode(raw).decode("ascii") != value:
            return value
        text = raw.decode("utf-8")
    except (UnicodeError, binascii.Error, ValueError):
        return value
    if not all(ord(ch) >= 0x20 and ord(ch) != 0x7F and not (0x80 <= ord(ch) <= 0x9F) for ch in text):
        return value
    return text


def is_pattern(component: str, regex: bool = False) -> bool:
    """Return ``True`` if path ``component`` is a pattern (glob metacharacter or regex mode)."""
    return regex or any(c in component for c in GLOB_CHARS)


def matcher(pattern: str, regex: bool = False) -> Callable[[str], bool]:
    """Case-insensitive name matcher: shell glob, or a regex searched in the name."""
    if regex:
        try:
            rx = re.compile(nfc(pattern), re.IGNORECASE)
        except re.error as err:
            raise QuadernoError(f"bad regular expression {pattern!r}: {err}") from err
        return lambda name: rx.search(nfc(name)) is not None
    pat = nfc(pattern).casefold()
    return lambda name: fnmatch.fnmatchcase(nfc(name).casefold(), pat)


def parse_device_time(s: object) -> int | None:
    """'2024-04-08T11:31:43Z' -> epoch seconds. None (field absent) gives None;
    anything else unreadable raises ValueError, including a time without a UTC
    offset, which would otherwise be read as local time."""
    if s is None:
        return None
    if not isinstance(s, str):
        raise ValueError(f"not a time: {s!r}")
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"time without a UTC offset: {s!r}")
    return int(dt.timestamp())


def device_time(when: datetime.datetime | None = None) -> str:
    """A UTC datetime (default: now) in the device's 'YYYY-MM-DDTHH:MM:SSZ' format."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n: float) -> str:
    """Bytes as '512B', '1.5K', '2.0M', '3.1G'."""
    size = float(n)
    for unit in ("B", "K", "M"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"
