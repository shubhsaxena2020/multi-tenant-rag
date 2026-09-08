#!/usr/bin/env bash
# Bring up the RAG monitoring stack NATIVELY (no Docker; socket is root-only here).
# Launches Prometheus, Alertmanager, and the local alert webhook sink.
# Idempotent: skips any component already listening on its port.
set -u
BASE=${MONITORING_BASE:-./monitoring}
BIN=$BASE/bin
REPO=${RAG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
LOG=$BASE/log
mkdir -p "$LOG"

listen() { # port -> 0 if something is listening
  (ss -ltn 2>/dev/null | grep -q ":$1 ") && return 0 || return 1
}

# 1) Alert webhook sink (app/deploy/alert_webhook.py) -> /var/log/rag-alerts.log
if ! listen 9099; then
  echo "starting alert-webhook on :9099"
  cd "$REPO" && nohup "$REPO/.venv/bin/python3" deploy/alert_webhook.py > "$LOG/alert-webhook.log" 2>&1 &
else
  echo "alert-webhook already on :9099"
fi

# 2) Prometheus on :9090
if ! listen 9090; then
  echo "starting prometheus on :9090"
  cd "$BASE" && nohup "$BIN/prometheus" \
    --config.file="$BASE/prometheus.native.yml" \
    --storage.tsdb.path="$BASE/data" \
    --storage.tsdb.retention.time=30d \
    --web.listen-address=":9090" > "$LOG/prometheus.log" 2>&1 &
else
  echo "prometheus already on :9090"
fi

# 3) Alertmanager on :9093
if ! listen 9093; then
  echo "starting alertmanager on :9093"
  cd "$BASE" && nohup "$BIN/alertmanager" \
    --config.file="$BASE/alertmanager.yml" \
    --storage.path="$BASE/am-data" \
    --web.listen-address=":9093" > "$LOG/alertmanager.log" 2>&1 &
else
  echo "alertmanager already on :9093"
fi

sleep 4
echo "--- target health (prometheus) ---"
curl -s -m5 http://localhost:9090/api/v1/targets/health | head -c 400
echo
echo "--- up targets ---"
curl -s -m5 'http://localhost:9090/api/v1/query?query=up' | head -c 500
echo
echo "DONE. Grafana (if used) is separate; add Prometheus datasource http://localhost:9090."
