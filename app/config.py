"""Service configuration via environment variables / .env."""
from __future__ import annotations
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@lru_cache
def get_settings() -> Settings:
    return Settings()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    audit_sample_rate: float = 1.0

    # Vector store
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    vector_size: int = 1024  # BGE-M3 dense dim
    distance: str = "Cosine"

    # Embedding
    # Default dense model is multilingual-e5-large (1024-dim) — works with the lightweight
    # fastembed provider and is multilingual (our tenants are independent client sites,
    # often non-English). For full BGE-M3 set EMBED_MODEL=BAAI/bge-m3 and
    # EMBED_PROVIDER=sentence_transformers (heavier, GPU-friendly).
    embed_model: str = "intfloat/multilingual-e5-large"
    embed_device: str = "cpu"  # cpu | cuda
    use_real_embedder: bool = False
    # Provider for real embedding: "fastembed" (default, lightweight ONNX) or
    # "sentence_transformers" (heavier, full BGE-M3). fastembed is preferred for CPU fleets.
    embed_provider: str = "fastembed"
    # Sparse (lexical) model for hybrid retrieval — paired with the dense model above.
    embed_sparse_model: str = "prithivida/Splade_PP_en_v1"
    # Prefix style for the dense model. multilingual-e5-large REQUIRES `query:`/`passage:` prefixes (v8 #4 retrieval-quality bug). Set to "none" for models that must not be prefixed (BGE-M3, most others). "e5" adds the required prefixes; auto-detect would be fragile, so we make it an explicit, audited knob.
    embed_prefix_style: str = "e5"
    # Optional TEI (Text Embeddings Inference) endpoint — offloads the model to a GPU node. When set, embedding is done over HTTP instead of in-process.
    embed_base_url: str = ""
    embed_api_key: str = ""

    # Reranking
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = "cpu"
    use_real_reranker: bool = True
    # Provider for real reranking: "flashrank" (default, lightweight CPU Cross-Encoder, no torch) or "sentence_transformers" (full BGE-Reranker-v2-m3, heavier / GPU).
    rerank_provider: str = "flashrank"

    # Tenant registry (SQLite v1; Postgres-ready)
    db_url: str = "sqlite:///./rag_tenants.db"

    # SLO (v9-5) config
    slo_availability_target: float = 0.95
    slo_latency_target_s: float = 1.0
    slo_latency_p95_s: float = 0.5
    slo_availability: float = 0.99

    # Auth
    api_key_header: str = "authorization"
    admin_api_key: str = ""  # if set, POST/GET/DELETE /tenants require Admin-Key header

    # Encryption-at-rest (per-tenant AES-GCM envelope). MASTER_ENCRYPTION_KEY is the
    # Encryption: MASTER_ENCRYPTION_KEY is the only operator secret (32 raw bytes,
    # base64 or 64-hex). master_key_version + MASTER_KEYRING (env JSON {"v":"key"})
    # enable non-breaking rotation (v9-2 KMS versioning): old ciphertext stays readable.
    master_encryption_key: str = ""
    master_key_version: int = 1

    # Qdrant client timeout (seconds) — bounds every vector call so a sick Qdrant
    # hangs a single request instead of the whole replica (v9-1 resilience).
    qdrant_timeout: float = 10.0
    # Directory on the Qdrant node where snapshots are written/read (same-node recovery).
    # Must match QDRANT__STORAGE__SNAPSHOTS_PATH in docker-compose (container path).
    qdrant_snapshot_dir: str = "/qdrant/storage/snapshots"

    # Resilience: retry + circuit breaker (v9-1 graceful degradation).
    retry_max_attempts: int = 3
    retry_base_delay_s: float = 0.2
    retry_max_delay_s: float = 2.0
    # Circuit breaker: after this many consecutive failures on a dependency, the
    # breaker trips OPEN and fails fast for cb_cooldown_s, then half-opens to probe.
    cb_failure_threshold: int = 5
    cb_cooldown_s: float = 30.0

    # Qdrant collection naming
    collection_prefix: str = "rag"

    # Redis (optional) — when set, rate limiting is shared across app replicas so a
    # VPS fleet enforces a single global per-tenant/per-IP budget. Empty = in-memory.
    redis_url: str = "redis://localhost:6379/0"

    # Job queue backend configuration (v9-5)
    # Controls the backend used for job queueing across replicas.
    # Options: "inline" (default, single-replica), "redis", "rq", "celery"
    # When using external backends, configure the corresponding connection URL.
    job_queue_backend: str = "redis"
    job_queue_connection: str = ""  # falls back to redis_url if empty

    # Trusted reverse-proxy CIDRs. X-Forwarded-For is ONLY trusted when the immediate
    # connection comes from one of these (otherwise a client can spoof it and bypass IP
    # throttling). Empty = never trust XFF (use the real socket peer). Example:
    # TRUSTED_PROXIES=10.0.0.0/8,172.17.0.0/16
    trusted_proxies: str = ""

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

    # Conversation-aware RAG (live chat widget support)
    retrieval_confidence_threshold: float = 0.15
    rewrite_enabled: bool = True
    injection_guard_enabled: bool = True

    # Embeddable widget (v9-3): origins allowed to embed the chat widget via
    # <iframe>. Enforced with CSP frame-ancestors. Empty = no embedding allowed.
    allowed_embed_origins: list[str] = []