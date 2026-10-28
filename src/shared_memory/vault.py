"""Secret vault for delegated-inference connections (interface:delegated-inference-v0 §3).

- At rest: Fernet (AES-128-CBC + HMAC) under JUNTO_VAULT_KEY (in .env on the
  server host, never in git). Only `encrypt`/`decrypt` touch it.
- In transit to a runner: sealed to that runner's X25519 public key (ECDH with
  an ephemeral key → HKDF-SHA256 → AES-256-GCM). Only the runner holding the
  private key can open it; the junto HTTP hop can stay plain on the LAN.

Nothing here logs or returns plaintext to an MCP caller; callers must not either.
"""

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEAL_INFO = b"junto-delegation-seal-v1"
MAX_SECRET_BYTES = 16384


class VaultError(Exception):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("JUNTO_VAULT_KEY", "").strip()
    if not key:
        raise VaultError("JUNTO_VAULT_KEY is not set on the server")
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise VaultError(f"JUNTO_VAULT_KEY is not a valid Fernet key: {e}")


def available() -> bool:
    try:
        _fernet()
        return True
    except VaultError:
        return False


def encrypt(secret: str) -> str:
    data = secret.encode()
    if not data or len(data) > MAX_SECRET_BYTES:
        raise VaultError(f"secret must be 1..{MAX_SECRET_BYTES} bytes")
    return _fernet().encrypt(data).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise VaultError("vault token does not decrypt (wrong JUNTO_VAULT_KEY?)")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


def _derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=SEAL_INFO).derive(shared)


def valid_public_key(pub_b64: str) -> bool:
    try:
        X25519PublicKey.from_public_bytes(_unb64(pub_b64))
        return True
    except Exception:
        return False


def seal(secret: str, runner_public_key_b64: str) -> dict:
    """Seal plaintext to a runner's X25519 public key."""
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(_unb64(runner_public_key_b64)))
    nonce = os.urandom(12)
    ct = AESGCM(_derive(shared)).encrypt(nonce, secret.encode(), SEAL_INFO)
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"alg": "x25519-hkdf-aesgcm-v1", "epk": _b64(epk), "nonce": _b64(nonce), "ct": _b64(ct)}


def open_sealed(sealed: dict, private_key: X25519PrivateKey) -> str:
    """Runner side (mirrored in contrib/runner/junto-runner.py)."""
    shared = private_key.exchange(X25519PublicKey.from_public_bytes(_unb64(sealed["epk"])))
    return AESGCM(_derive(shared)).decrypt(_unb64(sealed["nonce"]), _unb64(sealed["ct"]), SEAL_INFO).decode()
