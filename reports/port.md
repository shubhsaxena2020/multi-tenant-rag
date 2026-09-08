# Port Report

## Copied
- `app/job_queue.py`
- `app/webhook.py`
- `tests/test_phase_j_job_queue.py`
- `tests/test_phase_g_webhooks.py`

## Wired
- `app/ingestion/runner.py` now routes `submit()` through `get_job_queue(...)` instead of using the local executor directly.
- `app/ingestion/runner.py` now fires the tenant ingestion webhook after job completion/failure.
- `app/ingestion/ssrf.py` now exposes `validate_url_for_egress(...)` for webhook config/dispatch validation.
- `app/config.py` now includes `webhook_allow_private`.
- `app/db.py` now stores nullable `lead_webhook_url` and `ingest_webhook_url` columns, adds additive migrations, and exposes async accessors for both.
- `app/models.py` now exposes the webhook fields on `TenantOut` / `TenantRow` so tenant reads preserve the configured URLs.
- `app/main.py` now exposes `PATCH /api/v1/{tenant}/webhooks`, `GET /api/v1/{tenant}/config`, `GET /api/v1/{tenant}/leads`, and a `/lead` alias for the handoff capture path.
- `tests/conftest.py` now enables `WEBHOOK_ALLOW_PRIVATE` for loopback webhook servers and disposes temp DB engines on teardown.
- `tests/test_migration_safety.py` now runs synchronously so the full suite does not depend on a missing async pytest plugin.

## Pytest
- before: `1 failed, 240 passed, 4 skipped, 2 xfailed, 550 warnings, 30 errors in 36.46s`
- after: `241 passed, 4 skipped, 2 xfailed, 550 warnings in 39.49s`
