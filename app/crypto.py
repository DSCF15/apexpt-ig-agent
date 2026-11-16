"""Cifra AES-256-GCM para segredos guardados na BD (tokens)."""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


def _aes(key_b64: str) -> AESGCM:
    if not key_b64:
        raise RuntimeError("ENCRYPTION_KEY não definida (corre scripts/gen_key.py)")
    return AESGCM(base64.urlsafe_b64decode(key_b64))


def encrypt(plaintext: str, key_b64: str) -> str:
    nonce = os.urandom(12)
    ct = _aes(key_b64).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str, key_b64: str) -> str:
    raw = base64.urlsafe_b64decode(token)
    return _aes(key_b64).decrypt(raw[:12], raw[12:], None).decode()
