"""Where pairing credentials live, and loading/saving them: an explicit
directory, else CONFIG_DIR (~/.config/quaderno, or $QUADERNO_CONFIG)."""

import ipaddress
import os
import tempfile
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from . import crypto
from .errors import CertificateError, NotPairedError

CONFIG_DIR = os.path.expanduser(os.environ.get("QUADERNO_CONFIG", "~/.config/quaderno"))


@dataclass
class Credentials:
    client_id: str
    key: RSAPrivateKey
    serial: str | None             # device serial, used to compute its address
    pinned_cert: bytes | None      # DER of the device's TLS certificate, from pairing
    source: str                    # directory they were read from


def is_paired(cred_dir: str | None = None) -> bool:
    """Return ``True`` if ``deviceid.dat`` exists in ``cred_dir`` (or ``CONFIG_DIR``)."""
    return os.path.exists(os.path.join(cred_dir or CONFIG_DIR, "deviceid.dat"))


def find_credentials_dir(explicit: str | None = None) -> str:
    """Locate the credentials directory: ``explicit`` if given, else ``CONFIG_DIR``.

    Raises:
        NotPairedError: If ``explicit`` is not given and ``CONFIG_DIR`` is unpaired.
    """
    if explicit:
        return explicit
    if is_paired(CONFIG_DIR):
        return CONFIG_DIR
    raise NotPairedError("not paired: run `quaderno pair` with the device connected")


def load_pinned_cert(cred_dir: str) -> bytes | None:
    """Load and validate ``devicecert.bin`` from ``cred_dir`` as X.509 DER bytes.

    Returns:
        DER bytes, or ``None`` if ``devicecert.bin`` does not exist.

    Raises:
        CertificateError: If ``devicecert.bin`` exists but is not a valid X.509 cert.
    """
    path = os.path.join(cred_dir, "devicecert.bin")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        der = crypto.cert_to_der(f.read())
    if der is None:
        raise CertificateError(f"{path} is not a valid certificate; run `pair --force` or delete it")
    return der


def load_credentials(cred_dir: str | None = None) -> Credentials:
    """Load client ID, RSA private key, optional serial, and pinned cert from ``cred_dir``.

    Args:
        cred_dir: Credentials directory path, or ``None`` to use ``find_credentials_dir()``.

    Returns:
        Populated ``Credentials`` dataclass.

    Raises:
        NotPairedError: If required pairing files are missing.
        AuthError: If ``privatekey.dat`` is unreadable or not an RSA key.
        CertificateError: If ``devicecert.bin`` is corrupt.
    """
    cred_dir = cred_dir or find_credentials_dir()

    def read(name: str) -> str:
        """Read and strip a required text file inside ``cred_dir``."""
        p = os.path.join(cred_dir, name)
        if not os.path.exists(p):
            raise NotPairedError(f"incomplete pairing: {p} is missing; run `quaderno pair --force`")
        with open(p) as f:
            return f.read().strip()
    has_serial = os.path.exists(os.path.join(cred_dir, "serial.dat"))
    serial = (read("serial.dat") or None) if has_serial else None
    return Credentials(client_id=read("deviceid.dat"),
                       key=crypto.load_private_key(read("privatekey.dat")),
                       serial=serial, pinned_cert=load_pinned_cert(cred_dir), source=cred_dir)


def write_private(path: str, data: str | bytes, mode: int = 0o600) -> None:
    """Write atomically, creating the file with `mode` (never world-readable first)."""
    parent = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


WIFI_ADDR_FILE = "wifi_addr.dat"


def is_wifi_addr(addr: str) -> bool:
    """True for a routable/LAN IPv4 or non-link-local IPv6 address suitable for Wi-Fi."""
    bare = addr.strip("[]")
    if "%" in bare:
        return False
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_link_local:
        return False
    return True


def load_wifi_addr(cred_dir: str) -> str | None:
    """Return the last Wi-Fi address cached in `cred_dir`, or None."""
    path = os.path.join(cred_dir, WIFI_ADDR_FILE)
    try:
        with open(path) as f:
            addr = f.read().strip()
    except OSError:
        return None
    return addr if is_wifi_addr(addr) else None


def save_wifi_addr(cred_dir: str, addr: str) -> None:
    """Cache `addr` in `cred_dir` for fast Wi-Fi reconnects (best-effort)."""
    if not is_wifi_addr(addr) or not os.path.isdir(cred_dir):
        return
    try:
        write_private(os.path.join(cred_dir, WIFI_ADDR_FILE), addr.strip("[]") + "\n", 0o644)
    except OSError:
        pass


def clear_wifi_addr(cred_dir: str) -> None:
    """Remove any cached Wi-Fi address in `cred_dir` (best-effort)."""
    try:
        os.remove(os.path.join(cred_dir, WIFI_ADDR_FILE))
    except OSError:
        pass


def save_pairing(cred_dir: str, client_id: str, private_pem: str, public_pem: str, cert: bytes,
                 serial: str | None) -> bool:
    """Store a new pairing. Returns True if the device certificate was stored for
    TLS pinning (False if the device sent something unreadable)."""
    os.makedirs(cred_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(cred_dir, 0o700)
    except OSError:
        pass
    write_private(os.path.join(cred_dir, "privatekey.dat"), private_pem)
    write_private(os.path.join(cred_dir, "publickey.dat"), public_pem, 0o644)
    cert_path = os.path.join(cred_dir, "devicecert.bin")
    pinned = crypto.cert_to_der(cert) is not None
    if pinned:
        write_private(cert_path, cert, 0o644)
    elif os.path.exists(cert_path):
        os.remove(cert_path)  # never keep a pin from an earlier pairing
    if serial:
        write_private(os.path.join(cred_dir, "serial.dat"), serial + "\n", 0o644)
    write_private(os.path.join(cred_dir, "deviceid.dat"), client_id + "\n", 0o644)
    return pinned
