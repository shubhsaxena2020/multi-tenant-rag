"""Qdrant-backed per-tenant vector store — production scale.

Isolation: each tenant gets its own collection `{prefix}_{tenant_id}`. All
reads/writes are scoped to that collection (Truto 2026: isolation at the DB layer,
never app code). A tenant can never read another's vectors even under a bug.

Hybrid retrieval: each chunk is stored with a DENSE vector (for ANN recall) and a
SPARSE vector (lexical, BGE-M3 native). `query_points` runs dense; `query_points` with
sparse runs lexical; retrieval/__init__.py fuses them with Reciprocal Rank Fusion.

Resilience/scale:
- A single module-level QdrantClient (connection-pooled) — NOT one per call.
- Batch upsert (configurable batch size) to absorb large ingestion throughput.
- Encrypted `text` at rest via app.crypto (per-tenant AES-GCM).
- Quotas enforced at write time (chunk + vector caps) to protect a shared fleet.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from . import crypto
from .config import get_settings

_BATCH = 256
_client: QdrantClient | None = None
_client_lock = threading.Lock()

# Local/embedded Qdrant URLs (no server process needed). These are safe for CI and
# small single-node deployments; a real fleet still passes a normal `http(s)://host`
# or `qdrant://host` URL. Embedded mode runs the storage engine in-process.
_LOCAL_MEMORY = ":memory:"
_LOCAL_PREFIX = "qdrant-local://"


def collection_name(prefix: str, tenant_id: str) -> str:
    return f"{prefix}_{tenant_id}"


def _build_client() -> QdrantClient:
    s = get_settings()
    url = (s.qdrant_url or "").strip()
    if url == _LOCAL_MEMORY:
        # In-process, non-persistent Qdrant (ideal for tests/CI).
        return QdrantClient(location=":memory:")
    if url.startswith(_LOCAL_PREFIX):
        # On-disk local Qdrant: qdrant-local:///abs/path or qdrant-local://rel/path
        path = url[len(_LOCAL_PREFIX):] or "./qdrant_local"
        return QdrantClient(path=path)
    return QdrantClient(
        url=url,
        api_key=s.qdrant_api_key or None,
        timeout=10,
        # connection pooling handled internally by qdrant_client
    )


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


def reset_client() -> None:
    """Drop the cached Qdrant client (used by tests / config reload)."""
    global _client
    with _client_lock:
        _client = None


def ensure_collection(client: QdrantClient, tenant_id: str) -> str:
    s = get_settings()
    name = collection_name(s.collection_prefix, tenant_id)
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config=VectorParams(size=s.vector_size, distance=Distance.COSINE),
            sparse_vectors_config={"text": SparseVectorParams()},
        )
    # Payload indexes: required for fast filtering. The `acl` KEYWORD index enables
    # MatchAny over array payloads (document-level RBAC filter at query time). `doc_id`
    # and `tenant_id` indexes speed up scoped delete/filter ops.
    from qdrant_client.models import PayloadSchemaType

    for field, schema in (
        ("acl", PayloadSchemaType.KEYWORD),
        ("doc_id", PayloadSchemaType.KEYWORD),
        ("tenant_id", PayloadSchemaType.KEYWORD),
    ):
        try:
            client.create_payload_index(name, field, schema)
        except Exception:  # noqa: S110,BLE001 - index may already exist; ignore
            pass
    return name


def _sparse_vec(svec: dict[int, float]) -> SparseVector:
    return SparseVector(indices=list(svec.keys()), values=list(svec.values()))


def upsert_chunks(
    tenant_id: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    dense: list[list[float]],
    sparse: list[dict[int, float]],
    base_metadata: dict[str, Any],
    content_type: str,
) -> list[str]:
    """Store chunks with dense+sparse vectors and encrypted text. Returns chunk ids."""
    client = get_client()
    name = ensure_collection(client, tenant_id)
    points: list[PointStruct] = []
    chunk_ids: list[str] = []
    for i, (text, dvec, svec) in enumerate(zip(chunks, dense, sparse)):
        cid = str(uuid.uuid4())
        chunk_ids.append(cid)
        points.append(
            PointStruct(
                id=cid,
                vector={"": dvec, "text": _sparse_vec(svec)},
                payload={
                    "tenant_id": tenant_id,
                    "doc_id": doc_id,
                    "title": title,
                    "chunk_index": i,
                    "text": crypto.encrypt_text(tenant_id, text),
                    "content_type": content_type,
                    **base_metadata,
                },
            )
        )
        if len(points) >= _BATCH:
            client.upsert(name, points=points)
            points = []
    if points:
        client.upsert(name, points=points)
    return chunk_ids


def delete_document(tenant_id: str, doc_id: str) -> int:
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return 0
    client.delete(name, points_selector=Filter(
        must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
    ))
    return 1


def delete_tenant_collection(tenant_id: str) -> bool:
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return False
    client.delete_collection(name)
    return True


def search_dense(tenant_id: str, vector: list[float], limit: int,
                 score_threshold: float | None = None,
                 acl_filter: Filter | None = None) -> list[dict[str, Any]]:
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return []
    resp = client.query_points(
        name, query=vector, limit=limit, score_threshold=score_threshold,
        query_filter=acl_filter,
    )
    return [_hit(p) for p in resp.points]


def search_sparse(tenant_id: str, sparse: dict[int, float], limit: int,
                  score_threshold: float | None = None,
                  acl_filter: Filter | None = None) -> list[dict[str, Any]]:
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return []
    resp = client.query_points(
        name, query=_sparse_vec(sparse), using="text", limit=limit,
        score_threshold=score_threshold, query_filter=acl_filter,
    )
    return [_hit(p) for p in resp.points]


def _hit(p) -> dict[str, Any]:
    payload = p.payload or {}
    return {
        "chunk_id": str(p.id),
        "doc_id": payload.get("doc_id"),
        "title": payload.get("title"),
        "text": crypto.decrypt_text(payload.get("tenant_id", ""), payload.get("text", "")),
        "metadata": {k: v for k, v in payload.items() if k not in ("text", "tenant_id")},
        "score": float(p.score),
    }
