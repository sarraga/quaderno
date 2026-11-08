"""Cryptography for login and pairing: RSA, AES and key files via the
`cryptography` package; HMAC and PBKDF2 from the standard library; the one
Diffie-Hellman exchange with pow() (see dh_exchange).

Only the encodings the device expects are done here: an SPKI public key under an
'RSA PUBLIC KEY' PEM label, the 0x00-prefixed DH public value, and the HMAC-tagged
AES-CBC "wrap" used inside pairing messages. The first two copy the official
QUADERNO app byte for byte; whether the device also accepts the standard forms (a
'PUBLIC KEY' label, an unprefixed value) has not been tested."""

import base64
import hashlib
import hmac
import secrets
import ssl

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import AuthError, PairingError

# RFC 3526 group 14 (2048-bit MODP), generator 2: the group the device pairs with.
DH_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
DH_G = 2


# --- RSA ---------------------------------------------------------------------

def pem_encode(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def generate_keypair(bits: int = 2048) -> tuple[str, str]:
    """(private_pem, public_pem): PKCS#1 'RSA PRIVATE KEY', and the public key as
    SubjectPublicKeyInfo DER under an 'RSA PUBLIC KEY' label, the layout the device
    accepts at pairing (see the module docstring)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()).decode()
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, pem_encode("RSA PUBLIC KEY", spki)


def load_private_key(pem: str | bytes) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem,
                                                 password=None)
    except (ValueError, TypeError) as err:
        raise AuthError(f"cannot read the private key: {err}") from err
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError("the private key is not an RSA key")
    return key


def sign(key: rsa.RSAPrivateKey, data: bytes) -> bytes:
    """RSA-SHA256, PKCS#1 v1.5 (the login nonce signature)."""
    return key.sign(data, padding.PKCS1v15(), hashes.SHA256())


# --- symmetric pieces of pairing ---------------------------------------------

def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        unpadder = sym_padding.PKCS7(128).unpadder()
        return unpadder.update(dec.update(data) + dec.finalize()) + unpadder.finalize()
    except ValueError as err:
        raise PairingError(f"pairing: undecryptable message ({err})")


def wrap(data: bytes, auth_key: bytes, kw_key: bytes) -> bytes:
    """AES-128-CBC(data + first 8 bytes of HMAC-SHA256(data)) + IV: the format the
    device uses in both directions during pairing."""
    iv = secrets.token_bytes(16)
    return aes_cbc_encrypt(kw_key, iv, data + hmac_sha256(auth_key, data)[:8]) + iv


def unwrap(data: bytes, auth_key: bytes, kw_key: bytes) -> bytes:
    plain = aes_cbc_decrypt(kw_key, data[-16:], data[:-16])
    body, tag = plain[:-8], plain[-8:]
    if not hmac.compare_digest(tag, hmac_sha256(auth_key, body)[:8]):
        raise PairingError("pairing: wrapped data failed its integrity check")
    return body


def dh_exchange(peer_public: int) -> tuple[bytes, bytes]:
    """Diffie-Hellman against the device's public value. Returns (ya, zz): our
    public value as 0x00 + 256 bytes (see the module docstring) and the 256-byte
    shared secret. Keys are regenerated until both values have their full length,
    so leading-zero encoding conventions cannot differ from the device's.

    Done with Python's pow() on purpose: the device requires classic finite-field
    DH (RFC 3526 group 14), which the `cryptography` package has deprecated for
    removal; two modular exponentiations need no library."""
    if not 1 < peer_public < DH_P - 1:
        raise PairingError("pairing: device sent an invalid DH value")
    while True:
        x = secrets.randbelow(DH_P - 3) + 2
        y, zz = pow(DH_G, x, DH_P), pow(peer_public, x, DH_P)
        if y.bit_length() > 2040 and zz.bit_length() > 2040:
            return b"\x00" + y.to_bytes(256, "big"), zz.to_bytes(256, "big")


# --- certificates ------------------------------------------------------------

def cert_to_der(data: bytes) -> bytes | None:
    """PEM or DER certificate bytes -> DER, or None if it is not readable."""
    try:
        if data.startswith(b"-----BEGIN"):
            return ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
        return data if data[:1] == b"\x30" else None
    except (ValueError, UnicodeDecodeError):
        return None
