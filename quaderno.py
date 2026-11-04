#!/usr/bin/env python3
"""quaderno: command-line client for a Fujitsu QUADERNO (Sony DPT) over USB.

Standard library only. Pairs with the device itself (`quaderno.py pair`, PIN
shown on the device) and keeps its own client ID and RSA key in
~/.config/quaderno. Falls back to the QUADERNO PC App's pairing (read-only)
when it has none of its own.

Protocol:
  * USB: the device first enumerates as CDC-ACM serial (VID 0x04C5 PID 0x1656).
    Writing 01 00 00 01 00 00 00 01 01 04 to the tty switches it to CDC-ECM
    Ethernet (PID 0x1657). The PC App does this automatically while running.
  * Address: IPv6 link-local derived from the device serial number, HTTPS on
    port 8443 (self-signed certificate).
  * Auth: GET /auth/nonce/{client_id} -> RSA-SHA256 (PKCS#1 v1.5) signature of
    the nonce -> PUT /auth {client_id, nonce_signed}. Session is a cookie.
  * Pairing (HTTP, port 8080): /register/pin, /register/hash, /register/ca,
    /register, /register/cleanup. DH (RFC 3526 2048-bit group) + PBKDF2-SHA256
    -> HMAC-SHA256 auth key and AES-128-CBC key-wrap key; the PIN shown on the
    device is mixed in as HMAC(auth_key, pin). The client sends its ID and RSA
    public key wrapped under that key.
"""

import argparse
import base64
import calendar
import datetime
import fnmatch
import glob
import hashlib
import http.client
import hmac
import json
import math
import os
import queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from urllib.parse import quote

APP_DIR = os.path.expanduser(
    "~/Library/Application Support/Fujitsu/QUADERNO PC App/QUADERNO_PCApp")
CONFIG_DIR = os.path.expanduser(os.environ.get("QUADERNO_CONFIG", "~/.config/quaderno"))
REG_PORT = 8080
VID = 0x04C5
PID_SERIAL = 0x1656
PID_ETHERNET = 0x1657
SWITCH_TO_CDC_ECM = bytes([0x01, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x01, 0x04])
HTTPS_PORT = 8443
CHUNK = 8 * 1024 * 1024
ROOT = "Document"


class QuadernoError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------
# Credentials (pure-Python RSA PKCS#1 v1.5 signing; no third-party packages)
# --------------------------------------------------------------------------

def _der_read(buf, pos):
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    return tag, buf[pos:pos + length], pos + length


def load_rsa_private_key(pem_text):
    m = re.search(r"-----BEGIN RSA PRIVATE KEY-----(.*?)-----END RSA PRIVATE KEY-----",
                  pem_text, re.S)
    if not m:
        raise QuadernoError("privatekey.dat is not a PKCS#1 RSA PEM key")
    der = base64.b64decode("".join(m.group(1).split()))
    tag, body, _ = _der_read(der, 0)
    if tag != 0x30:
        raise QuadernoError("unexpected key encoding")
    ints, pos = [], 0
    while pos < len(body):
        tag, val, pos = _der_read(body, pos)
        if tag != 0x02:
            raise QuadernoError("unexpected key encoding")
        ints.append(int.from_bytes(val, "big"))
    # version, n, e, d, p, q, dp, dq, qinv
    return {"n": ints[1], "d": ints[3], "p": ints[4], "q": ints[5],
            "dp": ints[6], "dq": ints[7], "qinv": ints[8]}


_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_sign_sha256(key, message):
    k = (key["n"].bit_length() + 7) // 8
    t = _SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    # CRT
    s1 = pow(m, key["dp"], key["p"])
    s2 = pow(m, key["dq"], key["q"])
    h = (key["qinv"] * (s1 - s2)) % key["p"]
    s = s2 + h * key["q"]
    return s.to_bytes(k, "big")


# --- RSA key generation and DER/PEM encoding --------------------------------

def _der_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der(tag, body):
    return bytes([tag]) + _der_len(len(body)) + body


def _der_int(x):
    return _der(0x02, x.to_bytes(x.bit_length() // 8 + 1, "big"))


def _pem(label, der):
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % d for d in range(2, int(p ** 0.5) + 1))]


def _is_probable_prime(n, rounds=40):
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits, e):
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1  # top 2 bits set: n has full length
        if math.gcd(c - 1, e) == 1 and _is_probable_prime(c):
            return c


def generate_rsa_key(bits=2048, e=65537):
    """Return (private_pem, public_pem) in these formats: PKCS#1
    'RSA PRIVATE KEY', and SubjectPublicKeyInfo DER under an 'RSA PUBLIC KEY' label."""
    while True:
        p, q = _random_prime(bits // 2, e), _random_prime(bits // 2, e)
        if p != q and (p * q).bit_length() == bits:
            break
    n, phi = p * q, (p - 1) * (q - 1)
    d = pow(e, -1, phi)
    priv = _der(0x30, b"".join(_der_int(v) for v in
                               (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))))
    rsa_pub = _der(0x30, _der_int(n) + _der_int(e))
    alg = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    spki = _der(0x30, alg + _der(0x03, b"\x00" + rsa_pub))
    return _pem("RSA PRIVATE KEY", priv), _pem("RSA PUBLIC KEY", spki)


# --- AES-128-CBC (pure Python; only used for the few small pairing messages) --

def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _build_aes_tables():
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ _xtime(p)                       # p *= 3
        q ^= (q << 1) & 0xFF                    # q /= 3
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        rot = lambda x, s: ((x << s) | (x >> (8 - s))) & 0xFF
        sbox[p] = q ^ rot(q, 1) ^ rot(q, 2) ^ rot(q, 3) ^ rot(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i

    def mul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a, b = _xtime(a), b >> 1
        return r
    tables = {k: [mul(i, k) for i in range(256)] for k in (2, 3, 9, 11, 13, 14)}
    return sbox, inv, tables


_SBOX, _INV_SBOX, _MUL = _build_aes_tables()
_SHIFT = [((i // 4 + i % 4) % 4) * 4 + i % 4 for i in range(16)]
_INV_SHIFT = [((i // 4 - i % 4) % 4) * 4 + i % 4 for i in range(16)]


def _aes_round_keys(key):
    assert len(key) == 16
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _aes_encrypt_block(rk, block):
    m2, m3 = _MUL[2], _MUL[3]
    s = [b ^ k for b, k in zip(block, rk[0])]
    for r in range(1, 11):
        s = [_SBOX[s[j]] for j in _SHIFT]
        if r != 10:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                t += [m2[a0] ^ m3[a1] ^ a2 ^ a3, a0 ^ m2[a1] ^ m3[a2] ^ a3,
                      a0 ^ a1 ^ m2[a2] ^ m3[a3], m3[a0] ^ a1 ^ a2 ^ m2[a3]]
            s = t
        s = [b ^ k for b, k in zip(s, rk[r])]
    return bytes(s)


def _aes_decrypt_block(rk, block):
    m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    s = [b ^ k for b, k in zip(block, rk[10])]
    for r in range(9, -1, -1):
        s = [_INV_SBOX[s[j]] ^ k for j, k in zip(_INV_SHIFT, rk[r])]
        if r != 0:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                t += [m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3], m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
                      m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3], m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]]
            s = t
    return bytes(s)


def aes128_cbc_encrypt(key, iv, data):
    rk = _aes_round_keys(key)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        prev = _aes_encrypt_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return out


def aes128_cbc_decrypt(key, iv, data):
    if len(data) % 16 or not data:
        raise QuadernoError("bad ciphertext length")
    rk = _aes_round_keys(key)
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_aes_decrypt_block(rk, block), prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise QuadernoError("bad padding")
    return out[:-pad]


# --- Credential storage ------------------------------------------------------

def find_app_workspace():
    last = os.path.join(APP_DIR, "lastworkspaceid.dat")
    if os.path.exists(last):
        path = os.path.join(APP_DIR, open(last).read().strip())
        if os.path.isdir(path):
            return path
    cands = sorted(glob.glob(os.path.join(APP_DIR, "ws*")), key=os.path.getmtime)
    return cands[-1] if cands else None


def find_credentials(explicit=None):
    """--workspace DIR, else our own pairing in CONFIG_DIR, else the PC App's."""
    if explicit:
        return explicit
    if os.path.exists(os.path.join(CONFIG_DIR, "deviceid.dat")):
        return CONFIG_DIR
    ws = find_app_workspace()
    if ws:
        return ws
    raise QuadernoError("not paired: run `quaderno.py pair` with the device connected")


def load_credentials(cred_dir):
    def rd(name):
        p = os.path.join(cred_dir, name)
        if not os.path.exists(p):
            raise QuadernoError("missing " + p)
        return open(p).read().strip()
    client_id = rd("deviceid.dat")
    key = load_rsa_private_key(rd("privatekey.dat"))
    serial = None
    for name in ("serial.dat", "workspace.dat"):  # ours, PC App's
        if os.path.exists(os.path.join(cred_dir, name)):
            serial = rd(name) or None
            break
    return client_id, key, serial


def _write_private(path, data, mode=0o600):
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


# --------------------------------------------------------------------------
# USB discovery
# --------------------------------------------------------------------------

def ipv6_from_serial(serial):
    """The device's IPv6 link-local address, computed from its serial number."""
    v = (int("1" + str(serial)) * 0x9E37FFFFFFFC0001) & 0xFCFFFFFFFFFFFFFF
    h = format(v >> 16, "x").zfill(12)
    h = h[:6] + "fffe" + h[6:12]
    return "fe80::" + ":".join(h[i:i + 4] for i in range(0, 16, 4))


def usb_devices():
    """Return [(pid, serial, callout_tty_or_None)] for attached QUADERNO devices."""
    try:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    devs = []
    for block in re.split(r"\n(?=\+-o )", out):
        vid = re.search(r'"idVendor" = (\d+)', block)
        if not vid or int(vid.group(1)) != VID:
            continue
        pid = int(re.search(r'"idProduct" = (\d+)', block).group(1))
        ser = re.search(r'"USB Serial Number" = "([^"]+)"', block)
        tty = re.search(r'"IOCalloutDevice" = "([^"]+)"', block)
        devs.append((pid, ser.group(1) if ser else None, tty.group(1) if tty else None))
    return devs


def switch_to_ethernet(tty):
    import termios
    fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = attrs[5] = termios.B9600
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, SWITCH_TO_CDC_ECM)
        termios.tcdrain(fd)
    finally:
        os.close(fd)


def _probe(addr, timeout=1.5):
    try:
        with socket.create_connection((addr, HTTPS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _first_reachable(cands, timeout=1.5):
    """Probe all candidates at once; return the first that accepts a connection.
    Daemon threads, so probes still waiting on other interfaces never delay exit
    (a thread pool would make the interpreter wait up to `timeout` at shutdown)."""
    results = queue.Queue()
    for a in cands:
        threading.Thread(target=lambda a=a: results.put((a, _probe(a, timeout))),
                         daemon=True).start()
    for _ in cands:
        try:
            a, ok = results.get(timeout=timeout + 1)
        except queue.Empty:
            return None
        if ok:
            return a
    return None


def discover(serial_hint=None, verbose=False):
    """Find the device's scoped IPv6 address, switching USB mode if needed."""
    devs = usb_devices()
    serials = [s for _, s, _ in devs if s] or ([serial_hint] if serial_hint else [])
    serial_devs = [d for d in devs if d[0] == PID_SERIAL and d[2]]
    if serial_devs:
        for _, ser, tty in serial_devs:
            if verbose:
                print(f"switching {tty} ({ser}) to USB Ethernet", file=sys.stderr)
            switch_to_ethernet(tty)
        deadline = time.time() + 20
    else:
        deadline = time.time() + 3
    if not serials:
        raise QuadernoError("no QUADERNO found on USB (and no serial number to derive an address)")
    ifaces = [n for _, n in socket.if_nameindex() if n.startswith("en")]
    while True:
        cands = [f"{ipv6_from_serial(s)}%{i}" for s in serials for i in ifaces]
        found = _first_reachable(cands)
        if found:
            return found
        if time.time() > deadline:
            break
        time.sleep(1)
        ifaces = [n for _, n in socket.if_nameindex() if n.startswith("en")]
    raise QuadernoError("QUADERNO is on USB but did not answer on port 8443 "
                        "(unlock/wake the device, or replug it)")


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a self-signed device: accept exactly the certificate obtained at pairing."""

    def __init__(self, *args, pin=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pin = pin

    def connect(self):
        super().connect()
        if self._pin is not None and self.sock.getpeercert(binary_form=True) != self._pin:
            self.sock.close()
            raise QuadernoError("device TLS certificate differs from the one received at pairing "
                                "(devicecert.bin); if the firmware was updated, run `pair --force`")


def cert_to_der(data):
    """PEM or DER bytes -> DER bytes, or None if it is not a readable certificate."""
    try:
        if data.startswith(b"-----BEGIN"):
            return ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
        return data if data[:1] == b"\x30" else None
    except (ValueError, UnicodeDecodeError):
        return None


def load_pinned_cert(cred_dir):
    path = os.path.join(cred_dir, "devicecert.bin")
    if not os.path.exists(path):
        return None
    der = cert_to_der(open(path, "rb").read())
    if der is None:
        raise QuadernoError(f"{path} is not a valid certificate; run `pair --force` or delete it")
    return der


class Quaderno:
    def __init__(self, addr, client_id, key, timeout=60, pinned_cert=None):
        self.addr = addr.strip("[]")
        self.client_id = client_id
        self.key = key
        self.timeout = timeout
        self.pinned_cert = pinned_cert
        self.cookies = {}
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # device uses a self-signed certificate
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        self.ctx = ctx

    def _conn(self):
        return _PinnedHTTPSConnection(self.addr, HTTPS_PORT, context=self.ctx,
                                      timeout=self.timeout, pin=self.pinned_cert)

    def request(self, method, path, body=None, headers=None, ok=(200, 204), raw=False,
                stream_to=None):
        hdrs = {"Host": "digitalpaper.local:8443", "Accept": "application/json"}
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn = self._conn()
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            for h in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = h.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
            if resp.status not in ok:
                data = resp.read()
                try:
                    err = json.loads(data)
                    msg = f"{err.get('error_code')} {err.get('message')}"
                except Exception:
                    msg = data[:200].decode(errors="replace")
                raise QuadernoError(f"{method} {path} -> HTTP {resp.status}: {msg}", resp.status)
            if stream_to is not None:
                total = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    stream_to.write(chunk)
                    total += len(chunk)
                return total
            data = resp.read()
            if raw:
                return data
            return json.loads(data) if data else None
        finally:
            conn.close()

    # --- auth -------------------------------------------------------------
    def authenticate(self):
        nonce = self.request("GET", "/auth/nonce/" + quote(self.client_id))["nonce"]
        sig = rsa_sign_sha256(self.key, nonce.encode("latin-1"))
        self.request("PUT", "/auth", {"client_id": self.client_id,
                                      "nonce_signed": base64.b64encode(sig).decode()})

    # --- entries ----------------------------------------------------------
    def resolve(self, path):
        return self.request("GET", "/resolve/entry/" + quote(norm(path), safe=""))

    def entries(self, folder_id):
        out, offset = [], 0
        while True:
            r = self.request("GET", f"/folders/{folder_id}/entries?offset={offset}&limit=100")
            out += r.get("entry_list", [])
            offset = len(out)
            if offset >= int(r.get("count", offset)) or not r.get("entry_list"):
                return out

    def walk(self, folder):
        for e in self.entries(folder["entry_id"]):
            yield e
            if e["entry_type"] == "folder":
                yield from self.walk(e)

    def download(self, doc_id, fh):
        return self.request("GET", f"/documents/{doc_id}/file", stream_to=fh,
                            headers={"Accept": "*/*"})

    def upload(self, local, folder_id, name):
        doc = self.request("POST", "/documents",
                           {"parent_folder_id": folder_id, "file_name": name,
                            "document_source": ""})
        doc_id = doc["document_id"]
        try:
            return self._upload_file(local, doc_id)
        except BaseException:
            # POST created an entry; don't leave an empty/partial document behind.
            try:
                self.request("DELETE", f"/documents/{doc_id}")
            except (QuadernoError, OSError):
                print(f"warning: could not remove the incomplete upload {name}", file=sys.stderr)
            raise

    def _upload_file(self, local, doc_id):
        size = os.path.getsize(local)
        with open(local, "rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK)
                boundary = uuid.uuid4().hex
                body = (f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="file"; filename="altair.pdf"\r\n'
                        f"Content-Type: application/octet-stream\r\n\r\n").encode() + chunk + \
                    f"\r\n--{boundary}--\r\n".encode()
                r = self.request(
                    "PUT", f"/documents/{doc_id}/file?offset_bytes={offset}&total_bytes={size}",
                    body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    ok=(200, 206))
                offset = int(r["current_bytes"])
                if offset >= size:
                    if r.get("completed") != "yes":
                        raise QuadernoError("upload finished but device did not mark it complete")
                    return doc_id
                if not chunk:
                    raise QuadernoError("upload stalled")
                fh.seek(offset)

    def mkdir(self, parent_id, name):
        return self.request("POST", "/folders", {"parent_folder_id": parent_id,
                                                 "folder_name": name})["folder_id"]


def norm(path):
    """Accept 'foo.pdf', '/foo.pdf' or 'Document/foo.pdf'; return 'Document/...'."""
    p = (path or "").strip().strip("/")
    if p == ROOT or p.startswith(ROOT + "/"):
        return p
    return f"{ROOT}/{p}" if p else ROOT


def nfc(name):
    return unicodedata.normalize("NFC", name)


def try_resolve(q, path):
    try:
        return q.resolve(path)
    except QuadernoError as err:
        if err.status == 404:
            return None
        raise


def ensure_folder(q, path):
    """mkdir -p: create missing folders along `path`; return the folder's id."""
    path = norm(path)
    e = try_resolve(q, path)
    if e is not None:
        if e["entry_type"] != "folder":
            raise QuadernoError(f"{path} exists and is not a folder")
        return e["entry_id"]
    if path == ROOT:
        raise QuadernoError("root folder not found")
    parent, name = path.rsplit("/", 1)
    folder_id = q.mkdir(ensure_folder(q, parent), name)
    print(f"created {path}/")
    return folder_id


_GLOB_CHARS = frozenset("*?[")


def _is_pattern(component, regex):
    return regex or any(c in component for c in _GLOB_CHARS)


def _matcher(pattern, regex):
    """Case-insensitive name matcher: shell glob, or (with -E) a regex searched in the name."""
    if regex:
        try:
            rx = re.compile(nfc(pattern), re.IGNORECASE)
        except re.error as err:
            raise QuadernoError(f"bad regular expression {pattern!r}: {err}")
        return lambda name: rx.search(nfc(name)) is not None
    pat = nfc(pattern).casefold()
    return lambda name: fnmatch.fnmatchcase(nfc(name).casefold(), pat)


def expand(q, pattern, regex=False):
    """Resolve a remote path whose components may be glob patterns (or regexes with
    -E). An exact name always wins over a pattern, so names containing * ? [ still
    work literally. Returns (entries sorted by path, matched_by_pattern)."""
    path = norm(pattern)
    exact = try_resolve(q, path)
    if exact is not None:
        return [exact], False
    parts = path.split("/")[1:]
    if not any(_is_pattern(p, regex) for p in parts):
        q.resolve(path)  # not found and not a pattern: raise the device's 404
    current = [q.resolve(ROOT)]
    for i, part in enumerate(parts):
        found = []
        for folder in current:
            literal = try_resolve(q, folder["entry_path"].rstrip("/") + "/" + part)
            if literal is not None:
                found.append(literal)
            elif _is_pattern(part, regex):
                match = _matcher(part, regex)
                found += [c for c in q.entries(folder["entry_id"]) if match(c["entry_name"])]
        last = i == len(parts) - 1
        current = found if last else [x for x in found if x["entry_type"] == "folder"]
        if not current:
            raise QuadernoError(f"no match for {pattern!r}", 404)
    return sorted(current, key=lambda x: x["entry_path"].lower()), True


def expand_all(q, patterns, regex=False):
    """expand() several arguments; de-duplicated. Returns (entries, any_pattern)."""
    seen, out, any_pattern = set(), [], False
    for p in patterns:
        entries, by_pattern = expand(q, p, regex)
        any_pattern |= by_pattern
        for e in entries:
            if e["entry_id"] not in seen:
                seen.add(e["entry_id"])
                out.append(e)
    return out, any_pattern


def safe_local_name(name):
    """Device names become local path components; reject anything that could escape."""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise QuadernoError(f"refusing unsafe name from device: {name!r}")
    return name


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def human(n):
    n = float(n)
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def connect(args):
    cred_dir = find_credentials(args.workspace)
    client_id, key, serial = load_credentials(cred_dir)
    addr = args.addr or os.environ.get("QUADERNO_ADDR") or discover(serial, args.verbose)
    pin = load_pinned_cert(cred_dir)
    if args.verbose:
        print(f"credentials: {cred_dir}\ndevice: {addr}\n"
              f"tls: {'pinned to devicecert.bin' if pin else 'unverified (no pinned cert)'}",
              file=sys.stderr)
    q = Quaderno(addr, client_id, key, pinned_cert=pin)
    q.authenticate()
    return q


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------

_DH_P = int(  # RFC 3526 group 14 (2048-bit MODP), generator 2
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def _wrap(data, auth_key, kw_key):
    iv = secrets.token_bytes(16)
    return aes128_cbc_encrypt(kw_key, iv, data + _hmac(auth_key, data)[:8]) + iv


def _unwrap(data, auth_key, kw_key):
    plain = aes128_cbc_decrypt(kw_key, data[-16:], data[:-16])
    body, kwa = plain[:-8], plain[-8:]
    if not hmac.compare_digest(kwa, _hmac(auth_key, body)[:8]):
        raise QuadernoError("pairing: wrapped data failed its integrity check")
    return body


def _b64(b):
    return base64.b64encode(b).decode()


def _reg_request(addr, method, path, body=None):
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
    finally:
        conn.close()
    try:
        return resp.status, (json.loads(raw) if raw else None)
    except ValueError:
        return resp.status, raw[:200]


def register(addr, client_id, public_pem, read_pin):
    """Run the PIN pairing; returns the certificate the device hands over."""
    def call(path, body, want):
        status, res = _reg_request(addr, "POST", path, body)
        if status != want:
            code = res.get("error_code") if isinstance(res, dict) else None
            if path == "/register/ca" and status == 403 and code == "40301":
                raise QuadernoError("pairing failed: wrong PIN")
            raise QuadernoError(f"pairing: POST {path} -> HTTP {status}: {res}")
        return res

    u = base64.b64decode
    m1 = call("/register/pin", None, 200)
    n1, mac, yb = u(m1["a"]), u(m1["b"]), u(m1["c"])  # yb kept as sent (may carry a 0x00 sign byte)
    yb_int = int.from_bytes(yb, "big")
    if not 1 < yb_int < _DH_P - 1:
        raise QuadernoError("pairing: device sent an invalid DH value")
    # Pick a private value whose public value and shared secret are both a full
    # 256 bytes, so byte encodings cannot differ between padding conventions.
    while True:
        x = secrets.randbelow(_DH_P - 3) + 2
        ya_int, zz_int = pow(2, x, _DH_P), pow(yb_int, x, _DH_P)
        if ya_int.bit_length() > 2040 and zz_int.bit_length() > 2040:
            break
    ya = b"\x00" + ya_int.to_bytes(256, "big")
    zz = zz_int.to_bytes(256, "big")
    n2 = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", zz, n1 + mac + n2, 10000, 48)
    auth_key, kw_key = dk[:32], dk[32:]

    m2hmac = _hmac(auth_key, n1 + mac + yb + n1 + n2 + mac + ya)
    m3 = call("/register/hash", {"a": _b64(n1), "b": _b64(n2), "c": _b64(mac),
                                 "d": _b64(ya), "e": _b64(m2hmac)}, 200)
    if u(m3["a"]) != n2:
        raise QuadernoError("pairing: nonce mismatch")
    e_hash, m3hmac = u(m3["b"]), u(m3["e"])
    if not hmac.compare_digest(m3hmac, _hmac(auth_key, n1 + n2 + mac + ya + m2hmac + n2 + e_hash)):
        raise QuadernoError("pairing: device message failed HMAC check")

    pin = read_pin().strip()
    psk = _hmac(auth_key, pin.encode())
    rs = secrets.token_bytes(16)
    r_hash = _hmac(auth_key, rs + psk + yb + ya)
    wrapped_rs = _wrap(rs, auth_key, kw_key)
    m4hmac = _hmac(auth_key, n2 + e_hash + m3hmac + n1 + r_hash + wrapped_rs)
    m5 = call("/register/ca", {"a": _b64(n1), "b": _b64(r_hash), "d": _b64(wrapped_rs),
                               "e": _b64(m4hmac)}, 200)
    if u(m5["a"]) != n2:
        raise QuadernoError("pairing: nonce mismatch")
    wrapped_es_cert, m5hmac = u(m5["d"]), u(m5["e"])
    if not hmac.compare_digest(
            m5hmac, _hmac(auth_key, n1 + r_hash + wrapped_rs + m4hmac + n2 + wrapped_es_cert)):
        raise QuadernoError("pairing: device message failed HMAC check")
    es_cert = _unwrap(wrapped_es_cert, auth_key, kw_key)
    es, cert = es_cert[:16], es_cert[16:]
    # The device proves it knows the same PIN; guards against a spoofed device.
    if not hmac.compare_digest(e_hash, _hmac(auth_key, es + psk + yb + ya)):
        raise QuadernoError("pairing: device failed the PIN proof")

    wrapped_id_key = _wrap(client_id.encode() + public_pem.encode(), auth_key, kw_key)
    m6hmac = _hmac(auth_key, n2 + wrapped_es_cert + m5hmac + n1 + wrapped_id_key)
    call("/register", {"a": _b64(n1), "d": _b64(wrapped_id_key), "e": _b64(m6hmac)}, 204)
    return cert


def cmd_pair(args):
    if os.path.exists(os.path.join(CONFIG_DIR, "deviceid.dat")) and not args.force:
        raise QuadernoError(f"already paired (credentials in {CONFIG_DIR}); "
                            "use --force to pair again and replace them")
    addr = args.addr or os.environ.get("QUADERNO_ADDR") or discover(None, args.verbose)
    status, info = _reg_request(addr, "GET", "/register/information")
    serial = info.get("serial_number") if status == 200 and isinstance(info, dict) else None
    if not serial:
        serial = next((s for _, s, _ in usb_devices() if s), None)
    print(f"device {serial or '?'} at {addr}")
    print("generating 2048-bit RSA key ...", flush=True)
    private_pem, public_pem = generate_rsa_key()
    client_id = str(uuid.uuid4())

    def read_pin():
        prompt = "Enter the PIN shown on the QUADERNO: "
        if getattr(args, "pin_file", None):
            # Lets another process (or person) supply the PIN by writing this file.
            print(f"PIN is on the QUADERNO screen; waiting for it in {args.pin_file}", flush=True)
            deadline = time.time() + 150
            while time.time() < deadline:
                if os.path.exists(args.pin_file):
                    time.sleep(0.2)
                    pin = open(args.pin_file).read()
                    os.remove(args.pin_file)
                    if pin.strip():
                        return pin
                time.sleep(0.3)
            raise QuadernoError("timed out waiting for the PIN")
        if sys.stdin.isatty():
            return input(prompt)
        print(prompt, end="", flush=True)
        return sys.stdin.readline()

    try:
        _reg_request(addr, "PUT", "/register/cleanup")
        cert = register(addr, client_id, public_pem, read_pin)
    finally:
        try:
            _reg_request(addr, "PUT", "/register/cleanup")
        except OSError:
            pass

    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    _write_private(os.path.join(CONFIG_DIR, "privatekey.dat"), private_pem)
    _write_private(os.path.join(CONFIG_DIR, "publickey.dat"), public_pem, 0o644)
    cert_path = os.path.join(CONFIG_DIR, "devicecert.bin")
    if cert_to_der(cert) is not None:
        _write_private(cert_path, cert, 0o644)  # the device's TLS certificate; used for pinning
    else:
        print("warning: device sent an unreadable certificate; TLS will not be pinned",
              file=sys.stderr)
        if os.path.exists(cert_path):
            os.remove(cert_path)  # never keep a pin from an earlier pairing
    if serial:
        _write_private(os.path.join(CONFIG_DIR, "serial.dat"), serial + "\n", 0o644)
    _write_private(os.path.join(CONFIG_DIR, "deviceid.dat"), client_id + "\n", 0o644)
    print(f"paired: client {client_id}, credentials in {CONFIG_DIR}")

    q = Quaderno(addr, client_id, load_rsa_private_key(private_pem),
                 pinned_cert=load_pinned_cert(CONFIG_DIR))
    q.authenticate()
    print("authentication with the new key: OK")


def cmd_ls(q, args):
    matches, by_pattern = expand(q, args.path, args.regex)
    rows = {}
    for entry in matches:
        if entry["entry_type"] != "folder":
            rows[entry["entry_id"]] = entry
            continue
        if by_pattern:  # a matched folder is listed itself, like `ls -d`
            rows[entry["entry_id"]] = entry
            if not args.recursive:
                continue
        for e in (q.walk(entry) if args.recursive else q.entries(entry["entry_id"])):
            rows[e["entry_id"]] = e
    rows = sorted(rows.values(), key=lambda e: (e["entry_type"] != "folder", e["entry_path"].lower()))
    full_path = args.recursive or by_pattern
    for e in rows:
        name = e["entry_path"].rstrip("/") if full_path else e["entry_name"]
        if e["entry_type"] == "folder":
            name += "/"
        if args.long:
            size = human(e["file_size"]) if "file_size" in e else "-"
            date = (e.get("modified_date") or e.get("created_date") or "")[:16].replace("T", " ")
            pages = e.get("total_page", "")
            print(f"{size:>8}  {date:16}  {str(pages):>4}  {name}")
        else:
            print(name)


def fetch(q, e, target, quiet=False):
    """Download a document to `target` atomically; set mtime to the device's modified_date."""
    tmp = target + ".part"
    try:
        with open(tmp, "wb") as fh:
            n = q.download(e["entry_id"], fh)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    mtime = parse_device_time(e.get("modified_date"))
    if mtime is not None:
        os.utime(target, (mtime, mtime))
    if not quiet:
        print(f"{e['entry_path']} -> {target} ({human(n)})")
    return n


def parse_device_time(s):
    """'2024-04-08T11:31:43Z' -> epoch seconds (None if absent/unparseable)."""
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def get_tree(q, folder, dest):
    """Download `folder` as dest/<folder name>/..., keeping the subfolder layout."""
    base = os.path.join(dest, safe_local_name(folder["entry_name"]))
    prefix = folder["entry_path"].rstrip("/") + "/"
    os.makedirs(base, exist_ok=True)
    count = 0
    for e in q.walk(folder):  # parents are yielded before their children
        rel = e["entry_path"][len(prefix):] if e["entry_path"].startswith(prefix) else e["entry_name"]
        target = os.path.join(base, *[safe_local_name(p) for p in rel.split("/")])
        if e["entry_type"] == "folder":
            os.makedirs(target, exist_ok=True)
        else:
            fetch(q, e, target)
            count += 1
    print(f"{count} documents from {folder['entry_path']} -> {base}")


def cmd_get(q, args):
    dest = args.output or "."
    targets, _ = expand_all(q, args.remote, args.regex)
    if not args.recursive:
        folders = [e["entry_path"] for e in targets if e["entry_type"] == "folder"]
        if folders:
            raise QuadernoError(f"{folders[0]} is a folder (use -r)")
    if len(targets) > 1 or args.recursive:
        if os.path.exists(dest) and not os.path.isdir(dest):
            raise QuadernoError(f"{dest} is not a directory ({len(targets)} items to download)")
        os.makedirs(dest, exist_ok=True)
    for e in targets:
        if e["entry_type"] == "folder":
            get_tree(q, e, dest)
        elif os.path.isdir(dest):
            fetch(q, e, os.path.join(dest, safe_local_name(e["entry_name"])))
        else:
            fetch(q, e, dest)


def put_file(q, local, folder_id, folder_path, name, existing):
    """Upload one PDF unless a same-named entry exists. Returns True if uploaded."""
    name = nfc(name)
    if name in existing:
        print(f"skip {folder_path}/{name}: already exists", file=sys.stderr)
        return False
    q.upload(local, folder_id, name)
    existing.add(name)
    print(f"{local} -> {folder_path}/{name}")
    return True


def put_tree(q, local_dir, remote_path):
    """Mirror local_dir into remote_path (created as needed). Only PDFs are sent;
    folders are created only where there is a PDF at or below them."""
    plan, ignored = [], 0
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        visible = [f for f in files if not f.startswith(".")]
        pdfs = sorted(f for f in visible if f.lower().endswith(".pdf"))
        ignored += len(visible) - len(pdfs)
        if pdfs:
            rel = os.path.relpath(root, local_dir)
            sub_path = remote_path if rel == "." else \
                remote_path + "/" + "/".join(nfc(p) for p in rel.split(os.sep))
            plan.append((root, sub_path, pdfs))
    folder_id = ensure_folder(q, remote_path)
    uploaded = skipped = 0
    for root, sub_path, pdfs in plan:
        fid = folder_id if sub_path == remote_path else ensure_folder(q, sub_path)
        existing = {nfc(e["entry_name"]) for e in q.entries(fid)}
        for f in pdfs:
            if put_file(q, os.path.join(root, f), fid, sub_path, f, existing):
                uploaded += 1
            else:
                skipped += 1
    extra = f", {ignored} non-PDF ignored" if ignored else ""
    print(f"{uploaded} uploaded, {skipped} skipped{extra} -> {remote_path}")


def no_such_folder(q, target):
    """Error for a -d folder that doesn't exist, suggesting folders with that name."""
    name = nfc(target.rsplit("/", 1)[-1]).casefold()
    hits = [e["entry_path"].rstrip("/") for e in q.walk(q.resolve(ROOT))
            if e["entry_type"] == "folder" and nfc(e["entry_name"]).casefold() == name]
    msg = (f"there is no folder {target} on the device; create it with `quaderno mkdir`, "
           "or give a new file name ending in .pdf")
    if hits:
        msg += "\ndid you mean: " + ", ".join(repr(h[len(ROOT) + 1:]) for h in hits[:5])
    return QuadernoError(msg, 404)


def cmd_put(q, args):
    """Upload what can be uploaded; anything else gets a `skip` line on stderr.
    Exit status is 1 only if a named local file does not exist."""
    target = norm(args.remote) if args.remote else ROOT
    dest = try_resolve(q, target)
    if dest is not None and dest["entry_type"] != "folder":
        raise QuadernoError(f"{target} already exists")
    if dest is None and len(args.local) > 1:
        raise no_such_folder(q, target)
    items, missing = [], 0
    for local in args.local:
        if os.path.isdir(local):
            if args.recursive:
                items.append(local)
            else:
                print(f"skip {local}: is a directory (use -r)", file=sys.stderr)
        elif not os.path.exists(local):
            print(f"skip {local}: no such file", file=sys.stderr)
            missing += 1
        elif not local.lower().endswith(".pdf"):
            print(f"skip {local}: not a PDF", file=sys.stderr)
        else:
            items.append(local)
    if not items:
        print("nothing to upload", file=sys.stderr)
        return 1 if missing else None
    if dest is None:
        # Target does not exist: it is the new name for a single file or directory.
        local = items[0]
        if os.path.isdir(local):
            put_tree(q, local, target)
        else:
            if not target.lower().endswith(".pdf"):
                raise no_such_folder(q, target)  # almost always a mistyped/missing folder
            parent, name = target.rsplit("/", 1)
            put_file(q, local, q.resolve(parent)["entry_id"], parent, name, set())
        return 1 if missing else None
    existing = {nfc(e["entry_name"]) for e in q.entries(dest["entry_id"])}
    for local in items:
        if os.path.isdir(local):
            name = nfc(os.path.basename(os.path.normpath(local)))
            put_tree(q, local, f"{dest['entry_path']}/{name}")
        else:
            put_file(q, local, dest["entry_id"], dest["entry_path"],
                     os.path.basename(local), existing)
    return 1 if missing else None


def delete_empty_folder(q, e):
    # The device's behaviour on non-empty folders is undocumented (it may delete
    # recursively), so DELETE is only ever sent for a folder verified to be empty.
    children = q.entries(e["entry_id"])
    if children:
        raise QuadernoError(f"{e['entry_path']} is not empty ({len(children)} entries); "
                            "use -r to delete its contents first")
    q.request("DELETE", f"/folders/{e['entry_id']}")
    print(f"deleted {e['entry_path']}/")


def cmd_rm(q, args):
    plan, by_pattern = expand_all(q, args.remote, args.regex)
    plan.sort(key=lambda e: e["entry_path"])
    # Checks before anything is deleted.
    for e in plan:
        if e["entry_path"].rstrip("/") == ROOT:
            raise QuadernoError("refusing to delete the root folder")
        if e["entry_type"] == "folder" and not args.recursive and q.entries(e["entry_id"]):
            raise QuadernoError(f"{e['entry_path']} is not empty; use -r to delete its contents too")
    folders = [e["entry_path"].rstrip("/") + "/" for e in plan if e["entry_type"] == "folder"]
    plan = [e for e in plan if not any(e["entry_path"].startswith(f) for f in folders)]
    if by_pattern and not args.yes:
        print(f"pattern matches {len(plan)}:")
        for e in plan:
            print("  " + e["entry_path"].rstrip("/") + ("/" if e["entry_type"] == "folder" else ""))
        if not sys.stdin.isatty():
            raise QuadernoError("not deleting pattern matches without confirmation; add --yes")
        extra = " and everything inside" if args.recursive and folders else ""
        if input(f"delete these {len(plan)}{extra}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing deleted")
            return
    for e in plan:
        if e["entry_type"] != "folder":
            q.request("DELETE", f"/documents/{e['entry_id']}")
            print(f"deleted {e['entry_path']}")
            continue
        if e["entry_path"] == ROOT:
            raise QuadernoError("refusing to delete the root folder")
        if args.recursive:
            tree = list(q.walk(e))
            for x in tree:
                if x["entry_type"] == "document":
                    q.request("DELETE", f"/documents/{x['entry_id']}")
                    print(f"deleted {x['entry_path']}")
            folders = [x for x in tree if x["entry_type"] == "folder"]
            for x in sorted(folders, key=lambda x: x["entry_path"].count("/"), reverse=True):
                delete_empty_folder(q, x)
        delete_empty_folder(q, e)


def cmd_mkdir(q, args):
    for remote in args.remote:
        path = norm(remote)
        if args.parents:
            ensure_folder(q, path)
            continue
        if path == ROOT:
            raise QuadernoError(f"{ROOT} already exists")
        parent, name = path.rsplit("/", 1)
        q.mkdir(q.resolve(parent)["entry_id"], name)
        print(f"created {path}/")


def cmd_mv(q, args):
    sources, _ = expand_all(q, args.src, args.regex)
    dst = norm(args.dst)
    d = try_resolve(q, dst)
    if d is not None and d["entry_type"] != "folder":
        raise QuadernoError(f"{dst} already exists")
    if len(sources) > 1 and d is None:
        raise QuadernoError(f"{len(sources)} items to move: {dst} must be an existing folder")
    for e in sources:
        if e["entry_path"].rstrip("/") == ROOT:
            raise QuadernoError("refusing to move the root folder")
    for e in sources:
        if d is not None:
            parent_id, parent_path, name = d["entry_id"], d["entry_path"].rstrip("/"), e["entry_name"]
        else:
            parent_path, name = dst.rsplit("/", 1)
            parent_id = q.resolve(parent_path)["entry_id"]
        src_path = e["entry_path"].rstrip("/")
        if src_path == f"{parent_path}/{name}":
            print(f"{src_path}: already there")
            continue
        if e["entry_type"] == "folder":
            if parent_path == src_path or parent_path.startswith(src_path + "/"):
                raise QuadernoError(f"cannot move {src_path} into itself")
            kind, field = "folders", "folder_name"
        else:
            kind, field = "documents", "file_name"
        q.request("PUT", f"/{kind}/{e['entry_id']}", {"parent_folder_id": parent_id, field: name})
        new = q.request("GET", f"/{kind}/{e['entry_id']}") or {}
        print(f"{src_path} -> {new.get('entry_path', parent_path + '/' + name).rstrip('/')}")


def cmd_info(q, args):
    def get(path):
        try:
            return q.request("GET", path)
        except QuadernoError as err:
            return {"error": str(err)}
    info = {
        "api_version": get("/api_version"),
        "firmware_version": get("/system/status/firmware_version"),
        "storage": get("/system/status/storage"),
        "battery": get("/system/status/battery"),
        "owner": get("/system/configs/owner"),
        "mac_address": get("/system/status/mac_address"),
    }
    if args.json:
        print(json.dumps(info, indent=2))
        return
    def val(d, k="value"):
        return d.get(k, d.get("error", "?")) if isinstance(d, dict) else d
    st = info["storage"]
    print(f"address        {q.addr}")
    print(f"api version    {val(info['api_version'])}")
    print(f"firmware       {val(info['firmware_version'])}")
    print(f"owner          {val(info['owner'])}")
    print(f"mac address    {val(info['mac_address'])}")
    if "capacity" in st:
        cap, free = int(st["capacity"]), int(st["available"])
        print(f"storage        {human(cap - free)} used / {human(cap)} ({human(free)} free)")
    else:
        print(f"storage        {val(st)}")
    b = info["battery"]
    if "level" in b:
        print(f"battery        {b['level']}% ({b.get('status', '?')}, {b.get('plugged', '?')})")
    else:
        print(f"battery        {val(b)}")


def cmd_screenshot(q, args):
    with open(args.output, "wb") as fh:
        q.request("GET", "/system/controls/screen_shot", stream_to=fh,
                  headers={"Accept": "*/*"})
    print(f"saved {args.output}")


# --------------------------------------------------------------------------
# open / time
# --------------------------------------------------------------------------

def cmd_open(q, args):
    matches, _ = expand(q, args.remote, args.regex)
    docs = [e for e in matches if e["entry_type"] == "document"]
    if len(docs) != 1:
        if not docs:
            raise QuadernoError(f"{matches[0]['entry_path']} is a folder")
        listing = "\n  ".join(e["entry_path"] for e in docs[:10])
        raise QuadernoError(f"{args.remote!r} matches {len(docs)} documents; be more "
                            f"specific:\n  {listing}")
    e = docs[0]
    if args.page is None:
        q.request("PUT", "/viewer/controls/open", {"document_id": e["entry_id"]})
    else:
        try:
            q.request("PUT", "/viewer/controls/open2",
                      {"document_id": e["entry_id"], "page": int(args.page)})
        except QuadernoError as err:
            if err.status in (404, 405):
                raise QuadernoError("this firmware does not support opening at a page") from err
            raise
    where = f" at page {args.page}" if args.page is not None else ""
    print(f"opened {e['entry_path']}{where} on the device")


def mac_timezone_name():
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return time.strftime("%Z")


def cmd_time(q, args):
    """Set the device clock from the Mac."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    value = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    q.request("PUT", "/system/configs/datetime", {"value": value})
    print(f"device clock set to {value}")
    tz = (q.request("GET", "/system/configs/timezone") or {}).get("value")
    mac_tz = mac_timezone_name()
    print(f"device timezone {tz}, Mac timezone {mac_tz}")
    try:
        from zoneinfo import ZoneInfo
        dev_off = now.astimezone(ZoneInfo(tz)).utcoffset()
    except Exception:
        return
    mac_off = now.astimezone().utcoffset()
    if dev_off != mac_off:
        hours = (mac_off - dev_off).total_seconds() / 3600
        print(f"note: the device shows {abs(hours):g} h {'behind' if hours > 0 else 'ahead of'} "
              f"the Mac's local time because its timezone differs; change it in the "
              f"QUADERNO's own settings")


# --------------------------------------------------------------------------
# sync (one-way, device -> Mac)
# --------------------------------------------------------------------------

SYNC_STATE = ".quaderno-sync.json"


def cmd_sync(q, args):
    """Mirror a device folder into a local directory, downloading only what changed.

    State (entry id -> local path, file_revision, size, mtime) lives in
    LOCAL/.quaderno-sync.json. A local file is only replaced, moved or deleted if it
    is still exactly as sync left it; anything else is kept and reported."""
    root = os.path.abspath(os.path.expanduser(args.local))
    remote = q.resolve(args.remote)
    if remote["entry_type"] != "folder":
        raise QuadernoError(f"{remote['entry_path']} is not a folder")
    remote_path = remote["entry_path"].rstrip("/")
    state_path = os.path.join(root, SYNC_STATE)
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        if state.get("remote") != remote_path:
            raise QuadernoError(f"{root} mirrors {state.get('remote')}, not {remote_path}")
    else:
        if os.path.isdir(root) and os.listdir(root) and not args.force:
            raise QuadernoError(f"{root} is not empty and has no sync state; use an empty "
                                "directory, or --force to sync into it without touching "
                                "existing files")
        state = {"remote": remote_path, "entries": {}}
    entries = state["entries"]
    dry = args.dry_run
    stats = {"new": 0, "updated": 0, "moved": 0, "unchanged": 0, "kept": 0,
             "deleted": 0, "gone": 0, "conflict": 0}

    def save():
        if dry:
            return
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        os.replace(tmp, state_path)

    def local_of(rel):
        return os.path.join(root, *rel.split("/"))

    def pristine(rec):
        """True if the local copy is exactly what sync last wrote."""
        try:
            st = os.stat(local_of(rec["path"]))
        except OSError:
            return False
        return st.st_size == rec["size"] and int(st.st_mtime) == rec["mtime"]

    def act(tag, text):
        print(f"{'(dry run) ' if dry else ''}{tag:7} {text}")

    if not dry:
        os.makedirs(root, exist_ok=True)
    prefix = remote_path + "/"
    seen, planned = set(), {}
    for e in q.walk(remote):
        path = e["entry_path"].rstrip("/")
        if not path.startswith(prefix):
            continue
        rel = "/".join(safe_local_name(p) for p in path[len(prefix):].split("/"))
        key = nfc(rel).casefold()  # macOS volumes are usually case-insensitive
        if key in planned:
            stats["conflict"] += 1
            act("CLASH", f"{rel} (same local name as {planned[key]}; skipped)")
            continue
        planned[key] = rel
        target = local_of(rel)
        if e["entry_type"] == "folder":
            if not dry:
                os.makedirs(target, exist_ok=True)
            continue

        eid, rev = e["entry_id"], e.get("file_revision")
        seen.add(eid)
        old = entries.get(eid)
        if old and old["rev"] == rev and pristine(old):
            if old["path"] == rel:
                stats["unchanged"] += 1
                continue
            if not os.path.exists(target):  # moved/renamed on the device
                act("move", f"{old['path']} -> {rel}")
                if not dry:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.replace(local_of(old["path"]), target)
                    old["path"] = rel
                    save()
                stats["moved"] += 1
                continue
        if os.path.exists(target) and not (old and old["path"] == rel and pristine(old)):
            stats["kept"] += 1
            why = "changed locally" if old and old["path"] == rel else "exists locally, not from sync"
            act("KEEP", f"{rel} ({why}; device version not downloaded)")
            continue
        stats["updated" if old else "new"] += 1
        act("update" if old else "get", rel)
        if dry:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        size = fetch(q, e, target, quiet=True)
        if old and old["path"] != rel and pristine(old):
            os.remove(local_of(old["path"]))  # moved and changed: drop the stale copy
        entries[eid] = {"path": rel, "rev": rev, "size": size,
                        "mtime": int(os.stat(target).st_mtime)}
        save()

    for eid in [k for k in entries if k not in seen]:
        rec = entries[eid]
        if not args.delete:
            stats["gone"] += 1
            act("gone", f"{rec['path']} (deleted on device; kept, use --delete)")
            continue
        if pristine(rec):
            act("delete", rec["path"])
            if not dry:
                os.remove(local_of(rec["path"]))
                d = os.path.dirname(local_of(rec["path"]))
                while d != root and os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)
            stats["deleted"] += 1
        else:
            stats["kept"] += 1
            act("KEEP", f"{rec['path']} (deleted on device but changed locally)")
        if not dry:
            del entries[eid]
    save()
    parts = [f"{v} {k}" for k, v in stats.items() if v]
    print(f"{'(dry run) ' if dry else ''}sync {remote_path} -> {root}: "
          + (", ".join(parts) or "nothing to do"))


def complete(argv):
    """`quaderno __complete [--dirs] [--] PREFIX`: print device paths for shell tab
    completion, one per line, folders ending in '/'. Lists the folder PREFIX is in,
    keeping the folder part exactly as typed. Never prints errors; prints nothing
    when the device isn't connected."""
    dirs_only = "--dirs" in argv
    words = [a for a in argv if a not in ("--dirs", "--")]
    prefix = words[0] if words else ""
    try:
        addr = os.environ.get("QUADERNO_ADDR")
        if not addr and not usb_devices():
            return 0  # unplugged: answer instantly instead of waiting for discovery
        cred_dir = find_credentials(None)
        client_id, key, serial = load_credentials(cred_dir)
        q = Quaderno(addr or discover(serial), client_id, key, timeout=5,
                     pinned_cert=load_pinned_cert(cred_dir))
        q.authenticate()
        typed_dir, sep, _ = prefix.rpartition("/")
        folder = q.resolve(typed_dir)
        if folder["entry_type"] != "folder":
            return 0
        for e in sorted(q.entries(folder["entry_id"]), key=lambda e: e["entry_name"].lower()):
            is_dir = e["entry_type"] == "folder"
            if dirs_only and not is_dir:
                continue
            print(typed_dir + sep + e["entry_name"] + ("/" if is_dir else ""))
    except (QuadernoError, OSError, ValueError, KeyError):
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__complete"]:
        return complete(argv[1:])
    ap = argparse.ArgumentParser(prog="quaderno", description=__doc__.split("\n")[0])
    ap.add_argument("--addr", help="device address, e.g. 'fe80::...%%en8' (default: auto)")
    ap.add_argument("--workspace", help="credentials dir (default: ~/.config/quaderno, "
                                        "else the PC App's pairing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # Remote paths in ls/get/mv/rm/open accept glob patterns ('Fighter*'); -E makes
    # them regular expressions. Quote them so the shell leaves them alone.
    pat = argparse.ArgumentParser(add_help=False)
    pat.add_argument("-E", "--regex", action="store_true",
                     help="treat remote path components as regular expressions "
                          "(case-insensitive search) instead of glob patterns")
    _add_parser = sub.add_parser

    def add_parser(name, **kw):
        if name in ("ls", "get", "mv", "rm", "open"):
            kw["parents"] = [pat]
        return _add_parser(name, **kw)
    sub.add_parser = add_parser

    p = sub.add_parser("ls", help="list a folder (default: Document)")
    p.add_argument("path", nargs="?", default=ROOT)
    p.add_argument("-l", "--long", action="store_true", help="size, date, pages")
    p.add_argument("-R", "--recursive", action="store_true")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("get", help="download documents (or folders with -r)")
    p.add_argument("remote", nargs="+")
    p.add_argument("-o", "--output", help="file or directory (default: .)")
    p.add_argument("-r", "--recursive", action="store_true", help="download folders")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("put", help="upload PDFs (or directories with -r)")
    p.add_argument("local", nargs="+")
    p.add_argument("-d", "--dest", dest="remote", help="remote folder or new path (default: Document)")
    p.add_argument("-r", "--recursive", action="store_true", help="upload directories")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("rm", help="delete documents or empty folders (-r: with contents)")
    p.add_argument("remote", nargs="+")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before deleting pattern matches")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("mkdir", help="create folders")
    p.add_argument("remote", nargs="+")
    p.add_argument("-p", "--parents", action="store_true", help="create missing parents; ok if exists")
    p.set_defaults(fn=cmd_mkdir)

    p = sub.add_parser("mv", help="move or rename documents/folders (several -> into a folder)")
    p.add_argument("src", nargs="+")
    p.add_argument("dst")
    p.set_defaults(fn=cmd_mv)

    p = sub.add_parser("info", help="firmware, storage, battery, owner")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("screenshot", help="save a PNG of the current screen")
    p.add_argument("output", nargs="?", default="quaderno-screen.png")
    p.set_defaults(fn=cmd_screenshot)

    p = sub.add_parser("sync", help="mirror a device folder into a local directory (incremental)")
    p.add_argument("local", help="local directory (created if missing)")
    p.add_argument("remote", nargs="?", default=ROOT, help="device folder (default: Document)")
    p.add_argument("--delete", action="store_true",
                   help="also delete local copies of documents deleted on the device")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--force", action="store_true",
                   help="allow a first sync into a non-empty directory (existing files are kept)")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("open", help="open a document on the device's screen")
    p.add_argument("remote")
    p.add_argument("-p", "--page", type=int, help="page number")
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("time", help="set the device clock from the Mac")
    p.set_defaults(fn=cmd_time)

    p = sub.add_parser("addr", help="print the device address (switches USB mode if needed)")
    p.set_defaults(fn=None)

    p = sub.add_parser("pair", help="pair with the device using the PIN on its screen")
    p.add_argument("--force", action="store_true", help=f"replace an existing pairing in {CONFIG_DIR}")
    p.add_argument("--pin-file", help="read the PIN from this file once it appears (instead of a prompt)")
    p.set_defaults(fn=None)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "pair":
            cmd_pair(args)
            return 0
        if args.cmd == "addr":
            try:
                serial = load_credentials(find_credentials(args.workspace))[2]
            except QuadernoError:
                serial = None
            print(args.addr or discover(serial, args.verbose))
            return 0
        q = connect(args)
        return args.fn(q, args) or 0
    except QuadernoError as err:
        print(f"quaderno: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
