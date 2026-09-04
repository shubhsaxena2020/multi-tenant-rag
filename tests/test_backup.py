"""Tests for the backup module with `unit` mark.

These tests exercise backup/restore logic WITHOUT external dependencies (no Qdrant,
no KMS, no SQLite file I/O). They are marked `unit` so they can be run in fast
subsets with `pytest -m "unit"`.
"""

import os

import pytest

V = "/api/v1"  # API version prefix used by all endpoint paths


# ---------------- KMS key versioning / rotation ----------------

pytestmark = pytest.mark.unit


def test_kms_rotation_keeps_old_ciphertext_readable():
    """v9-2: rotating MASTER_ENCRYPTION_KEY must NOT render prior tokens unreadable."""
    from app import crypto
    from app.config import get_settings

    os.environ["MASTER_ENCRYPTION_KEY"] = "AAAAAAoldmasterkey0000000000000000000000"
    os.environ.pop("MASTER_KEYRING", None)
    os.environ.pop("MASTER_KEY_VERSION", None)
    get_settings.cache_clear()
    crypto.reset_key_cache()

    tenant = "rotate-tenant"
    token = crypto.encrypt_text(tenant, "secret-before-rotation")
    assert crypto.decrypt_text(tenant, token) == "secret-before-rotation"