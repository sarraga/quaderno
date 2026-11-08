"""Crypto wiring and the pairing protocol, against a simulated device.

The simulated device uses independent implementations (Python pow() for DH, the
system's /usr/bin/openssl for AES-CBC, stdlib hmac), so a mistake in how quaderno
calls the `cryptography` library can't be mirrored on the other side.
If the PC App has a pairing on this Mac, its publickey.dat is read (never written)
to compare the public key layout."""
import base64
import glob
import hashlib
import hmac
import io
import os
import secrets
import subprocess
import types

import pytest

from quaderno import cli, client, credentials, crypto, pairing
from quaderno.errors import QuadernoError

OPENSSL = "/usr/bin/openssl"


def ossl(args, data):
    return subprocess.run([OPENSSL, *args], input=data, capture_output=True, check=True).stdout


def ossl_aes(decrypt, key, iv, data):
    return ossl(["enc", "-d" if decrypt else "-e", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()], data)


def H(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def der(pem):
    return base64.b64decode("".join(pem.strip().splitlines()[1:-1]))


@pytest.fixture(scope="module")
def keypair():
    return crypto.generate_keypair()


# --- 1. AES-CBC + PKCS#7 wiring vs openssl -----------------------------------------

@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 33, 300])
def test_aes_cbc_matches_openssl(n):
    key, iv, data = secrets.token_bytes(16), secrets.token_bytes(16), secrets.token_bytes(n)
    ours = crypto.aes_cbc_encrypt(key, iv, data)
    assert ours == ossl_aes(False, key, iv, data)
    assert crypto.aes_cbc_decrypt(key, iv, ours) == data


def test_aes_cbc_rejects_bad_padding():
    with pytest.raises(QuadernoError):
        crypto.aes_cbc_decrypt(bytes(16), bytes(16), b"\x00" * 16)


# --- 2. RSA: key generation, PEM layout, signing -------------------------------------

def test_rsa_keys_and_signatures(keypair, tmp_path):
    priv, pub = keypair
    pp, sp = tmp_path / "priv.pem", tmp_path / "pub.pem"
    pp.write_text(priv)
    assert priv.startswith("-----BEGIN RSA PRIVATE KEY-----\n")
    r = subprocess.run([OPENSSL, "rsa", "-check", "-noout", "-in", pp], capture_output=True, text=True)
    assert "RSA key ok" in (r.stdout + r.stderr), r.stdout + r.stderr
    sp.write_text(pub.replace("RSA PUBLIC KEY", "PUBLIC KEY"))

    def modulus(*a):
        return subprocess.run([OPENSSL, "rsa", *a, "-noout", "-modulus"], capture_output=True,
                              text=True, check=True).stdout
    assert modulus("-pubin", "-in", sp) == modulus("-in", pp)
    (tmp_path / "m").write_bytes(b"nonce")
    (tmp_path / "s").write_bytes(crypto.sign(crypto.load_private_key(priv), b"nonce"))
    v = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", sp, "-signature", tmp_path / "s",
                        tmp_path / "m"], capture_output=True, text=True)
    assert "Verified OK" in v.stdout, v.stdout + v.stderr
    assert len(der(pub)) == 294 and pub.startswith("-----BEGIN RSA PUBLIC KEY-----\n")
    assert pub.endswith("-----\n")


def test_openssl_made_pkcs1_key_loads_and_signs(tmp_path):
    kp = tmp_path / "k1.pem"
    subprocess.run([OPENSSL, "genrsa", "-out", kp, "2048"], capture_output=True, check=True)
    crypto.sign(crypto.load_private_key(kp.read_text()), b"x")


def test_public_key_layout_matches_the_pc_app(keypair):
    found = glob.glob(os.path.join(credentials.APP_DIR, "ws*", "publickey.dat"))
    if not found:
        pytest.skip("no PC App pairing on this Mac")
    with open(found[0]) as f:
        app_pub = f.read()
    pub = keypair[1]
    assert der(pub)[:32] == der(app_pub)[:32] and len(der(pub)) == len(der(app_pub))
    assert [len(line) for line in pub.splitlines()] == [len(line) for line in app_pub.splitlines()]


# --- 3. The pairing protocol against a simulated device ------------------------------

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


CID = "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("run", range(3))  # fresh DH keys each time
def test_pairing_round_trip(run, keypair, monkeypatch):
    pub = keypair[1]
    dev = FakeRegDevice("4242")
    monkeypatch.setattr(pairing, "reg_request", dev)
    cert = pairing.register("fake", CID, pub, lambda: "4242\n")
    assert dev.registered == (CID, pub) and cert == dev.cert


def test_pairing_with_the_wrong_pin(keypair, monkeypatch):
    dev = FakeRegDevice("4242")
    monkeypatch.setattr(pairing, "reg_request", dev)
    with pytest.raises(QuadernoError, match="wrong PIN"):
        pairing.register("fake", CID, keypair[1], lambda: "9999")
    assert dev.registered is None


# --- 4. `quaderno pair` end to end, config dir isolated, login stubbed --------------

def test_cli_pair(tmp_path, monkeypatch, capsys):
    cfg = str(tmp_path / "cfg")
    monkeypatch.setattr(credentials, "CONFIG_DIR", cfg)
    monkeypatch.setattr(credentials, "APP_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(client.Client, "authenticate", lambda self: None)

    def run_pair(dev, pin, force=False):
        monkeypatch.setattr(pairing, "reg_request", dev)
        monkeypatch.setattr("sys.stdin", io.StringIO(pin + "\n"))
        cli.cmd_pair(types.SimpleNamespace(addr="fake", verbose=False, force=force, pin_file=None))
        return capsys.readouterr().out

    dev = FakeRegDevice("1357")
    out = run_pair(dev, "1357")
    assert "paired: client" in out and "authentication with the new key: OK" in out, out
    assert sorted(os.listdir(cfg)) == ["deviceid.dat", "privatekey.dat", "publickey.dat", "serial.dat"]
    assert oct(os.stat(os.path.join(cfg, "privatekey.dat")).st_mode & 0o777) == "0o600"
    assert oct(os.stat(cfg).st_mode & 0o777) == "0o700"
    creds = credentials.load_credentials(credentials.find_credentials_dir())
    assert creds.source == cfg and creds.client_id == dev.registered[0]
    assert creds.serial == "000000000000001"
    assert creds.pinned_cert is None and dev.cleanups == 2  # the fake cert is malformed: not stored

    with pytest.raises(QuadernoError, match="already paired"):   # re-pair without --force
        run_pair(FakeRegDevice("1357"), "1357")

    cp = tmp_path / "c.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", os.devnull,
                    "-subj", "/CN=digitalpaper.local", "-days", "1", "-out", cp],
                   check=True, capture_output=True)
    dev = FakeRegDevice("2468")
    dev.cert = cp.read_bytes()
    run_pair(dev, "2468", force=True)
    creds = credentials.load_credentials(cfg)
    assert creds.pinned_cert == crypto.cert_to_der(dev.cert) and creds.client_id == dev.registered[0]

    run_pair(FakeRegDevice("1111"), "1111", force=True)    # unreadable cert: stale pin removed
    assert credentials.load_pinned_cert(cfg) is None
