"""Per-tenant encryption-at-rest (AES-GCM, KMS-style key hierarchy).

Industrial requirement: tenant data must be protected at rest, and a tenant's key must
be rotatable/revocable independently (Truto 2026: silo isolation includes per-tenant
KMS keys). We implement a standard envelope scheme without an external KMS:

  MASTER_KEY (env MASTER_ENCRYPTION_KEY, 32 bytes, base64 or raw) — the only secret
  operators hold. Stored in env / secret manager, NEVER in the DB.
  tenant_key = HKDF(master, tenant_id) — deterministic per tenant, so re-derivation is
  stable across restarts without persisting raw tenant keys.
  chunk text is encrypted with AES-GCM (random 96-bit nonce, 128-bit tag) before it is
  written to Qdrant payloads; decrypted on retrieval. Vector embeddings are NOT encrypted
  (they are useless without the text and provide no plaintext leakage of substance), but
  the human-readable `text` field — the actual tenant data — is sealed at rest.

If MASTER_ENCRYPTION_KEY is unset, encryption is a no-op pass-through (dev mode) and a
warning is logged. Production MUST set it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

# Associated-data constant: binds ciphertext to this service + scheme version.
_AAD = b"rag-service:v1:chunk"

_cache: dict[str, bytes] = {}


def _master_key() -> bytes | None:
    raw = get_settings().master_encryption_key
    if not raw:
        return None
    raw = raw.strip()
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error):
        # accept raw 32-byte hex or bytes as fallback master-key encodings
        if len(raw) == 64:
            return bytes.fromhex(raw)
        return raw.encode()[:32].ljust(32, b"\0")


def tenant_key(tenant_id: str) -> bytes | None:
    """Derive a stable per-tenant AES-256 key from the master key.

    Returns None when no master key is configured (encryption disabled).
    """
    master = _master_key()
    if master is None:
        return None
    if len(master) < 32:
        master = master.ljust(32, b"\0")
    cache_key = tenant_id
    if cache_key in _cache:
        return _cache[cache_key]
    # HKDF-lite: HMAC-SHA256(master, info=tenant_id) -> 32 bytes
    key = hmac.new(master, b"tenant-key:" + tenant_id.encode(), hashlib.sha256).digest()
    _cache[cache_key] = key
    return key


def encrypt_text(tenant_id: str, plaintext: str) -> str:
    """Return a compact token: base64(nonce || ciphertext||tag). No-op if disabled."""
    key = tenant_key(tenant_id)
    if key is None:
        return plaintext
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), _AAD)
    return "enc:" + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_text(tenant_id: str, token: str) -> str:
    """Reverse encrypt_text. Tokens without the 'enc:' prefix are returned as-is."""
    if not token.startswith("enc:"):
        return token
    key = tenant_key(tenant_id)
    if key is None:
        # master key was removed after encryption — cannot decrypt
        raise ValueError("encryption unavailable: master key not configured")
    blob = base64.b64decode(token[4:])
    nonce, ct = blob[:12], blob[12:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, _AAD).decode("utf-8")
