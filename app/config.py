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
    # If false, use the deterministic hash embedder (tests / no-model environments)
    use_real_embedder: bool = False

    # Reranking
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cpu"
    use_real_reranker: bool = False

    # Tenant registry (SQLite v1; Postgres-ready)
    db_url: str = "sqlite:///./rag_tenants.db"

    # Auth
    api_key_header: str = "authorization"
    admin_api_key: str = ""  # if set, POST/GET/DELETE /tenants require Admin-Key header

    # Qdrant collection naming
    collection_prefix: str = "rag"

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
