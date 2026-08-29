# Qdrant Healthcheck Postmortem & Fix

**Date:** 2026-08-29
**Service:** `rag-service-qdrant-1` (image `qdrant/qdrant:pinned-20260829`, qdrant 1.19.0)
**Compose file:** `/home/ubuntu/rag-service/docker-compose.yml` (project `rag-service`, service `qdrant`)
**Affected stack:** rag-service (currently serving live tenant traffic on qdrant + app + redis)

---

## 1. Symptom

`rag-service-qdrant-1` was reporting as **unhealthy** under Docker, even though it was
accepting connections and the qdrant process itself was running fine. At the time of
investigation the container had been recreated ~5 minutes earlier and was already
reporting `(healthy)` again — but the underlying healthcheck definition still contained
the broken probe, so a future `docker compose up -d` from a clean checkout would
reproduce the failing label.

## 2. Root cause (two independent defects)

The healthcheck committed at `HEAD` was:

```yaml
healthcheck:
  test: ["CMD", "wget", "-q", "--spider", "http://localhost:6333/health"]
```

### Defect A — `wget` is not installed in the image
The official `qdrant/qdrant` image ships **no** `wget`, `curl`, `nc`, or `python`.
Verified inside the running container:

```
$ command -v wget
wget: not found
```

So the probe exited with `exec: wget: not found` permanently -> Docker marked the
container unhealthy after `retries` (3) failed checks.

### Defect B — wrong endpoint (`/health` returns 404)
Even if `wget` were present, the probed path is wrong for this qdrant build.
All health endpoints were probed from inside the container (via bash `/dev/tcp`):

| Endpoint             | Result            |
|----------------------|-------------------|
| `/health`            | `404 Not Found`   |
| `/healthz`           | `200 OK`  ✅       |
| `/health/readiness`  | `404 Not Found`   |
| `/health/liveness`   | `404 Not Found`   |

The correct liveness/readiness endpoint for qdrant 1.19.0 is `/healthz`.

**Conclusion:** the failure was a *misconfigured healthcheck*, not a broken service.

## 3. The fix (already applied on disk, pre-existing this investigation)

The compose file's working tree already contains the corrected probe (it was applied in
a prior session; this investigation verified it):

```yaml
healthcheck:
  # FIX: the qdrant image ships NO wget/curl/python/nc, and `/health` returns 404.
  # This uses bash's built-in /dev/tcp against the real endpoint /healthz (returns 200 OK),
  # then greps the status line.
  test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/6333; printf 'GET /healthz HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3; head -1 <&3 | grep -q 200"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 120s
```

Why this works:
- `bash` **is** present in the image (`GNU bash, version 5.2.37`), so `CMD bash -c ...` executes.
- `/dev/tcp` is a bash builtin — no external tool required.
- `/healthz` returns `200 OK`, so `grep -q 200` succeeds and the probe exits 0.

`start_period` was also lowered from `600s` to `120s` (the 10-minute wait was unnecessary
and just delayed first recovery).

## 4. Verification

All checks performed read-only against the live stack; no container was restarted or recreated.

**Healthcheck status (running container):**
```
$ docker inspect rag-service-qdrant-1 --format '{{.State.Health.Status}}'
healthy
FailingStreak: 0
last 5 checks (10:46-10:48 UTC): all exit=0
```

**Service itself is genuinely working:**
- Qdrant `/healthz` (inside container) -> `HTTP/1.1 200 OK`
- Qdrant logs: collection `rag` recovered 1/1 (100%), `Qdrant HTTP listening on 6333`, `gRPC listening on 6334`, no errors
- `rag-service-redis-1`: `redis-cli ping` -> `PONG`
- `rag-service-app-1`: `GET /health` -> `{"status":"ok","service":"rag-service","version":"1.0.0"}`
- Disk: 148G total, 59G free (61% used)
- Memory: `MemAvailable` ~12.4G

**Test suite integrity (non-disruptive):**
- `pytest --co -q` -> **115 tests collected** cleanly.
- `tests/conftest.py` forces `QDRANT_URL=:memory:`, so the suite does **not** require a live
  daemon and will not write to the production tenant collection. (A full *run* against the
  live stack was deliberately avoided because `tests/test_api.py` pins `QDRANT_URL=http://localhost:6333`
  = the live tenant DB; collect-only was used as the safe middle ground.)

**Sibling containers untouched and still healthy:**
- `rag-service-redis-1` (Up 7h) — PONG
- `rag-service-app-1` (Up 7h) — `/health` ok

No other rag-service container was restarted, recreated, or modified during this work.

## 5. Current state (RESOLVED — 2026-08-29 ~10:53 UTC)

- `rag-service-qdrant-1` now reports `(healthy)` in `docker ps`. FailingStreak 0; every
  health check since the container was recreated at `StartedAt 10:42:43` has exited 0
  (~20 consecutive passing checks).
- The **running container** carries the fixed probe (verified via `docker inspect`):
  `Test: ["CMD","bash","-c","exec 3<>/dev/tcp/localhost/6333; ... grep -q 200"]`.
- The fix is **now committed.** It landed in `HEAD` at `6b6a710 v10.5 (PHASE F)` — the
  qdrant service's `healthcheck.test` in `docker-compose.yml` is the corrected
  `bash /dev/tcp` form. The broken `wget` probe is gone from the committed tree, so a
  future `docker compose up -d` or `git checkout` of this file will keep the correct probe.
- The container was recreated once at 10:42:43 (RestartCount 0) — that `up -d` applied the
  committed fix, which is why it has been healthy continuously since. (This also explains
  the earlier "unhealthy" observation: it was pre-fix, before that recreate.)

**No action required on the container:** it is healthy and the fix is durable in the
committed code. The only remaining artifact is this documentation file, which is untracked
and recommended to be committed (see §6).

## 6. Recommendation / closeout

- **Container:** no restart or recreate needed. It is `(healthy)` and stable; the probe is
  correct and has not failed since the 10:42:43 recreate.
- **Code fix:** already committed at `6b6a710` (v10.5 PHASE F). Nothing further to change
  in `docker-compose.yml`.
- **Docs:** commit this file (`QDRANT-HEALTHCHECK-FIX.md`) as a standalone record. Branch
  `master` is **diverged** from `origin/master` (8 vs 34 commits) — do **NOT** push without
  coordinating with the other agents active in this repo (agent-1 / window-0), since pushing
  the diverged history could clobber their in-flight work.
