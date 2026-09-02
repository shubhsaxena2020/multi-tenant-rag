import asyncio
import pytest
from app.audit import append_audit, verify_chain
from app.db import get_session_maker, create_tenant, get_tenant
from app.vector_store import upsert_chunks, get_document_chunks, reset_client
from app.ratelimit import _MemoryLimiter
import app.ingestion.ssrf as ssrf


@pytest.mark.asyncio
async def test_audit_concurrent_hash_chain():
    """Verify that concurrent calls to append_audit serialize cleanly and keep the hash chain intact."""
    tasks = [
        append_audit("CONCURRENT_TEST", f"actor_{i}", f"target_{i}", {"seq": i})
        for i in range(15)
    ]
    await asyncio.gather(*tasks)
    report = await verify_chain()
    assert report["ok"] is True, f"Audit chain broke: {report}"


@pytest.mark.asyncio
async def test_db_get_session_with_active_session():
    """Verify that passing an active AsyncSession to db functions does not crash with TypeError."""
    maker = get_session_maker()
    async with maker() as session:
        t = await create_tenant(
            name="SessionTester",
            tenant_id="sess_tenant_1",
            api_key="rk_sess_test_12345",
            plan="free",
            session=session,
        )
        assert t["tenant_id"] == "sess_tenant_1"
        fetched = await get_tenant("sess_tenant_1", session=session)
        assert fetched is not None
        assert fetched["name"] == "SessionTester"


def test_vector_store_get_document_chunks_preserves_order():
    """Verify that get_document_chunks returns chunks in sequential chunk_index order."""
    reset_client()
    tenant_id = "test_doc_seq_tenant"
    doc_id = "doc_sequential_123"
    chunks = ["First paragraph", "Second paragraph", "Third paragraph", "Fourth paragraph"]
    dense = [[0.1] * 1024 for _ in chunks]
    sparse = [{1: 0.5} for _ in chunks]

    upsert_chunks(
        tenant_id=tenant_id,
        doc_id=doc_id,
        title="Ordered Document",
        chunks=chunks,
        dense=dense,
        sparse=sparse,
        base_metadata={"source": "test"},
        content_type="text",
    )

    retrieved = get_document_chunks(tenant_id, doc_id)
    assert len(retrieved) == 4
    for i, c in enumerate(retrieved):
        assert c["text"] == chunks[i]
        assert c["metadata"]["chunk_index"] == i


def test_memory_limiter_prunes_empty_buckets():
    """Verify that _MemoryLimiter prunes dead empty keys when capacity threshold is reached."""
    limiter = _MemoryLimiter()
    # Add dummy empty keys
    for i in range(6000):
        limiter._buckets[f"dead_key_{i}"] = []

    # Hit a new key; it should trigger pruning of empty keys
    allowed, retry = limiter.hit("live_key_1", limit=10, window_min=1)
    assert allowed is True
    # Verify pruned down
    assert len(limiter._buckets) <= 1001


def test_crypto_multidigit_key_version_roundtrip(monkeypatch):
    """Verify that multi-digit master key versions (>=10) encrypt and decrypt correctly."""
    import os
    import app.crypto as crypto
    from app.config import get_settings

    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("MASTER_KEY_VERSION", "15")
    get_settings.cache_clear()
    crypto.reset_key_cache()

    plaintext = "Sensitive multi-digit versioned tenant information."
    tenant_id = "tenant_v15"

    token = crypto.encrypt_text(tenant_id, plaintext)
    assert token.startswith("enc:15:")

    decrypted = crypto.decrypt_text(tenant_id, token)
    assert decrypted == plaintext


def test_crypto_tampered_ciphertext_fails_gracefully(monkeypatch):
    """Verify that tampered ciphertext raises ValueError rather than leaking unhandled exceptions."""
    import app.crypto as crypto
    from app.config import get_settings

    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("MASTER_KEY_VERSION", "1")
    get_settings.cache_clear()
    crypto.reset_key_cache()

    token = crypto.encrypt_text("tenant_tamper", "Clean text")
    # Tamper with the base64 payload
    tampered = token[:-4] + "AAAA"

    with pytest.raises(ValueError, match="decryption failed"):
        crypto.decrypt_text("tenant_tamper", tampered)
