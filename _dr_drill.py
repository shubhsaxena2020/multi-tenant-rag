"""Real DR drill against the LIVE Qdrant (localhost:6333).

Proves the Backup & DR recovery path works end-to-end WITHOUT touching the production
`rag` collection: uses a throwaway collection, snapshots it, recovers it, and asserts
point counts survive. Also exercises the app's backup.py helpers so the code path is real.

Run: QDRANT_URL=http://localhost:6333 .venv/bin/python _dr_drill.py
"""
import os
import sys
import uuid

# Avoid triggering real embedding/rerank heavy paths.
os.environ.setdefault("USE_REAL_EMBEDDER", "0")
os.environ.setdefault("USE_REAL_RERANKER", "0")

from qdrant_client import QdrantClient  # type: ignore
from app.backup import create_qdrant_snapshot, recover_qdrant_snapshot  # noqa: E402
from app.config import get_settings  # noqa: E402

TMP = f"dr_drill_{uuid.uuid4().hex[:8]}"
DIM = 4


def main() -> int:
    s = get_settings()
    client = QdrantClient(url=s.qdrant_url)

    # 1) create throwaway collection + points
    client.recreate_collection(
        collection_name=TMP,
        vectors_config={"size": DIM, "distance": "Cosine"},
    )
    pts = [{"id": i, "vector": [float(i)] * DIM, "payload": {"k": f"v{i}"}} for i in range(5)]
    client.upsert(collection_name=TMP, points=pts)
    before = client.count(collection_name=TMP).count
    print(f"[dr] temp collection {TMP}: {before} points")

    # 2) snapshot via app helper
    snap = create_qdrant_snapshot(TMP)
    print(f"[dr] snapshot created: {snap}")

    # 3) delete points, then recover from snapshot
    client.delete(collection_name=TMP, points_selector=[p["id"] for p in pts])
    missing = client.count(collection_name=TMP).count
    print(f"[dr] after delete: {missing} points (simulating data loss)")

    recover_qdrant_snapshot(snap, collection=TMP)
    print(f"[dr] recover_snapshot invoked for {TMP}")

    # 4) verify recover (may need a moment for the server to ingest the snapshot)
    import time
    time.sleep(3)
    after = client.count(collection_name=TMP).count
    print(f"[dr] after recover: {after} points")

    # cleanup
    client.delete_collection(TMP)
    ok = before == 5 and missing == 0 and after == 5
    print("DR_DRILL_OK" if ok else "DR_DRILL_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
