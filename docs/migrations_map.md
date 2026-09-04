# Database Migrations Map

This document lists every database migration that has been applied or needs to be applied to the live schema. Each entry documents what column was added, to which table, and any relevant backfill or idempotency notes.

## Migration Entries (in deployment order)

| # | Migration | Table | Column | Type | Notes |
|---|-----------|-------|--------|------|-------|
| 1 | v10.8: key tier | `tenant_keys` | `kind` | VARCHAR(16) | Default "secret". v10.8: optional `expires_at` column added next. |
| 2 | v10.8: key expiry | `tenant_keys` | `expires_at` | TIMESTAMP WITH TIME ZONE | NULL = never expires. Expired keys rejected at auth time. Referenced by `add_api_key` and `require_secret_key`. |
| 3 | per-tenant chunk quota | `tenants` | `chunk_quota` | INTEGER | NULL = use global default. |
| 4 | per-tenant widget branding | `tenants` | `branding` | TEXT | JSON blob {logo_url, header_title, accent, ...}. Default '{}'. |
| 5 | per-tenant system prompt | `tenants` | `system_prompt` | TEXT | Default "". PHASE D: per-tenant custom system persona. |
| 6 | per-tenant allowed groups | `tenants` | `allowed_groups` | TEXT | JSON list, default '[*"*"]'. |
| 7 | per-tenant rate limit RPM | `tenants` | `rate_limit_rpm` | INTEGER | NULL = use global default. |
| 8 | per-tenant ingest rate limit RPM | `tenants` | `ingest_rate_limit_rpm` | INTEGER | NULL = use global default. |

## Migration Discipline

- **All migrations are additive only** (ADD COLUMN, no DROP, no ALTER that could lose data).
- Each migration is **idempotent**: executed via `CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` pattern.
- New model columns must be migrated in `app/db.py:init_db()` under the `# Idempotent additive column migrations` section, or they will not propagate to production.
- The `_run_add_column_migrations` function uses dialect-aware introspection (PRAGMA for SQLite, information_schema for Postgres) to avoid false positives/negatives.

## Missing / Unmigrated Columns

If a model column exists in `app/models.py` / `app/db.py` but is NOT listed in the migrations table above, it has not been deployed to production. Create a migration entry and add it to the `migrations` list in `init_db()`.

## Adding a New Migration

1. Add a `(table, column, type)` tuple to the `migrations` list in `app/db.py:init_db()`.
2. Add a row to this map documenting the column and any backfill notes.
3. Run `init_db()` against a scratch database to verify the column is added without errors.