"""PIN pairing ("registration") with the device.

Plain HTTP on port 8080: /register/pin, /register/hash, /register/ca, /register,
/register/cleanup. A Diffie-Hellman exchange (RFC 3526 group 14) feeds PBKDF2-SHA256,
giving an HMAC-SHA256 key and an AES-128 key-wrap key. The PIN shown on the device
enters as HMAC(key, PIN), so both sides prove they know it without sending it.
The client sends its ID and public key wrapped; the device answers with its TLS
certificate, which is kept for pinning."""

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import uuid
from dataclasses import dataclass

from . import credentials, crypto, usb
from .client import Client
from .errors import DeviceUnreachableError, PairingError, ProtocolError, WrongPinError
from .events import emit
from .util import REG_PORT


def reg_request(addr, method, path, body=None):
    """One call to the registration API; returns (status, decoded JSON or raw text)."""
    conn = http.client.HTTPConnection(addr.strip("[]"), REG_PORT, timeout=150)
    hdrs = {"Host": f"digitalpaper.local:{REG_PORT}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
    except OSError as err:
        raise DeviceUnreachableError(f"pairing: lost contact with the device ({err})") from err
    finally:
        conn.close()
    try:
        return resp.status, (json.loads(raw) if raw else None)
    except ValueError:
        return resp.status, raw[:200]


def _b64(b):
    return base64.b64encode(b).decode()


def register(addr, client_id, public_pem, read_pin):
    """Run the PIN exchange; returns the certificate the device hands over.
    `read_pin()` is called once the device shows the PIN."""
    H = crypto.hmac_sha256
    u = base64.b64decode

    def call(path, body, want):
        status, res = reg_request(addr, "POST", path, body)
        if status != want:
            code = res.get("error_code") if isinstance(res, dict) else None
            if path == "/register/ca" and status == 403 and code == "40301":
                raise WrongPinError("pairing failed: wrong PIN")
            raise PairingError(f"pairing: POST {path} -> HTTP {status}: {res}")
        if want != 204 and not isinstance(res, dict):  # 204: no body expected
            raise ProtocolError(f"pairing: POST {path}: the device's answer isn't JSON: {res!r}")
        return res

    m1 = call("/register/pin", None, 200)
    n1, mac, yb = u(m1["a"]), u(m1["b"]), u(m1["c"])  # yb as sent (may carry a 0x00 sign byte)
    ya, zz = crypto.dh_exchange(int.from_bytes(yb, "big"))
    n2 = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", zz, n1 + mac + n2, 10000, 48)
    auth_key, kw_key = dk[:32], dk[32:]

    m2hmac = H(auth_key, n1 + mac + yb + n1 + n2 + mac + ya)
    m3 = call("/register/hash", {"a": _b64(n1), "b": _b64(n2), "c": _b64(mac),
                                 "d": _b64(ya), "e": _b64(m2hmac)}, 200)
    if u(m3["a"]) != n2:
        raise PairingError("pairing: nonce mismatch")
    e_hash, m3hmac = u(m3["b"]), u(m3["e"])
    if not hmac.compare_digest(m3hmac, H(auth_key, n1 + n2 + mac + ya + m2hmac + n2 + e_hash)):
        raise PairingError("pairing: device message failed HMAC check")

    psk = H(auth_key, read_pin().strip().encode())
    rs = secrets.token_bytes(16)
    r_hash = H(auth_key, rs + psk + yb + ya)
    wrapped_rs = crypto.wrap(rs, auth_key, kw_key)
    m4hmac = H(auth_key, n2 + e_hash + m3hmac + n1 + r_hash + wrapped_rs)
    m5 = call("/register/ca", {"a": _b64(n1), "b": _b64(r_hash), "d": _b64(wrapped_rs),
                               "e": _b64(m4hmac)}, 200)
    if u(m5["a"]) != n2:
        raise PairingError("pairing: nonce mismatch")
    wrapped_es_cert, m5hmac = u(m5["d"]), u(m5["e"])
    if not hmac.compare_digest(m5hmac, H(auth_key, n1 + r_hash + wrapped_rs + m4hmac + n2
                                          + wrapped_es_cert)):
        raise PairingError("pairing: device message failed HMAC check")
    es_cert = crypto.unwrap(wrapped_es_cert, auth_key, kw_key)
    es, cert = es_cert[:16], es_cert[16:]
    # The device proves it knows the same PIN; guards against a spoofed device.
    if not hmac.compare_digest(e_hash, H(auth_key, es + psk + yb + ya)):
        raise PairingError("pairing: device failed the PIN proof")

    wrapped_id_key = crypto.wrap(client_id.encode() + public_pem.encode(), auth_key, kw_key)
    m6hmac = H(auth_key, n2 + wrapped_es_cert + m5hmac + n1 + wrapped_id_key)
    call("/register", {"a": _b64(n1), "d": _b64(wrapped_id_key), "e": _b64(m6hmac)}, 204)
    return cert


@dataclass
class PairResult:
    client_id: str
    serial: str | None
    addr: str
    cred_dir: str
    pinned: bool


def pair(read_pin, addr=None, force=False, cred_dir=None, on_event=None):
    """Pair with the device (it shows a PIN; `read_pin()` must return it) and store
    new credentials in `cred_dir` (default ~/.config/quaderno). Refuses to replace
    an existing pairing unless `force`."""
    cred_dir = cred_dir or credentials.CONFIG_DIR
    if credentials.is_paired(cred_dir) and not force:
        raise PairingError(f"already paired (credentials in {cred_dir}); "
                            "use --force to pair again and replace them")
    addr = addr or os.environ.get("QUADERNO_ADDR") or usb.discover()
    status, info = reg_request(addr, "GET", "/register/information")
    serial = info.get("serial_number") if status == 200 and isinstance(info, dict) else None
    if not serial:
        serial = next((d.serial for d in usb.usb_devices() if d.serial), None)
    emit(on_event, "device", f"device {serial or '?'} at {addr}")
    emit(on_event, "keygen", "generating 2048-bit RSA key ...")
    private_pem, public_pem = crypto.generate_keypair()
    client_id = str(uuid.uuid4())
    try:
        reg_request(addr, "PUT", "/register/cleanup")
        cert = register(addr, client_id, public_pem, read_pin)
    finally:
        try:
            reg_request(addr, "PUT", "/register/cleanup")
        except OSError:
            pass
    pinned = credentials.save_pairing(cred_dir, client_id, private_pem, public_pem, cert, serial)
    if not pinned:
        emit(on_event, "warning", "warning: device sent an unreadable certificate; "
                                  "TLS will not be pinned", warning=True)
    emit(on_event, "paired", f"paired: client {client_id}, credentials in {cred_dir}")
    with Client(addr, client_id, crypto.load_private_key(private_pem),
                pinned_cert=credentials.load_pinned_cert(cred_dir)) as c:
        c.authenticate()
    emit(on_event, "authenticated", "authentication with the new key: OK")
    return PairResult(client_id, serial, addr, cred_dir, pinned)
