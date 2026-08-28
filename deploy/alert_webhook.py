"""Minimal local Alertmanager webhook receiver (v8 P0 monitoring).

Alertmanager POSTs alert JSON here; we append each alert to /var/log/rag-alerts.log
(and stdout) so an operator can `tail -f` it without any external integration. This makes
the "dead Qdrant detected within minutes" goal observable on the VPS today; swap in
Slack/PagerDuty for real paging in production (see deploy/alertmanager.yml).

Run: python3 deploy/alert_webhook.py   (listens on :9099)
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.environ.get("RAG_ALERT_LOG", "/var/log/rag-alerts.log")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("alert-webhook")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        alerts = payload.get("alerts", [])
        with open(LOG_PATH, "a") as f:
            for a in alerts:
                line = (
                    f"ALERT severity={a.get('labels', {}).get('severity')} "
                    f"name={a.get('labels', {}).get('alertname')} "
                    f"status={a.get('status')} :: {a.get('annotations', {}).get('description', '')}\n"
                )
                f.write(line)
                log.info(line.strip())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):  # noqa: A002 - silence default request logging
        return


if __name__ == "__main__":
    port = int(os.environ.get("ALERT_WEBHOOK_PORT", "9099"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("alert webhook listening on :%s -> %s", port, LOG_PATH)
    srv.serve_forever()
