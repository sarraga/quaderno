#!/usr/bin/env python3
import base64, hashlib, os, secrets, subprocess, sys, tempfile, time
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PUB = sys.argv[2] if len(sys.argv) > 2 else None  # optional: PC App publickey.dat to compare layout
import quaderno as Q

OPENSSL = "/usr/bin/openssl"
tmp = tempfile.mkdtemp()

# 1. AES-128 block: FIPS-197 Appendix C.1
k = bytes(range(16)); pt = bytes.fromhex("00112233445566778899aabbccddeeff")
rk = Q._aes_round_keys(k)
ct = Q._aes_encrypt_block(rk, pt)
assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()
assert Q._aes_decrypt_block(rk, ct) == pt
print("AES FIPS-197 C.1: ok")

# 2. CBC + PKCS#7 vs system openssl, several lengths
for n in (0, 1, 15, 16, 17, 33, 300):
    key, iv, data = secrets.token_bytes(16), secrets.token_bytes(16), secrets.token_bytes(n)
    ours = Q.aes128_cbc_encrypt(key, iv, data)
    ref = subprocess.run([OPENSSL, "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
                         input=data, capture_output=True, check=True).stdout
    assert ours == ref, n
    assert Q.aes128_cbc_decrypt(key, iv, ours) == data
print("AES-CBC vs openssl (7 lengths): ok")

# 3. RSA keygen
t = time.time()
priv, pub = Q.generate_rsa_key()
print(f"RSA keygen: {time.time() - t:.1f}s")
pp = os.path.join(tmp, "priv.pem"); open(pp, "w").write(priv)
r = subprocess.run([OPENSSL, "rsa", "-check", "-noout", "-in", pp], capture_output=True, text=True)
assert "RSA key ok" in (r.stdout + r.stderr), r.stdout + r.stderr
spki_pem = pub.replace("RSA PUBLIC KEY", "PUBLIC KEY")
sp = os.path.join(tmp, "pub.pem"); open(sp, "w").write(spki_pem)
mod_pub = subprocess.run([OPENSSL, "rsa", "-pubin", "-in", sp, "-noout", "-modulus"], capture_output=True, text=True, check=True).stdout
mod_priv = subprocess.run([OPENSSL, "rsa", "-in", pp, "-noout", "-modulus"], capture_output=True, text=True, check=True).stdout
assert mod_pub == mod_priv
key = Q.load_rsa_private_key(priv)
sig = Q.rsa_sign_sha256(key, b"nonce")
vp = os.path.join(tmp, "m"); open(vp, "wb").write(b"nonce")
sf = os.path.join(tmp, "s"); open(sf, "wb").write(sig)
v = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", sp, "-signature", sf, vp], capture_output=True, text=True)
assert "Verified OK" in v.stdout, v.stdout + v.stderr
der = lambda pem: base64.b64decode("".join(pem.strip().splitlines()[1:-1]))
app_pub = open(APP_PUB).read() if APP_PUB else None
if app_pub:
  assert der(pub)[:32] == der(app_pub)[:32] and len(der(pub)) == len(der(app_pub)) == 294
  assert pub.splitlines()[0] == app_pub.splitlines()[0] and pub.endswith("-----\n") and app_pub.endswith("-----\n")
  assert [len(l) for l in pub.splitlines()] == [len(l) for l in app_pub.splitlines()]
print("RSA: openssl check, modulus match, signature verifies, layout identical to PC App key: ok")


# 4. Pairing round-trip against a simulated device (device side of the app's protocol)
class FakeRegDevice:
    def __init__(self, pin):
        self.pin = pin.encode(); self.registered = None; self.cert = b"-----BEGIN CERTIFICATE-----\nFAKE\n"
        self.cleanups = 0

    def __call__(self, addr, method, path, body=None):
        H = Q._hmac; u = base64.b64decode; b = Q._b64
        if path == "/register/cleanup":
            self.cleanups += 1; return 204, None
        if path == "/register/information":
            return 200, {"serial_number": "000000000000001"}
        if path == "/register/pin":
            self.n1, self.mac = secrets.token_bytes(16), secrets.token_bytes(6)
            self.x = secrets.randbelow(Q._DH_P - 3) + 2
            yb = pow(2, self.x, Q._DH_P).to_bytes(256, "big")
            self.yb = b"\x00" + yb  # Java-style sign byte
            return 200, {"a": b(self.n1), "b": b(self.mac), "c": b(self.yb)}
        if path == "/register/hash":
            n2, ya, m2 = u(body["b"]), u(body["d"]), u(body["e"])
            zz = pow(int.from_bytes(ya, "big"), self.x, Q._DH_P).to_bytes(256, "big")
            dk = hashlib.pbkdf2_hmac("sha256", zz, self.n1 + self.mac + n2, 10000, 48)
            self.ak, self.kw, self.n2, self.ya, self.m2 = dk[:32], dk[32:], n2, ya, m2
            if H(self.ak, self.n1 + self.mac + self.yb + self.n1 + n2 + self.mac + ya) != m2:
                return 403, {"error_code": "40300"}
            self.psk = H(self.ak, self.pin)
            self.es = secrets.token_bytes(16)
            self.eh = H(self.ak, self.es + self.psk + self.yb + ya)
            self.m3 = H(self.ak, self.n1 + n2 + self.mac + ya + m2 + n2 + self.eh)
            return 200, {"a": b(n2), "b": b(self.eh), "e": b(self.m3)}
        if path == "/register/ca":
            rh, wrs, m4 = u(body["b"]), u(body["d"]), u(body["e"])
            if H(self.ak, self.n2 + self.eh + self.m3 + self.n1 + rh + wrs) != m4:
                return 403, {"error_code": "40300"}
            rs = Q._unwrap(wrs, self.ak, self.kw)
            if H(self.ak, rs + self.psk + self.yb + self.ya) != rh:
                return 403, {"error_code": "40301"}
            self.wes = Q._wrap(self.es + self.cert, self.ak, self.kw)
            self.m5 = H(self.ak, self.n1 + rh + wrs + m4 + self.n2 + self.wes)
            return 200, {"a": b(self.n2), "d": b(self.wes), "e": b(self.m5)}
        if path == "/register":
            wd, m6 = u(body["d"]), u(body["e"])
            if H(self.ak, self.n2 + self.wes + self.m5 + self.n1 + wd) != m6:
                return 403, {"error_code": "40300"}
            data = Q._unwrap(wd, self.ak, self.kw).decode()
            self.registered = (data[:36], data[36:])
            return 204, None
        raise AssertionError(path)


cid = "11111111-2222-4333-8444-555555555555"
dev = FakeRegDevice("4242")
Q._reg_request = dev
cert = Q.register("fake", cid, pub, lambda: "4242\n")
assert dev.registered == (cid, pub) and cert == dev.cert
print("pairing round-trip (correct PIN): ok; device received client id + public key intact")
dev = FakeRegDevice("4242"); Q._reg_request = dev
try:
    Q.register("fake", cid, pub, lambda: "9999"); raise SystemExit("wrong PIN accepted")
except Q.QuadernoError as e:
    assert "wrong PIN" in str(e) and dev.registered is None
    print("pairing with wrong PIN: rejected, nothing registered: ok")

# 5. cmd_pair end-to-end with config dir isolated (auth step stubbed)
Q.CONFIG_DIR = os.path.join(tmp, "cfg")
dev = FakeRegDevice("1357"); Q._reg_request = dev
Q.Quaderno.authenticate = lambda self: None
import io, types
sys.stdin = io.StringIO("1357\n")
Q.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=False))
files = sorted(os.listdir(Q.CONFIG_DIR))
assert files == ["deviceid.dat", "privatekey.dat", "publickey.dat", "serial.dat"], files  # fake cert is malformed
assert oct(os.stat(os.path.join(Q.CONFIG_DIR, "privatekey.dat")).st_mode & 0o777) == "0o600"
assert oct(os.stat(Q.CONFIG_DIR).st_mode & 0o777) == "0o700"
assert Q.find_credentials() == Q.CONFIG_DIR
cid2, k2, ser = Q.load_credentials(Q.CONFIG_DIR)
assert dev.registered[0] == cid2 and ser == "000000000000001" and dev.cleanups == 2
assert Q.load_pinned_cert(Q.CONFIG_DIR) is None  # malformed cert was not stored
try:
    Q.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=False)); raise SystemExit("re-pair allowed")
except Q.QuadernoError as e:
    print("re-pair without --force refused:", e)

# 6. --force re-pair with a real certificate -> stored and loadable as pin
cp = os.path.join(tmp, "c.pem")
subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", os.devnull,
                "-subj", "/CN=digitalpaper.local", "-days", "1", "-out", cp], check=True, capture_output=True)
dev = FakeRegDevice("2468"); dev.cert = open(cp, "rb").read(); Q._reg_request = dev
sys.stdin = io.StringIO("2468\n")
Q.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=True))
assert Q.load_pinned_cert(Q.CONFIG_DIR) == Q.ssl.PEM_cert_to_DER_cert(dev.cert.decode())
assert Q.load_credentials(Q.CONFIG_DIR)[0] == dev.registered[0]
print("--force re-pair with real cert: replaced credentials, pin stored: ok")

# 7. --force re-pair where device cert is unreadable -> old pin removed
dev = FakeRegDevice("1111"); Q._reg_request = dev
sys.stdin = io.StringIO("1111\n")
Q.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=True))
assert Q.load_pinned_cert(Q.CONFIG_DIR) is None
print("--force re-pair with unreadable cert: stale pin removed: ok")
print("ALL PAIRING TESTS PASSED")
