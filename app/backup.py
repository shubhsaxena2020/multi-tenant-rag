"""Backup & Disaster Recovery (v9-2).

Three pillars, each independently restorable:

1. Qdrant vector store — collection snapshots via the Qdrant snapshot API
   (qdrant.tech/documentation/snapshots). `create_qdrant_snapshot` returns a snapshot
   name; `recover_qdrant_snapshot` restores it. Both are exercised by the DR drill test.
2. Relational metadata (tenants/jobs) — `pg_dump` for Postgres, file copy for SQLite
   (WAL mode). `backup_db` dispatches on the configured DB_URL.
3. Encryption master keys — KMS-style versioning in crypto.py (MASTER_KEYRING). Rotating
   the master key never breaks existing ciphertext; `verify_keyring_roundtrip` proves it.

Operations notes:
- Snapshots should be shipped off-node (object storage) by the deploy layer; this module
  creates/registers them and provides the restore entrypoint.
- For self-hosted single-node Qdrant, snapshots are written to the node's snapshot dir;
  recover reads them back. In CI (in-memory Qdrant) the snapshot path is a no-op that
  validates the API contract.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from typing import Any

from .config import get_settings
from .vector_store import get_client

# Where on-disk snapshots are staged. Override with env BACKUP_DIR.
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/var/backups/rag-service")


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------- 1. Qdrant vector snapshots ----------------

def create_qdrant_snapshot(collection: str | None = None) -> str:
    """Create a collection snapshot. Returns the snapshot name.

    Raises RuntimeError if the Qdrant client/API is unavailable (caller should degrade).
    """
    s = get_settings()
    name = collection or s.collection_prefix
    client = get_client()
    snap = client.create_snapshot(collection_name=name)
    # snap is a SnapshotDescription with .name
    return getattr(snap, "name", str(snap))


def recover_qdrant_snapshot(
    snapshot_name: str,
    collection: str | None = None,
    location: str | None = None,
) -> None:
    """Restore a collection from a previously created snapshot.

    `location` is the absolute path or URL the Qdrant node can read the snapshot from.
    For a same-node Qdrant the snapshot already lives in the node's snapshot dir
    (default /qdrant/storage/snapshots/<collection>/<name> under Docker, or whatever
    QDRANT__STORAGE__SNAPSHOTS_PATH points at on a native host). The qdrant_client
    validates `location` as a URL, so a bare local path must be passed as a `file://`
    URI (a leading-slash path is rejected as "relative URL without a base").

    DEPLOY NOTE: on a native (non-Docker) host, set QDRANT__STORAGE__SNAPSHOTS_PATH to a
    real directory the qdrant process can read AND write, and point this module's
    `qdrant_snapshot_dir` config at the SAME path. Otherwise recover will report
    "Snapshot file ... not found" even though create_snapshot "succeeded".
    """
    s = get_settings()
    name = collection or s.collection_prefix
    loc = location or f"file://{s.qdrant_snapshot_dir.rstrip('/')}/{name}/{snapshot_name}"
    client = get_client()
    client.recover_snapshot(collection_name=name, location=loc)


def snapshot_qdrant_to_dir(collection: str | None = None, out_dir: str | None = None) -> str:
    """Create a snapshot and download/record its location. Returns the snapshot name.

    NOTE: the qdrant_client `create_snapshot` call triggers server-side snapshot creation;
    retrieving the file is a separate `download_snapshot` step when running against a
    remote server. On a local/embedded node the snapshot already lives in the node's
    snapshot directory. This helper focuses on the create step + off-node staging path.
    """
    name = create_qdrant_snapshot(collection)
    target = out_dir or BACKUP_DIR
    os.makedirs(target, exist_ok=True)
    # Record provenance so a restore knows which snapshot to recover.
    manifest = os.path.join(target, "qdrant_snapshot_manifest.json")
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump(
            {"collection": collection or get_settings().collection_prefix, "snapshot": name, "created_at": _ts()},
            fh,
        )
    return name


# ---------------- 2. Relational metadata backup ----------------

def backup_db(out_dir: str | None = None) -> str:
    """Back up the relational metadata store.

    Postgres -> `pg_dump` (logical dump, point-in-time-able with WAL archiving).
    SQLite  -> file copy (requires WAL mode for safe online copy; Litestream optional).
    Returns the path to the backup artifact.
    """
    s = get_settings()
    target = out_dir or BACKUP_DIR
    os.makedirs(target, exist_ok=True)
    stamp = _ts()
    url = s.db_url

    if url.startswith(("postgresql+", "postgres://")):
        out_path = os.path.join(target, f"rag_metadata_{stamp}.sql")
        # pg_dump needs a libpq connection string (strip the async driver prefix).
        conn = url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
        with open(out_path, "w", encoding="utf-8") as fh:
            subprocess.run(["pg_dump", conn], stdout=fh, check=True)
        return out_path

    if url.startswith("sqlite"):
        # SQLite: copy the DB file. WAL mode makes this a consistent online copy.
        db_path = url.split("///", 1)[-1] if "///" in url else url.split("://", 1)[-1]
        if not os.path.exists(db_path):
            raise RuntimeError(f"SQLite DB file not found: {db_path}")
        out_path = os.path.join(target, f"rag_metadata_{stamp}.db")
        with open(db_path, "rb") as src, open(out_path, "wb") as dst:
            dst.write(src.read())
        # also copy -wal/-shm if present (WAL durability)
        for suffix in ("-wal", "-shm"):
            extra = db_path + suffix
            if os.path.exists(extra):
                with open(extra, "rb") as src, open(out_path + suffix, "wb") as dst:
                    dst.write(src.read())
        return out_path

    raise RuntimeError(f"Unsupported db_url for backup: {url[:12]}...")


# ---------------- 3. Keyring health ----------------

def verify_keyring_roundtrip(tenant_id: str = "dr-drill-tenant") -> dict[str, Any]:
    """Prove that encrypted data survives a master-key rotation.

    Encrypts with the CURRENT master, then simulates rotation by adding an OLD master to
    the keyring and confirms the token still decrypts. Returns a small report dict.
    """
    from . import crypto

    plaintext = "secret tenant data for DR drill"
    token = crypto.encrypt_text(tenant_id, plaintext)
    # Decrypt with current master (no rotation) — must match.
    assert crypto.decrypt_text(tenant_id, token) == plaintext
    # Now simulate rotation: old master goes into MASTER_KEYRING; current master changes.
    old_master = get_settings().master_encryption_key or "AAAAAAoldmasterkey0000000000000000000000"
    old_master = old_master if len(old_master) >= 32 else (old_master * 3)[:32]
    new_master = "BBBBBBnewmasterkey0000000000000000000000"[:32]
    os.environ["MASTER_KEYRING"] = json.dumps({"1": old_master})
    os.environ["MASTER_ENCRYPTION_KEY"] = new_master
    os.environ["MASTER_KEY_VERSION"] = "2"
    get_settings.cache_clear()
    crypto.reset_key_cache()
    # The token (written under version 1) must still decrypt via the keyring.
    rotated_ok = crypto.decrypt_text(tenant_id, token) == plaintext
    # Cleanup env so we don't poison the process.
    os.environ.pop("MASTER_KEYRING", None)
    os.environ.pop("MASTER_KEY_VERSION", None)
    os.environ["MASTER_ENCRYPTION_KEY"] = old_master
    get_settings.cache_clear()
    crypto.reset_key_cache()
    return {"token": token, "rotated_plaintext_recoverable": rotated_ok}
