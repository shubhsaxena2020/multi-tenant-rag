#!/usr/bin/env bash
# Nightly Backup & DR job for the multi-tenant RAG service (v8.1.1 / BACKLOG DR item).
# Creates Qdrant collection snapshots + relational metadata backup and stages them for
# offsite copy. Designed to run from cron (see the cron line at the bottom of this file).
#
# Requirements:
#   - Runs with the project venv so `app.backup` imports cleanly.
#   - The Qdrant node must have QDRANT__STORAGE__SNAPSHOTS_PATH set to a real, server-readable
#     dir, and the app's qdrant_snapshot_dir config must point at the SAME path (see backup.py).
#   - Offsite copy: set OFFSITE_DIR to an rclone/gsutil/S3 mount; the script just `cp`s there.
set -euo pipefail

REPO=${RAG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
VENV=${VIRTUAL_ENV:-$REPO/.venv}/bin
OUT=${BACKUP_DIR:-$REPO/backups}
OFFSITE_DIR=${OFFSITE_DIR:-""}     # e.g. /mnt/offsite/rag-backups  (optional)
QDRANT_URL=${QDRANT_URL:-http://localhost:6333}
DAYS_KEEP=${DAYS_KEEP:-14}
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"

export PYTHONPATH="$REPO:${PYTHONPATH:-}"
export OUT

echo "[nightly-backup] $TS start"

# 1) Qdrant snapshots for every collection (uses app.backup helpers).
"$VENV/python3" - <<'PY'
import os, sys
os.environ.setdefault("USE_REAL_EMBEDDER", "0")
os.environ.setdefault("USE_REAL_RERANKER", "0")
from qdrant_client import QdrantClient
from app.config import get_settings
from app.backup import create_qdrant_snapshot
s = get_settings()
c = QdrantClient(url=s.qdrant_url)
cols = [c.name for c in c.get_collections().collections]
print(f"[nightly-backup] collections: {cols}")
for name in cols:
    snap = create_qdrant_snapshot(name)
    print(f"[nightly-backup] snapshot {name} -> {snap}")
PY

# 2) Relational metadata backup (SQLite file copy or pg_dump).
"$VENV/python3" - <<'PY' >> "$OUT/nightly_${TS}.log" 2>&1
import os, sys
os.environ.setdefault("USE_REAL_EMBEDDER", "0"); os.environ.setdefault("USE_REAL_RERANKER", "0")
from app.backup import backup_db
out_dir = os.environ.get("OUT", "backups")
out = backup_db(out_dir)
print("[nightly-backup] db backup ->", out)
PY

# 3) Offsite copy (optional).
if [ -n "$OFFSITE_DIR" ] && [ -d "$OFFSITE_DIR" ]; then
  cp -a "$OUT"/qdrant_snapshot_manifest.json "$OFFSITE_DIR"/ 2>/dev/null || true
  echo "[nightly-backup] offsite copy -> $OFFSITE_DIR"
fi

# 4) Local retention.
find "$OUT" -name "rag_metadata_*.db*" -mtime +"$DAYS_KEEP" -delete 2>/dev/null || true

echo "[nightly-backup] $TS done"
#
# --- Cron line (native host, runs 02:17 daily; adjust OFFSITE_DIR as needed) ---
# 17 2 * * *  RAG_HOME=/path/to/rag-service BACKUP_DIR=/path/to/backups OFFSITE_DIR=/mnt/offsite/rag-backups QDRANT_URL=http://localhost:6333 $RAG_HOME/deploy/native/nightly_backup.sh >> /path/to/backups/nightly.log 2>&1
