# Security & production hardening

`rag-service` is safe to run as a template out of the box, but a real deployment
that serves untrusted traffic must do the following. `setup.sh` leaves the
defaults in the "safe for a single trusted operator" position.

## 1. Document-level RBAC — `allowed_groups`

A new tenant is created with `allowed_groups = ["__public__"]`. Under that
default, a query that does **not** pass an `acl` parameter only sees documents
tagged public — documents tagged for a named group are hidden. This is the safe
default.

- To use per-group document isolation inside a tenant, provision the group
  labels explicitly and tag documents with a matching `acl`:

  ```
  curl -X PATCH localhost:8000/api/v1/tenants/$TID \
    -H "Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
    -d '{"allowed_groups":["support","billing"]}'
  ```

- `allowed_groups = ["*"]` restores the legacy "tenant sees everything it is
  entitled to, no intra-tenant separation" behavior. Only use it for a
  single-user tenant.

## 2. Transport — always run behind TLS

The app speaks plain HTTP on its container port. Never expose it directly.
`./setup.sh --tls your.domain` writes a `Caddyfile` that terminates TLS and
reverse-proxies to the app. Any TLS-terminating proxy (Caddy, nginx, a cloud LB)
is fine.

## 3. Database — SQLite is single-writer

The default `DB_URL` is SQLite, which serializes writes and will bottleneck or
error under concurrent ingestion from multiple workers. For anything beyond one
worker use Postgres: `./setup.sh --postgres` (brings up a `postgres` service and
points `DB_URL` at it). `asyncpg` is already a dependency.

## 4. Encryption key

`MASTER_ENCRYPTION_KEY` must be a real 32-byte secret (`openssl rand -base64 32`).
`setup.sh` generates one for a fresh `.env`. Rotating it makes every existing
tenant API key undecryptable — tenants must be re-issued keys.

## 5. Rate limiting

Two independent limiters apply: per-tenant (`RATE_PER_TENANT_PER_MIN`, default
600) and per-IP (`RATE_PER_IP_PER_MIN`, default 120). A single-source client hits
the per-IP ceiling first. Put real per-replica sharing behind Redis
(`./setup.sh --with-redis`).

## Production checklist

- Behind TLS (`--tls`) — no direct HTTP exposure
- `--postgres` if more than one ingestion worker
- `MASTER_ENCRYPTION_KEY` is a fresh 32-byte value, stored in a secret manager
- `ADMIN_API_KEY` rotated from the generated one, not in source control
- Per-tenant `allowed_groups` provisioned if you need intra-tenant doc isolation
- `python -m scripts.migrate` run against the live DB after every upgrade
- `--with-redis` for shared rate limiting across replicas
- Monitoring stack (`--with-monitoring`) scraped and alerting
