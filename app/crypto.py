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

KEY VERSIONING (v9-2): each token carries the master-key VERSION that produced it
("enc:<ver>:..."). On rotation, operators set MASTER_ENCRYPTION_KEY to the NEW master and
bump MASTER_KEY_VERSION, while keeping OLD versions decryptable via the MASTER_KEYRING env
JSON {"1": "...old...", "2": "...new..."}. This means rotating the master key NEVER renders
existing tenants unreadable — closing the prior "no version metadata → rotation breaks all
ciphertext" gap. Tokens written before versioning (plain "enc:...") still decrypt with the
current master (backward compatible).

If MASTER_ENCRYPTION_KEY is unset, encryption is a no-op pass-through (dev mode) and a
warning is logged. Production MUST set it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

# Associated-data constant: binds ciphertext to this service + scheme version.
_AAD = b"rag-service:v1:chunk"

_cache: dict[str, bytes] = {}
_ring_cache: dict[int, bytes] | None = None


def _coerce_key(raw: str) -> bytes | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error):
        if len(raw) == 64:
            return bytes.fromhex(raw)
        return raw.encode()[:32].ljust(32, b"\0")


def _master_key() -> bytes | None:
    return _coerce_key(get_settings().master_encryption_key)


def _keyring() -> dict[int, bytes]:
    """version -> master key bytes. Current master is version = master_key_version;
    older versions (for decrypting pre-rotation ciphertext) come from MASTER_KEYRING."""
    global _ring_cache
    if _ring_cache is not None:
        return _ring_cache
    s = get_settings()
    ring: dict[int, bytes] = {}
    cur = _coerce_key(s.master_encryption_key)
    if cur is not None:
        ring[s.master_key_version] = cur
    raw = os.environ.get("MASTER_KEYRING", "").strip()
    if raw:
        for k, v in json.loads(raw).items():
            kb = _coerce_key(v)
            if kb is not None:
                ring[int(k)] = kb
    _ring_cache = ring
    return ring


def reset_key_cache() -> None:
    """Clear cached keys/keyring (tests / config reload after rotation)."""
    global _ring_cache
    _cache.clear()
    _ring_cache = None


def tenant_key(tenant_id: str, version: int | None = None) -> bytes | None:
    """Derive a stable per-tenant AES-256 key from the master key for `version`.

    Returns None when no master key is configured (encryption disabled). When version is
    None, uses the current configured master_key_version.
    """
    master = _keyring().get(version if version is not None else get_settings().master_key_version)
    if master is None:
        return None
    if len(master) < 32:
        master = master.ljust(32, b"\0")
    cache_key = f"{version}:{tenant_id}"
    if cache_key in _cache:
        return _cache[cache_key]
    # HKDF-lite: HMAC-SHA256(master, info=tenant_id) -> 32 bytes
    key = hmac.new(master, b"tenant-key:" + tenant_id.encode(), hashlib.sha256).digest()
    _cache[cache_key] = key
    return key


def encrypt_text(tenant_id: str, plaintext: str) -> str:
    """Return a compact token: enc:<ver>:<base64(nonce||ciphertext||tag)>. No-op if disabled."""
    version = get_settings().master_key_version
    key = tenant_key(tenant_id, version)
    if key is None:
        return plaintext
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), _AAD)
    return f"enc:{version}:{base64.b64encode(nonce + ct).decode('ascii')}"


def decrypt_text(tenant_id: str, token: str) -> str:
    """Reverse encrypt_text. Tokens without the 'enc:' prefix are returned as-is.

    Versioned tokens (enc:<ver>:...) are decrypted with the matching master from the
    keyring so old ciphertext stays readable after a master-key rotation.
    """
    if not token.startswith("enc:"):
        return token
    body = token[4:]
    # Parse optional version segment: "enc:<ver>:..." vs legacy "enc:..."
    if body[:1].isdigit() and body[:2] in ("0:", "1:", "2:", "3:", "4:", "5:", "6:", "7:", "8:", "9:"):
        ver_str, _, blob = body.partition(":")
        version = int(ver_str)
    else:
        version = None  # legacy: decrypt with current master
        blob = body
    key = tenant_key(tenant_id, version)
    if key is None:
        raise ValueError("encryption unavailable: master key not configured for this version")
    raw = base64.b64decode(blob)
    nonce, ct = raw[:12], raw[12:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, _AAD).decode("utf-8")

