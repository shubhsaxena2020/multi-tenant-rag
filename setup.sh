#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# rag-service — one-command setup.
#
#   git clone <repo> && cd rag-service && ./setup.sh
#
# Brings up Qdrant + the API in Docker, generates a .env with fresh secrets,
# waits for health, creates your first tenant, and prints working curl
# examples. Idempotent: safe to re-run. Never overwrites an existing .env.
#
# Flags:
#   --with-monitoring   also start Prometheus + Alertmanager + Grafana
#   --with-redis        also start Redis (shared rate-limit across replicas)
#   --no-tenant         skip creating the first tenant
#   --port <n>          host port for the API (default 8000)
#   --real-models       set USE_REAL_EMBEDDER=1 / USE_REAL_RERANKER=1 in a new .env
# ---------------------------------------------------------------------------
set -eu

API_PORT=8000
PROFILES=""
MAKE_TENANT=1
REAL_MODELS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-monitoring) PROFILES="$PROFILES --profile monitoring" ;;
    --with-redis)      PROFILES="$PROFILES --profile ratelimit" ;;
    --no-tenant)       MAKE_TENANT=0 ;;
    --real-models)     REAL_MODELS=1 ;;
    --port)            shift; API_PORT="$1" ;;
    -h|--help)         sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites -------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker Desktop / Engine and retry."
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else die "docker compose plugin not found."; fi
docker info >/dev/null 2>&1 || die "docker daemon not reachable. Start Docker and retry."
ok "docker + compose present"

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 36 | tr -d '\n'
  else python3 - <<'PY'
import secrets,base64,sys; sys.stdout.write(base64.b64encode(secrets.token_bytes(36)).decode())
PY
  fi
}

# --- .env --------------------------------------------------------------------
if [ -f .env ]; then
  ok ".env already exists — leaving it untouched"
else
  say "generating .env from .env.example with fresh secrets"
  [ -f .env.example ] || die ".env.example missing — are you in the repo root?"
  cp .env.example .env
  ADMIN_KEY="adm_$(gen_secret | tr -dc 'A-Za-z0-9' | cut -c1-40)"
  MASTER_KEY="$(gen_secret)"
  # portable in-place edit (BSD + GNU sed)
  sed -i.bak "s|^ADMIN_API_KEY=.*|ADMIN_API_KEY=${ADMIN_KEY}|" .env
  sed -i.bak "s|^MASTER_ENCRYPTION_KEY=.*|MASTER_ENCRYPTION_KEY=${MASTER_KEY}|" .env
  if [ "$REAL_MODELS" = 1 ]; then
    sed -i.bak "s|^USE_REAL_EMBEDDER=.*|USE_REAL_EMBEDDER=1|" .env
    sed -i.bak "s|^USE_REAL_RERANKER=.*|USE_REAL_RERANKER=1|" .env
    say "real models enabled — first boot will download ~2 GB of weights"
  fi
  rm -f .env.bak
  ok ".env written (ADMIN_API_KEY + MASTER_ENCRYPTION_KEY generated)"
fi
# shellcheck disable=SC1091
set -a; . ./.env; set +a
ADMIN_API_KEY="${ADMIN_API_KEY:-}"

# --- bring up the stack -----------------------------------------------------
say "starting containers ($DC up -d$PROFILES)"
# API_PORT lets the compose file map a custom host port if it reads ${API_PORT}
API_PORT="$API_PORT" $DC up -d $PROFILES
ok "containers started"

# --- wait for health -------------------------------------------------------
BASE="http://localhost:${API_PORT}"
say "waiting for ${BASE}/health"
i=0
until curl -fsS "${BASE}/health" >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { $DC logs --tail=40 app || true; die "API did not become healthy in 120s"; }
  printf '.'; sleep 2
done
printf '\n'; ok "API healthy: $(curl -fsS "${BASE}/health")"
READY="$(curl -fsS "${BASE}/health/ready" 2>/dev/null || true)"; [ -n "$READY" ] && ok "ready: $READY"

# --- first tenant ---------------------------------------------------------
if [ "$MAKE_TENANT" = 1 ]; then
  say "creating your first tenant"
  HDR=""; [ -n "$ADMIN_API_KEY" ] && HDR="-H Admin-Key:${ADMIN_API_KEY}"
  RESP="$(curl -fsS -X POST "${BASE}/api/v1/tenants" $HDR \
            -H 'Content-Type: application/json' \
            -d '{"name":"my-first-tenant","plan":"standard"}')" || die "tenant create failed: check ADMIN_API_KEY"
  TID="$(printf '%s' "$RESP" | sed -n 's/.*"tenant_id" *: *"\([^"]*\)".*/\1/p')"
  KEY="$(printf '%s' "$RESP" | sed -n 's/.*"api_key" *: *"\([^"]*\)".*/\1/p')"
  ok "tenant_id = ${TID}"
  ok "api_key   = ${KEY}    (this is a secret rk_ key — store it)"
  cat <<EOF

  ── Try it ─────────────────────────────────────────────────────────────
  # ingest a line of text
  curl -X POST ${BASE}/api/v1/${TID}/ingest/text \\
    -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \\
    -d '{"title":"note","text":"Acme onboarding software helps teams start fast."}'

  # ask a question
  curl -X POST ${BASE}/api/v1/${TID}/query \\
    -H "Authorization: Bearer ${KEY}" -H 'Content-Type: application/json' \\
    -d '{"question":"what does Acme do?","top_k":3}'

  # issue a browser-safe publishable key for the widget
  curl -X POST ${BASE}/api/v1/${TID}/keys/publishable -H "Authorization: Bearer ${KEY}"
  ──────────────────────────────────────────────────────────────────────
EOF
fi

cat <<EOF

  Service     : ${BASE}
  API docs    : ${BASE}/api/v1/docs
  Demo page   : ${BASE}/demo
  Admin UI    : ${BASE}/admin/console
  Metrics     : ${BASE}/metrics
  Widget      : <script src="${BASE}/widget.js" data-tenant="<tenant_id>" data-key="<pk_ key>"></script>

  Stop:    $DC down
  Logs:    $DC logs -f app
  Config:  edit .env then '$DC up -d' to apply
EOF
ok "setup complete"
