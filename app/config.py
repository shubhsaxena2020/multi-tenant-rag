"""Service configuration via environment variables / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vector store
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    vector_size: int = 1024  # BGE-M3 dense dim
    distance: str = "Cosine"

    # Embedding
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cpu"  # cpu | cuda
    use_real_embedder: bool = False
    # Optional TEI (Text Embeddings Inference) endpoint — offloads the model to a GPU
    # node. When set, embedding is done over HTTP instead of in-process.
    embed_base_url: str = ""
    embed_api_key: str = ""

    # Reranking
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cpu"
    use_real_reranker: bool = False

    # Tenant registry (SQLite v1; Postgres-ready)
    db_url: str = "sqlite:///./rag_tenants.db"

    # Auth
    api_key_header: str = "authorization"
    admin_api_key: str = ""  # if set, POST/GET/DELETE /tenants require Admin-Key header

    # Encryption-at-rest (per-tenant AES-GCM envelope). MASTER_ENCRYPTION_KEY is the
    # only operator secret; 32 raw bytes (base64 or 64-hex). Unset = pass-through (dev).
    master_encryption_key: str = ""

    # Qdrant collection naming
    collection_prefix: str = "rag"

    # Redis (optional) — when set, rate limiting is shared across app replicas so a
    # VPS fleet enforces a single global per-tenant/per-IP budget. Empty = in-memory.
    redis_url: str = ""

    # Per-tenant quotas (fleet protection). 0 disables.
    tenant_chunk_quota: int = 5_000_000

    # Rate limiting (requests/min); 0 disables that dimension
    rate_per_tenant_per_min: int = 600
    rate_per_ip_per_min: int = 120
    rate_ingest_jobs_per_min: int = 60

    # Optional generation
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
