"""Where pairing credentials live, and loading/saving them.

Lookup order: an explicit directory, else our own pairing in CONFIG_DIR
(~/.config/quaderno, or $QUADERNO_CONFIG), else the QUADERNO PC App's pairing,
which is only ever read."""

import glob
import os
from dataclasses import dataclass

from . import crypto
from .errors import QuadernoError

APP_DIR = os.path.expanduser(
    "~/Library/Application Support/Fujitsu/QUADERNO PC App/QUADERNO_PCApp")
CONFIG_DIR = os.path.expanduser(os.environ.get("QUADERNO_CONFIG", "~/.config/quaderno"))


@dataclass
class Credentials:
    client_id: str
    key: object                    # cryptography RSAPrivateKey
    serial: str | None             # device serial, used to compute its address
    pinned_cert: bytes | None      # DER of the device's TLS certificate, from pairing
    source: str                    # directory they were read from


def find_app_workspace():
    last = os.path.join(APP_DIR, "lastworkspaceid.dat")
    if os.path.exists(last):
        with open(last) as f:
            path = os.path.join(APP_DIR, f.read().strip())
        if os.path.isdir(path):
            return path
    cands = sorted(glob.glob(os.path.join(APP_DIR, "ws*")), key=os.path.getmtime)
    return cands[-1] if cands else None


def is_paired(cred_dir=None):
    return os.path.exists(os.path.join(cred_dir or CONFIG_DIR, "deviceid.dat"))


def find_credentials_dir(explicit=None):
    if explicit:
        return explicit
    if is_paired(CONFIG_DIR):
        return CONFIG_DIR
    ws = find_app_workspace()
    if ws:
        return ws
    raise QuadernoError("not paired: run `quaderno pair` with the device connected")


def load_pinned_cert(cred_dir):
    path = os.path.join(cred_dir, "devicecert.bin")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        der = crypto.cert_to_der(f.read())
    if der is None:
        raise QuadernoError(f"{path} is not a valid certificate; run `pair --force` or delete it")
    return der


def load_credentials(cred_dir=None):
    cred_dir = cred_dir or find_credentials_dir()

    def read(name):
        p = os.path.join(cred_dir, name)
        if not os.path.exists(p):
            raise QuadernoError("missing " + p)
        with open(p) as f:
            return f.read().strip()
    serial = None
    for name in ("serial.dat", "workspace.dat"):  # ours, the PC App's
        if os.path.exists(os.path.join(cred_dir, name)):
            serial = read(name) or None
            break
    return Credentials(client_id=read("deviceid.dat"),
                       key=crypto.load_private_key(read("privatekey.dat")),
                       serial=serial, pinned_cert=load_pinned_cert(cred_dir), source=cred_dir)


def write_private(path, data, mode=0o600):
    """Write atomically, creating the file with `mode` (never world-readable first)."""
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def save_pairing(cred_dir, client_id, private_pem, public_pem, cert, serial):
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
