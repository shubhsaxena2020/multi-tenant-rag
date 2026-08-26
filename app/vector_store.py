"""Qdrant-backed per-tenant vector store.

Isolation: each tenant gets its own collection named
`{prefix}_{tenant_id}`. All reads/writes are scoped to that collection, so a tenant
can never read another's vectors (Truto 2026: isolation at the DB layer, not app code).
Qdrant runs in Docker on the VPS; the client only needs qdrant_url.
"""
from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from .config import get_settings


def collection_name(prefix: str, tenant_id: str) -> str:
    return f"{prefix}_{tenant_id}"


def get_client() -> QdrantClient:
    s = get_settings()
    return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None, timeout=10)


def ensure_collection(client: QdrantClient, tenant_id: str) -> str:
    s = get_settings()
    name = collection_name(s.collection_prefix, tenant_id)
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config=VectorParams(size=s.vector_size, distance=Distance.COSINE),
        )
    return name


def upsert_chunks(
    tenant_id: str,
    doc_id: str,
    title: str,
    chunks: list[str],
    vectors: list[list[float]],
    base_metadata: dict[str, Any],
    content_type: str,
) -> list[str]:
    client = get_client()
    name = ensure_collection(client, tenant_id)
    points = []
    chunk_ids: list[str] = []
    for i, (text, vec) in enumerate(zip(chunks, vectors)):
        cid = str(uuid.uuid4())
        chunk_ids.append(cid)
        points.append(
            PointStruct(
                id=cid,
                vector=vec,
                payload={
                    "tenant_id": tenant_id,
                    "doc_id": doc_id,
                    "title": title,
                    "chunk_index": i,
                    "text": text,
                    "content_type": content_type,
                    **base_metadata,
                },
            )
        )
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
    """Drop a tenant's entire collection (offboarding / hard isolation removal).

    Returns True if a collection existed and was removed. Safe to call when the
    tenant has no data yet."""
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return False
    client.delete_collection(name)
    return True


def search(
    tenant_id: str,
    vector: list[float],
    limit: int,
    score_threshold: float | None = None,
) -> list[dict[str, Any]]:
    client = get_client()
    name = collection_name(get_settings().collection_prefix, tenant_id)
    if not client.collection_exists(name):
        return []
    resp = client.query_points(
        name, query=vector, limit=limit, score_threshold=score_threshold
    )
    return [
        {
            "chunk_id": str(p.id),
            "doc_id": p.payload.get("doc_id"),
            "title": p.payload.get("title"),
            "text": p.payload.get("text"),
            "metadata": {k: v for k, v in (p.payload or {}).items()
                         if k not in ("text", "tenant_id")},
            "score": float(p.score),
        }
        for p in resp.points
    ]
