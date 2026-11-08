"""Where pairing credentials live, and loading/saving them: an explicit
directory, else CONFIG_DIR (~/.config/quaderno, or $QUADERNO_CONFIG)."""

import os
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
    return os.path.exists(os.path.join(cred_dir or CONFIG_DIR, "deviceid.dat"))


def find_credentials_dir(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if is_paired(CONFIG_DIR):
        return CONFIG_DIR
    raise NotPairedError("not paired: run `quaderno pair` with the device connected")


def load_pinned_cert(cred_dir: str) -> bytes | None:
    path = os.path.join(cred_dir, "devicecert.bin")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        der = crypto.cert_to_der(f.read())
    if der is None:
        raise CertificateError(f"{path} is not a valid certificate; run `pair --force` or delete it")
    return der


def load_credentials(cred_dir: str | None = None) -> Credentials:
    cred_dir = cred_dir or find_credentials_dir()

    def read(name: str) -> str:
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
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def save_pairing(cred_dir: str, client_id: str, private_pem: str, public_pem: str, cert: bytes,
                 serial: str | None) -> bool:
    """Store a new pairing. Returns True if the device certificate was stored for
    TLS pinning (False if the device sent something unreadable)."""
    os.makedirs(cred_dir, mode=0o700, exist_ok=True)
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
