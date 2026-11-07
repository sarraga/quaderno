#!/usr/bin/env python3
"""Crypto wiring and the pairing protocol, against a simulated device.

The simulated device uses independent implementations (Python pow() for DH, the
system's /usr/bin/openssl for AES-CBC, stdlib hmac), so a mistake in how quaderno
calls the `cryptography` library can't be mirrored on the other side.
Optional argument: the PC App's publickey.dat, to compare the public key layout."""
import base64
import hashlib
import io
import os
import secrets
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PUB = sys.argv[1] if len(sys.argv) > 1 else None
from quaderno import cli, client, credentials, crypto, pairing  # noqa: E402
from quaderno.errors import QuadernoError  # noqa: E402

OPENSSL = "/usr/bin/openssl"
tmp = tempfile.mkdtemp()


def ossl(args, data):
    return subprocess.run([OPENSSL, *args], input=data, capture_output=True, check=True).stdout


def ossl_aes(decrypt, key, iv, data):
    return ossl(["enc", "-d" if decrypt else "-e", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()], data)


def H(key, data):
    import hmac
    return hmac.new(key, data, hashlib.sha256).digest()


# 1. AES-CBC + PKCS#7 wiring vs openssl, several lengths
for n in (0, 1, 15, 16, 17, 33, 300):
    key, iv, data = secrets.token_bytes(16), secrets.token_bytes(16), secrets.token_bytes(n)
    ours = crypto.aes_cbc_encrypt(key, iv, data)
    assert ours == ossl_aes(False, key, iv, data), n
    assert crypto.aes_cbc_decrypt(key, iv, ours) == data
try:
    crypto.aes_cbc_decrypt(bytes(16), bytes(16), b"\x00" * 16)
    raise SystemExit("bad padding accepted")
except QuadernoError:
    pass
print("AES-CBC vs openssl (7 lengths), bad padding rejected: ok")

# 2. RSA: key generation, PEM layout, signing
priv, pub = crypto.generate_keypair()
pp = os.path.join(tmp, "priv.pem"); open(pp, "w").write(priv)
assert priv.startswith("-----BEGIN RSA PRIVATE KEY-----\n")
r = subprocess.run([OPENSSL, "rsa", "-check", "-noout", "-in", pp], capture_output=True, text=True)
assert "RSA key ok" in (r.stdout + r.stderr), r.stdout + r.stderr
sp = os.path.join(tmp, "pub.pem"); open(sp, "w").write(pub.replace("RSA PUBLIC KEY", "PUBLIC KEY"))
mod = lambda *a: subprocess.run([OPENSSL, "rsa", *a, "-noout", "-modulus"], capture_output=True, text=True, check=True).stdout
assert mod("-pubin", "-in", sp) == mod("-in", pp)
sig = crypto.sign(crypto.load_private_key(priv), b"nonce")
open(os.path.join(tmp, "m"), "wb").write(b"nonce"); open(os.path.join(tmp, "s"), "wb").write(sig)
v = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", sp, "-signature", os.path.join(tmp, "s"),
                    os.path.join(tmp, "m")], capture_output=True, text=True)
assert "Verified OK" in v.stdout, v.stdout + v.stderr
der = lambda pem: base64.b64decode("".join(pem.strip().splitlines()[1:-1]))
assert len(der(pub)) == 294 and pub.startswith("-----BEGIN RSA PUBLIC KEY-----\n") and pub.endswith("-----\n")
if APP_PUB:
    app_pub = open(APP_PUB).read()
    assert der(pub)[:32] == der(app_pub)[:32] and len(der(pub)) == len(der(app_pub))
    assert [len(l) for l in pub.splitlines()] == [len(l) for l in app_pub.splitlines()]
# a PKCS#1 key made by openssl itself loads and signs
kp = os.path.join(tmp, "k1.pem")
subprocess.run([OPENSSL, "genrsa", "-out", kp, "2048"], capture_output=True, check=True)
crypto.sign(crypto.load_private_key(open(kp).read()), b"x")
print("RSA: openssl key check, modulus match, signature verifies, PEM layout"
      + (" identical to PC App key" if APP_PUB else "") + ", openssl key loads: ok")


# 3. Pairing protocol against a simulated device
class FakeRegDevice:
    def __init__(self, pin):
        self.pin = pin.encode()
        self.registered = None
        self.cert = b"-----BEGIN CERTIFICATE-----\nFAKE\n"
        self.cleanups = 0

    def wrap(self, data):
        iv = secrets.token_bytes(16)
        return ossl_aes(False, self.kw, iv, data + H(self.ak, data)[:8]) + iv

    def unwrap(self, data):
        plain = ossl_aes(True, self.kw, data[-16:], data[:-16])
        assert plain[-8:] == H(self.ak, plain[:-8])[:8], "client's wrap tag is wrong"
        return plain[:-8]

    def __call__(self, addr, method, path, body=None):
        u, b = base64.b64decode, lambda x: base64.b64encode(x).decode()
        P = crypto.DH_P
        if path == "/register/cleanup":
            self.cleanups += 1
            return 204, None
        if path == "/register/information":
            return 200, {"serial_number": "000000000000001"}
        if path == "/register/pin":
            self.n1, self.mac = secrets.token_bytes(16), secrets.token_bytes(6)
            self.x = secrets.randbelow(P - 3) + 2
            self.yb = b"\x00" + pow(2, self.x, P).to_bytes(256, "big")  # Java-style sign byte
            return 200, {"a": b(self.n1), "b": b(self.mac), "c": b(self.yb)}
        if path == "/register/hash":
            n2, ya, m2 = u(body["b"]), u(body["d"]), u(body["e"])
            assert len(ya) == 257 and ya[0] == 0, "ya must be 0x00 + 256 bytes"
            zz = pow(int.from_bytes(ya, "big"), self.x, P).to_bytes(256, "big")
            dk = hashlib.pbkdf2_hmac("sha256", zz, self.n1 + self.mac + n2, 10000, 48)
            self.ak, self.kw, self.n2, self.ya = dk[:32], dk[32:], n2, ya
            if H(self.ak, self.n1 + self.mac + self.yb + self.n1 + n2 + self.mac + ya) != m2:
                return 403, {"error_code": "40300"}  # wrong shared secret => wrong DH
            self.psk = H(self.ak, self.pin)
            self.es = secrets.token_bytes(16)
            self.eh = H(self.ak, self.es + self.psk + self.yb + ya)
            self.m3 = H(self.ak, self.n1 + n2 + self.mac + ya + m2 + n2 + self.eh)
            return 200, {"a": b(n2), "b": b(self.eh), "e": b(self.m3)}
        if path == "/register/ca":
            rh, wrs, m4 = u(body["b"]), u(body["d"]), u(body["e"])
            if H(self.ak, self.n2 + self.eh + self.m3 + self.n1 + rh + wrs) != m4:
                return 403, {"error_code": "40300"}
            rs = self.unwrap(wrs)
            if H(self.ak, rs + self.psk + self.yb + self.ya) != rh:
                return 403, {"error_code": "40301"}
            self.wes = self.wrap(self.es + self.cert)
            self.m5 = H(self.ak, self.n1 + rh + wrs + m4 + self.n2 + self.wes)
            return 200, {"a": b(self.n2), "d": b(self.wes), "e": b(self.m5)}
        if path == "/register":
            wd, m6 = u(body["d"]), u(body["e"])
            if H(self.ak, self.n2 + self.wes + self.m5 + self.n1 + wd) != m6:
                return 403, {"error_code": "40300"}
            data = self.unwrap(wd).decode()
            self.registered = (data[:36], data[36:])
            return 204, None
        raise AssertionError(path)


cid = "11111111-2222-4333-8444-555555555555"
for _ in range(3):  # several runs: fresh DH keys each time
    dev = FakeRegDevice("4242")
    pairing.reg_request = dev
    cert = pairing.register("fake", cid, pub, lambda: "4242\n")
    assert dev.registered == (cid, pub) and cert == dev.cert
print("pairing round-trip x3 (correct PIN): ok; device got client id + public key intact")
dev = FakeRegDevice("4242")
pairing.reg_request = dev
try:
    pairing.register("fake", cid, pub, lambda: "9999")
    raise SystemExit("wrong PIN accepted")
except QuadernoError as e:
    assert "wrong PIN" in str(e) and dev.registered is None
print("pairing with wrong PIN: rejected, nothing registered: ok")

# 4. `quaderno pair` end to end, config dir isolated, login stubbed
credentials.CONFIG_DIR = cfg = os.path.join(tmp, "cfg")
client.Client.authenticate = lambda self: None


def run_pair(pin, force=False):
    sys.stdin = io.StringIO(pin + "\n")
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        cli.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=force, pin_file=None))
    finally:
        sys.stdout = real
    return out.getvalue()


dev = FakeRegDevice("1357")
pairing.reg_request = dev
out = run_pair("1357")
assert "paired: client" in out and "authentication with the new key: OK" in out, out
assert sorted(os.listdir(cfg)) == ["deviceid.dat", "privatekey.dat", "publickey.dat", "serial.dat"]
assert oct(os.stat(os.path.join(cfg, "privatekey.dat")).st_mode & 0o777) == "0o600"
assert oct(os.stat(cfg).st_mode & 0o777) == "0o700"
creds = credentials.load_credentials(credentials.find_credentials_dir())
assert creds.source == cfg and creds.client_id == dev.registered[0] and creds.serial == "000000000000001"
assert creds.pinned_cert is None and dev.cleanups == 2  # the fake cert is malformed: not stored
try:
    run_pair("1357")
    raise SystemExit("re-pair allowed")
except QuadernoError as e:
    print("re-pair without --force refused:", e)

cp = os.path.join(tmp, "c.pem")
subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", os.devnull,
                "-subj", "/CN=digitalpaper.local", "-days", "1", "-out", cp], check=True, capture_output=True)
dev = FakeRegDevice("2468")
dev.cert = open(cp, "rb").read()
pairing.reg_request = dev
run_pair("2468", force=True)
creds = credentials.load_credentials(cfg)
assert creds.pinned_cert == crypto.cert_to_der(dev.cert) and creds.client_id == dev.registered[0]
print("--force re-pair with real cert: replaced credentials, pin stored: ok")

dev = FakeRegDevice("1111")
pairing.reg_request = dev
run_pair("1111", force=True)
assert credentials.load_pinned_cert(cfg) is None
print("--force re-pair with unreadable cert: stale pin removed: ok")
print("ALL PAIRING TESTS PASSED")
