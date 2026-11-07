"""Path, name and formatting helpers shared by the API and the CLI."""

import datetime
import fnmatch
import re
import unicodedata

from .errors import QuadernoError

ROOT = "Document"  # the device's single top-level folder
GLOB_CHARS = frozenset("*?[")
HTTPS_PORT = 8443  # Web API (TLS, self-signed certificate)
REG_PORT = 8080    # pairing ("registration"), plain HTTP


def norm(path):
    """Accept 'foo.pdf', '/foo.pdf' or 'Document/foo.pdf'; return 'Document/...'.
    Only slashes are trimmed: device names may begin or end with spaces."""
    p = (path or "").strip("/")
    if p == ROOT or p.startswith(ROOT + "/"):
        return p
    return f"{ROOT}/{p}" if p else ROOT


def nfc(name):
    return unicodedata.normalize("NFC", name)


def safe_local_name(name):
    """Device names become local path components; reject anything that could escape."""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise QuadernoError(f"refusing unsafe name from device: {name!r}")
    return name


def is_pattern(component, regex=False):
    return regex or any(c in component for c in GLOB_CHARS)


def matcher(pattern, regex=False):
    """Case-insensitive name matcher: shell glob, or a regex searched in the name."""
    if regex:
        try:
            rx = re.compile(nfc(pattern), re.IGNORECASE)
        except re.error as err:
            raise QuadernoError(f"bad regular expression {pattern!r}: {err}")
        return lambda name: rx.search(nfc(name)) is not None
    pat = nfc(pattern).casefold()
    return lambda name: fnmatch.fnmatchcase(nfc(name).casefold(), pat)


def parse_device_time(s):
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


def device_time(when=None):
    """A UTC datetime (default: now) in the device's 'YYYY-MM-DDTHH:MM:SSZ' format."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
