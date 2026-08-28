"""Live cross-tenant isolation re-verification (v8 #2 highest-risk migration).

The v8 migration moved from collection-per-tenant (Silo) to ONE shared collection
partitioned by payload `tenant_id`. Before trusting it, we re-run adversarial isolation
against the REAL Qdrant at QDRANT_URL (not the in-memory test double) using the actual
repo code (app.vector_store + app.retrieval). We:
  1. ingest a secret doc into TENANT_A and a different secret into TENANT_B,
  2. query as TENANT_A for B's secret -> must return NOTHING from B,
  3. confirm A sees only A's content and vice-versa,
  4. attempt a delete scoped to A that must NOT touch B's points.

Run: QDRANT_URL=http://localhost:6333 .venv/bin/python _verify_live_isolation.py
"""
from __future__ import annotations

import os
import uuid

from app.vector_store import (
    ensure_collection,
    upsert_chunks,
    search_hybrid,
    delete_document,
    get_client,
)
from app.embed import get_embedder
from app.retrieval import retrieve

TA = f"live_verify_A_{uuid.uuid4().hex[:8]}"
TB = f"live_verify_B_{uuid.uuid4().hex[:8]}"


def _ingest(tenant, title, text):
    emb = get_embedder()
    res = emb.embed_passages([text])
    upsert_chunks(
        tenant, f"doc_{tenant}", title, [text],
        [res[0].dense], [res[0].sparse], {}, "text/plain",
    )


def main():
    # Use the deterministic embedder so the hybrid pipeline is exercised without GB downloads;
    # isolation is orthogonal to embedding quality. Override to real if desired.
    os.environ.setdefault("USE_REAL_EMBEDDER", "0")
    client = get_client()
    ensure_collection(client)

    secret_a = "TENANT A launch code is ALPHA-SECRET-12345"
    secret_b = "TENANT B launch code is BRAVO-SECRET-67890"
    _ingest(TA, "A doc", secret_a)
    _ingest(TB, "B doc", secret_b)

    # Query as A for B's secret phrase
    hits_a = retrieve(TA, "What is the launch code?", top_k=10)
    a_texts = " ".join(h.get("text", "") for h in hits_a)
    leak = TB not in a_texts and "BRAVO" not in a_texts
    saw_own = "ALPHA" in a_texts

    # Query as B for A's secret
    hits_b = retrieve(TB, "What is the launch code?", top_k=10)
    b_texts = " ".join(h.get("text", "") for h in hits_b)
    leak_b = TA not in b_texts and "ALPHA" not in b_texts
    saw_own_b = "BRAVO" in b_texts

    # Delete A's doc; ensure B's points survive (count check)
    before_b = len(search_hybrid(TB, get_embedder().embed_query("launch code").dense, get_embedder().embed_query("launch code").sparse, limit=50))
    delete_document(TA, f"doc_{TA}")
    after_b = len(search_hybrid(TB, get_embedder().embed_query("launch code").dense, get_embedder().embed_query("launch code").sparse, limit=50))

    ok = leak and saw_own and leak_b and saw_own_b and before_b == after_b and after_b >= 1
    print(f"TENANT_A saw own secret : {saw_own}")
    print(f"TENANT_A leaked B's secret: {not leak}  (want False)")
    print(f"TENANT_B saw own secret : {saw_own_b}")
    print(f"TENANT_B leaked A's secret: {not leak_b}  (want False)")
    print(f"B points before/after A-delete: {before_b}/{after_b}  (want equal, >=1)")
    print("RESULT:", "ISOLATION_OK" if ok else "ISOLATION_BROKEN",
          "| exit=0" if ok else "| exit=1")
    # cleanup B
    delete_document(TB, f"doc_{TB}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
