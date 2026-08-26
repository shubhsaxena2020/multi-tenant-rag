#!/usr/bin/env python3
"""Benchmark rerank candidate_k latency vs top-k agreement (FlashRank, CPU)."""
import os, time, statistics
os.environ.setdefault("USE_REAL_EMBEDDER", "1")
os.environ.setdefault("USE_REAL_RERANKER", "1")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
from app.embed import get_embedder
from app.rerank import get_reranker

emb = get_embedder()
rk = get_reranker()

# Simulate candidates: a relevant doc + distractors.
base = [
    ("The capital of France is Paris. The Eiffel Tower is in Paris.", 1.0),
    ("Cats are small domesticated mammals that meow and purr.", 0.62),
    ("Dogs are loyal canines that bark and fetch.", 0.60),
    ("The French Revolution began in 1789 in France.", 0.58),
    ("Berlin is the capital of Germany.", 0.55),
    ("The Louvre is a museum in Paris containing the Mona Lisa.", 0.54),
    ("Weather in Paris is mild with four seasons.", 0.50),
    ("Birds are avians with feathers and beaks.", 0.48),
    ("The Seine river flows through Paris.", 0.47),
    ("Croissants are a French pastry.", 0.45),
]
q = "What is the capital of France and what is it known for?"
# build a pool of 100 candidates (extend with lorem)
pool = []
for i in range(100):
    c, s = base[i % len(base)]
    pool.append({"text": c + f" [variant {i}]", "score": s - i*0.001})

for ck in (100, 50, 30, 20):
    cand = pool[:ck]
    times = []
    top1 = None
    for _ in range(5):
        t = time.perf_counter()
        out = rk.rerank(q, cand)
        times.append(time.perf_counter() - t)
    top1 = out[0]["text"][:40]
    print(f"candidate_k={ck:3d}  rerank_ms={statistics.mean(times)*1000:6.1f}  top1={top1!r}")
