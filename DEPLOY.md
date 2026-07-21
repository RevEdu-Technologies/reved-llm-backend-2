# RevEd Backend — Deployment Runbook

**Audience:** anyone deploying or operating the RevEd FastAPI backend.
**Companion docs:** [`.env.example`](.env.example) (the canonical env var list), [`FRONTEND_HANDOFF.md`](FRONTEND_HANDOFF.md) (the API contract).

---

## 1. Prerequisites

Before the first deploy you need accounts and credentials for:

| Dependency | What you need | Notes |
|---|---|---|
| **Supabase** | A project with Postgres + Auth enabled | Grab the DB URL (transaction pooler, port 6543), `SUPABASE_URL`, anon key, service-role key, and the **JWT signing secret** from Project Settings → API → JWT Settings. |
| **Groq** | API key | Used for student tutoring, lesson notes, quizzes, feedback, parent explanations. |
| **Pinecone** | API key + index | Index dimension must match `PINECONE_DIMENSION` (default `768` for `BAAI/bge-base-en-v1.5`). The metric must be `cosine`. |
| **HuggingFace** | API key (only if `EMBEDDING_BACKEND=hf_api`) | Local embeddings (`EMBEDDING_BACKEND=local`) don't need this. |
| **Redis** | A reachable instance | Backs distributed rate limiting and (optionally) the response cache. Compose ships one; production should use a managed service. |
| **Container registry** | GHCR / ECR / GAR write access | The CI workflow pushes to `ghcr.io/<org>/<repo>/reved-backend`. |
| **Hosting target** | Anything that runs an OCI container | k8s, ECS, Cloud Run, Fly, Render, Railway — see §5. |

---

## 2. Environment variables

The full canonical list lives in [`.env.example`](.env.example). Grouped by criticality:

**Required (app refuses to start without these):**
- `DATABASE_URL` — Supabase pooler URL. URL-encode special chars in the password (`#` → `%23`, `/` → `%2F`, `@` → `%40`).
- `GROQ_API_KEY`
- `PINECONE_API_KEY`
- `SUPABASE_JWT_SECRET` — required whenever `AUTH_ENABLED=true`.

**Production hardening (must be set in staging/prod):**
- `ENVIRONMENT=production` (or `staging`). The app **refuses to start** if `ENVIRONMENT` is production-like and `AUTH_ENABLED=false`.
- `AUTH_ENABLED=true`.
- `CORS_ALLOWED_ORIGINS` — comma-separated list of the real frontend origins (no trailing slash). Default `http://localhost:3000` will reject your real frontend.
- `REDIS_URL` — without this, slowapi falls back to in-memory limits which are per-worker (and therefore wrong with >1 uvicorn worker).
- `CACHE_BACKEND=redis` if you want shared caching.

**Tunable (defaults are sane):**
- `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` — match to your Supabase pooler quota.
- `GROQ_MODEL` / `GROQ_MAX_COMPLETION_TOKENS` / `GROQ_TEMPERATURE`.
- `PINECONE_INDEX_NAME` / `PINECONE_DIMENSION` / `PINECONE_REGION` / `PINECONE_NAMESPACE`.
- `HF_EMBEDDING_MODEL` / `EMBEDDING_BACKEND`.

> **Secrets management:** `.env` files are fine for local dev only. In staging/prod, secrets are fetched at process start by [`app/core/secrets.py`](app/core/secrets.py). See §2a for the backend selection and rotation playbook.

### 2a. Secrets backend

The loader is pluggable; pick the backend that matches your platform.

| Backend | `SECRETS_BACKEND` | Extra dependency | Required env |
|---|---|---|---|
| Env vars (default, local dev) | `env` (or unset) | — | the env var named by `fallback_env` (or `NAME.upper()`) |
| AWS Secrets Manager | `aws` | `pip install boto3` | standard AWS credential chain + `AWS_REGION` |
| GCP Secret Manager | `gcp` | `pip install google-cloud-secret-manager` | application-default creds + `GCP_PROJECT` |
| HashiCorp Vault (KV v2) | `vault` | `pip install hvac` | `VAULT_ADDR`, `VAULT_TOKEN`, optional `VAULT_KV_MOUNT` (defaults to `secret`) |

Optional knobs (all backends):
- `SECRETS_PREFIX` — prepended to logical names, e.g. `reved/prod` → AWS secret `reved/prod/groq_api_key`.
- `SECRETS_CACHE_TTL_SECONDS` — in-process cache TTL (default 300). Set to `0` to disable caching.

**Currently migrated** (fetched via `load_secret`, with env var fallback):
- `groq_api_key` ↔ `GROQ_API_KEY`
- `pinecone_api_key` ↔ `PINECONE_API_KEY`
- `supabase_jwt_secret` ↔ `SUPABASE_JWT_SECRET`

The remaining env vars in §2 are still read directly from the environment — your platform's secret manager should still inject them as env vars. We migrate more keys to `load_secret` as the deploy target firms up.

### 2b. Rotation playbook

The same procedure works for any of the three migrated secrets.

1. **Stage the new value** in your secrets backend as a new version. AWS Secrets Manager: `aws secretsmanager put-secret-value --secret-id reved/prod/groq_api_key --secret-string '<new>'`. Vault: `vault kv put secret/reved/prod/groq_api_key value='<new>'`. GCP: `gcloud secrets versions add reved-prod-groq-api-key --data-file=-`.
2. **Validate** by reading the new version back from a throwaway shell that has the same IAM role as the running pods.
3. **Restart the pods.** A rolling restart (`kubectl rollout restart deploy/reved-api`, `gcloud run services update --revision-suffix=...`, `fly deploy --image <same>`) is sufficient — there is no in-place reload; the in-process cache TTL is up to `SECRETS_CACHE_TTL_SECONDS`, but a full restart is the contract.
4. **Confirm** at least one new pod is serving and healthy (`/api/v1/health/ready`) before proceeding to the next stage.
5. **Revoke** the old value at the upstream provider (Groq dashboard, Pinecone console, Supabase JWT rotation). Mark the old backend version as deprecated; keep it for one release in case rollback is needed.
6. **Audit:** check `event=jwt_decode outcome=failure reason=invalid_signature` traffic for an hour after a `SUPABASE_JWT_SECRET` rotation. A spike indicates frontends are still holding old tokens — expected and self-healing as users re-authenticate.

Emergency rotation (compromised key): follow steps 1–4 immediately, then revoke at the upstream provider before doing anything else. Spend caps and rate limits buy time but do not stop a determined attacker holding a valid key.

---

## 3. Build the image

The Dockerfile lives at [`docker/Dockerfile`](docker/Dockerfile). Build from the repo root so the build context contains `main.py`, `app/`, `requirements.txt`, etc.

```bash
docker build -f docker/Dockerfile -t reved-backend:$(git rev-parse --short HEAD) .
docker tag  reved-backend:$(git rev-parse --short HEAD) reved-backend:latest
```

**Tagging convention:** every push to main produces two tags via CI (`.github/workflows/ci.yml`):
- `ghcr.io/<org>/<repo>/reved-backend:<full-sha>` — immutable, what staging/prod deploys reference.
- `ghcr.io/<org>/<repo>/reved-backend:latest` — moving pointer for convenience only; **never** deploy `latest` to prod.

Verify locally before pushing:
```bash
docker run --rm -p 8000:8000 --env-file .env reved-backend:latest
curl http://localhost:8000/api/v1/health        # → 200 with {"status":"success",...}
curl http://localhost:8000/api/v1/health/ready  # → 200 with DB + cache OK
```

---

## 4. Migrations

The backend uses Alembic. Migration scripts are in [`app/db/migrations/versions`](app/db/migrations/versions).

Apply against any environment by exporting `DATABASE_URL` (sync URL — Alembic uses psycopg, not asyncpg; the loader auto-rewrites the scheme):

```bash
# Inspect first
alembic current
alembic history --verbose

# Apply
alembic upgrade head

# Roll back one revision
alembic downgrade -1
```

**Always run migrations BEFORE deploying the new app image** so the new code never starts against a schema it doesn't understand. If a migration fails partway, recover by:

1. `alembic current` — confirm the recorded revision.
2. If the schema is stuck mid-migration, fix manually in the DB and `alembic stamp <good-revision>` to reset Alembic's view.
3. Re-deploy the **previous** app image to restore service while you investigate.

---

## 5. First deploy

Order matters. The provisioning sequence below is the same on k8s, Cloud Run, ECS, or Fly:

1. **Provision dependencies first** (Supabase project, Pinecone index, Redis, secret manager). Confirm you can reach each from a throwaway pod / instance before deploying the app.
2. **Push the image** to the registry (`docker push ghcr.io/.../reved-backend:<sha>`).
3. **Inject env vars** into your platform (Kubernetes Secret, Cloud Run env, ECS task definition, Fly secrets, etc.). Double-check `ENVIRONMENT=production` and `AUTH_ENABLED=true`.
4. **Run `alembic upgrade head`** against the production DB (one-shot job or local run with prod `DATABASE_URL`).
5. **Deploy the app**.
6. **Smoke test** (see §7).
7. **Wire health probes** to `/api/v1/health` (liveness) and `/api/v1/health/ready` (readiness).

---

## 6. Subsequent deploys (zero-downtime)

```
[CI builds image] → [push to registry] → [run migrations] → [rolling app deploy] → [smoke test]
```

1. CI runs on every push: lint, test, build. Merging into `main` pushes `:latest` and `:<sha>`.
2. Run `alembic upgrade head` against prod **before** changing the running image. Migrations must be backwards-compatible with the *currently-running* app (additive changes only; defer destructive drops to the deploy *after* the column/table is no longer referenced).
3. Update the deployment to the new `:<sha>` tag. Kubernetes/Cloud Run/ECS handle rolling replacement; ensure `maxUnavailable=0` (k8s) or equivalent.
4. The platform's readiness probe should hit `/api/v1/health/ready`; only mark new pods healthy when DB + cache come back OK.
5. Old pods drain on SIGTERM. FastAPI's lifespan calls `dispose_engine()` to close DB connections cleanly.

---

## 6a. Background workers — webhook dispatcher

If you use **outbound webhooks** (any active `webhook_subscriptions` rows), you
must run the dispatcher as a **separate long-running process** alongside the
API. The API only writes events into the `webhook_deliveries` outbox; nothing is
delivered until the dispatcher drains it — without it, deliveries pile up as
`pending` forever.

- Run it from the **same image** (the Dockerfile already ships `scripts/`):
  ```
  python -m scripts.webhook_dispatcher           # loop, poll every 2s
  python -m scripts.webhook_dispatcher --once     # one pass (cron/Job-friendly)
  ```
- It needs the same `DATABASE_URL` as the API and outbound network egress to
  subscriber URLs. No inbound port; **no health probe** (it's not an HTTP
  server) — supervise it by restart policy / liveness-by-logs.
- Safe to run **multiple replicas**: it claims rows with
  `FOR UPDATE SKIP LOCKED`, so there's no double-delivery. Exits cleanly on
  SIGINT/SIGTERM; an interrupted pass loses nothing (rows stay `pending`).
- Deploy shape: a second k8s Deployment / Cloud Run Job (cron with `--once`) /
  ECS service / Fly process group running the command above. Local + container
  dev: `docker compose --profile webhooks up` starts a `webhook-dispatcher`
  service next to the API.

Full subscriber contract, retry policy, and event catalog: see `WEBHOOKS.md`.
The `webhook_subscriptions` / `webhook_deliveries` tables are created by the
standard `alembic upgrade head` (migration `a7c1e2f4b809`).

---

## 7. Health checks

| Path | Purpose | Suggested probe config |
|---|---|---|
| `GET /api/v1/health` | **Liveness** — confirms the process is up. Always returns 200 unless the worker is wedged. | Every 30s, fail after 3, timeout 5s. |
| `GET /api/v1/health/ready` | **Readiness** — actually queries Postgres and Redis. Returns 200 if both report healthy, 200 with `data.status="degraded"` if any dep is down (the body tells you which). | Every 15s, fail after 3, timeout 5s. Only route traffic when ready. |

Kubernetes example:
```yaml
livenessProbe:
  httpGet: { path: /api/v1/health, port: 8000 }
  initialDelaySeconds: 20
  periodSeconds: 30
  timeoutSeconds: 5
  failureThreshold: 3
readinessProbe:
  httpGet: { path: /api/v1/health/ready, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 3
```

Cloud Run / Fly / Render: point health checks at the same paths.

---

## 8. Logs & alerts

**Log format**: every line on stdout is a JSON object (`app/core/logging.py`). Keys:
- `timestamp` (ISO8601 with offset)
- `level` (`INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- `logger` (e.g. `main`, `app.services.student.tutor_service`)
- `message`
- `request_id` (per-request UUID, when emitted inside a request)
- `trace_id` (W3C 32-hex, when emitted inside a request)
- `span_id` (W3C 16-hex, identifies the logical span within the trace)

The dedicated `reved.audit` logger emits separate pure-JSON lines (no level/timestamp wrapper) for auth events. Filter `event in {jwt_decode, role_check, admin_action}` to capture them in a SIEM stream.

The dedicated `reved.access` logger emits one line per HTTP request with `method`, `path`, `status`, `duration_ms`, `user_id`, `role`, plus the three correlation IDs above. Health probes and `/metrics` are skipped (`OBSERVABILITY_EXCLUDED_PATHS`).

**Request correlation**: every response carries two headers:
- `X-Request-Id` — our local ops ID. Inbound `X-Request-Id` is honored so ingress → app → logs share an id you can grep.
- `traceparent` — W3C distributed-tracing header. Inbound is honored (trace continues, fresh local span); fresh trace minted when absent. Pair our service with sibling services or front-door gateways that speak W3C trace-context.

**Log grep snippets** (replace `app.log` with your aggregator query):

```bash
# Every line emitted while handling one request
jq -c 'select(.request_id == "abcd...")' app.log

# Every span inside one distributed trace (cross-service when ingress sets traceparent)
jq -c 'select(.trace_id == "0af7651916cd43dd8448eb211c80319c")' app.log

# All requests one user made today
jq -c 'select(.user_id == "user-uuid")' app.log

# Slow requests (>1s) by route
jq -c 'select(.logger == "reved.access" and .duration_ms > 1000) | {path, duration_ms, request_id}' app.log

# Auth failures, grouped by reason
jq -c 'select(.event == "jwt_decode" and .outcome == "failure") | .reason' app.log | sort | uniq -c
```

**Recommended alerts** (wire to whatever you use — PagerDuty, Slack, Datadog, Grafana OnCall):

| Signal | Threshold | Severity |
|---|---|---|
| HTTP 5xx rate | > 1% over 5 min | P1 |
| p95 latency on `/student/ask` | > 5s over 5 min | P2 |
| Auth failure spike (`event=jwt_decode outcome=failure`) | > 50/min | P2 (potential brute force / mass token rotation) |
| Role-mismatch spike (`event=role_check outcome=failure`) | > 20/min | P3 (likely a frontend bug; can also be probing) |
| Rate-limit-hit volume (`code=rate_limited`) | > 100/min | P3 (capacity planning signal) |
| LLM upstream errors (`code=upstream_error`) | > 5/min for 10 min | P2 |
| DB pool exhaustion (asyncpg errors) | any | P1 |

Phase 4 of the plan adds Prometheus `/metrics`; until then, alert off log queries.

---

## 9. Rollback

**App rollback** (most common case — code regression):
```bash
# Identify the last-known-good SHA
gh run list --workflow ci.yml --branch main --limit 5
# Re-deploy that tag
kubectl set image deploy/reved-api api=ghcr.io/<org>/<repo>/reved-backend:<good-sha>
# Or Cloud Run:
gcloud run services update reved-api --image ghcr.io/<org>/<repo>/reved-backend:<good-sha>
```

**Migration rollback** (less common — schema change broke prod):
```bash
alembic downgrade -1     # or `alembic downgrade <revision-id>`
```
Only safe if the previous app version can still operate against the rolled-back schema. The hard rule: every migration in `app/db/migrations/versions/` should be additive-then-destructive across **two** deploys (deploy N adds, deploy N+1 reads/writes, deploy N+2 drops). When that contract is honored, single-step rollback is always safe.

**Emergency: rate-limit-spam, runaway LLM spend, or compromised key:**
1. Rotate `GROQ_API_KEY` in the secret manager and restart pods (this is the fastest spend brake).
2. If a single caller is the source, identify their `rate_limit_key` from logs and add an explicit deny (currently manual — N7 in the plan adds tiered/blocklist limits).
3. As a last resort, set `CORS_ALLOWED_ORIGINS=` to an empty value and roll — blocks all browser traffic.

---

## 10. CI pipeline

The single workflow lives at [`.github/workflows/ci.yml`](.github/workflows/ci.yml). Three jobs:

| Job | What it does | Runs on |
|---|---|---|
| `lint` | `ruff check .` | every push & PR |
| `test` | Postgres 16 + Redis 7 as services, `alembic upgrade head`, then `pytest -q` | every push & PR |
| `build` | `docker build` from the repo root. Pushes to `ghcr.io/<org>/<repo>/reved-backend:{sha,latest}` only when the event is a push to `main`. | every push & PR (push only on main) |

Required GitHub secrets: `GITHUB_TOKEN` (auto, used to push to GHCR). To deploy to your hosting platform from CI, add a deploy step at the end of `build` with platform-specific credentials.

---

## 10. Performance & capacity

Load-test harness lives in [`loadtest/`](loadtest/README.md). Three k6 scenarios — reads, LLM writes, mixed — cover the shapes the production traffic will take.

### How to run

```bash
# Smoke (10 s)
k6 run --duration 10s --vus 5 loadtest/k6/reads.js

# Full scenarios (point at staging, not prod)
BASE_URL=https://reved-staging.example.com AUTH_MODE=bearer TOKEN=<jwt> \
  k6 run loadtest/k6/reads.js
```

See [`loadtest/README.md`](loadtest/README.md) for env vars, tuning knobs, and the symptom → knob table.

### Tunables (defaults in [`app/core/config.py`](app/core/config.py))

| Setting | Default | Notes |
|---|---|---|
| `DATABASE_POOL_SIZE` | 5 | Per-worker SQLAlchemy pool. Multiply by uvicorn `--workers` to size Supabase pooler quota. |
| `DATABASE_MAX_OVERFLOW` | 10 | Burst above pool_size. Long-tail of reads tolerates more overflow than writes. |
| `LLM_LIMIT` | `10/minute` | slowapi per-key cap on `/student/ask`, `/teacher/{lesson-notes,quiz,student-feedback}`, `/parent/explain-topic`. Raising this without raising Groq quota = spend without throughput. |
| `CACHE_DEFAULT_TTL_SECONDS` | 300 | Used by the response cache layer (Phase 5 N6). |
| uvicorn `--workers` | 1 | Set via the container `CMD` or `WEB_CONCURRENCY`. Start at `2 × CPU + 1`; halve if memory pressure shows up. |

### Results

Capture k6 summary output here after each baseline run.

```
# 2026-05-17 — Smoke (10 s, 5 VUs, localhost dev box, AUTH_MODE=dev)
http_req_duration     p50=3.00s  p95=6.01s  p99=—   (12 iterations)
http_req_failed       0%
iterations            12 over 13.6 s
checks                24/24 succeeded
Notes:
  - Single uvicorn worker on dev laptop hitting real Supabase (UK→US pooler RTT).
  - Pinecone + Groq creds present in .env but reads scenario doesn't call either.
  - All three endpoints (student_conversations, parent_child_activity,
    teacher_class_progress) returned the standard envelope.

# 2026-05-17 — Reads (2 min, 20 VUs, localhost dev box, AUTH_MODE=dev)
http_req_duration     p50=4.05ms  p95=3.89s  max=6.79s
  parent_child_activity      p50=3.86ms  p95=3.46s
  student_conversations      p50=4.65ms  p95=3.88s
  teacher_class_progress     p50=4.07ms  p95=4.32s
http_req_failed       70% (slowapi 429s — see note)
iterations            1202 over 2m00.8s (~10 req/s aggregate)
checks                2404/2404 succeeded
Notes:
  - The 70% "failure" rate is slowapi rate-limiting, not a server bug.
    AUTH_MODE=dev shares one rate-limit key per role; 20 VUs split across
    3 roles ≈ 7 req/s per key vs the default 60/min cap → most calls 429.
    Switch to AUTH_MODE=bearer with per-VU tokens for a clean read baseline.
  - When NOT rate-limited, median is 3-4 ms (cached path) and p95 is ~4 s
    (cold DB read). The 4 s tail is dominated by Supabase pooler latency
    from the UK dev box, not application work. Same query from a Pod in
    the same region as Supabase typically lands under 100 ms.
  - These numbers are a sanity check on the harness, NOT a production
    baseline. Re-run from staging once that exists.

# YYYY-MM-DD — Reads (10 min, 100 VUs, staging, AUTH_MODE=bearer)
http_req_duration     p50=…ms  p95=…ms  p99=…ms
http_req_failed       …%
iterations            …

# YYYY-MM-DD — LLM writes (10 min, 20 VUs, staging, AUTH_MODE=bearer)
http_req_duration     p50=…ms  p95=…ms  p99=…ms
http_req_failed       …%
iterations            …

# YYYY-MM-DD — Mixed (30 min, 80/20, staging)
read p95              …ms
write p95             …ms
DB pool peak          …/X
Redis hit rate        …%
```

Acceptance for Phase 4 sign-off: mixed scenario sustains target load with read p95 < 1 s, write p95 < 5 s, 0 % non-rate-limit errors.

### CLI overrides

`reads.js` honours `DURATION` and `VUS` env vars so smoke runs don't need file edits:

```bash
DURATION=2m VUS=20 k6 run loadtest/k6/reads.js   # short dev run
k6 run loadtest/k6/reads.js                       # full 10m × 100 VUs default
```

---

## 11a. Observability (Prometheus)

Backend exposes `GET /metrics` (Prometheus text format) — installed in [`app/core/metrics.py`](app/core/metrics.py) via `prometheus-fastapi-instrumentator`. No auth required; restrict at the ingress / Prometheus scrape-IP allowlist level if needed.

| Asset | Path |
|---|---|
| Grafana dashboard JSON | [`observability/dashboard.json`](observability/dashboard.json) |
| Prometheus alert rules | [`observability/alerts.yml`](observability/alerts.yml) |
| How-to | [`observability/README.md`](observability/README.md) |

Three required alerts (Phase 4 sign-off):

1. `HighFiveXxRate` — 5xx > 1% over 5 min → P1
2. `HighP95LatencyStudentAsk` — p95 > 5s over 5 min → P2
3. `AuthFailureSpike` — JWT decode failures > 50/min over 5 min → P2 (security)

Recurring alerts in §8 are still valid — they're log-derived and complement these metric-derived ones.

To trigger a synthetic 5xx for an end-to-end test of the alert: temporarily set `DATABASE_URL` to a bogus host in a canary pod; the readiness probe fails, the load balancer routes traffic to other pods, but in-flight requests to the canary log 5xx briefly.

---

## 11b. Observability (OpenTelemetry traces)

The backend ships an OpenTelemetry tracer + auto-instrumentation for FastAPI, SQLAlchemy, httpx (catches Groq), and asyncpg. Manual spans wrap Pinecone retrieval + the embedder. Trace + span IDs flow into the access log and the app log via the correlation filter — same `trace_id` / `span_id` JSON keys ops already grep on.

### Exporter swap

| `OTEL_EXPORTER` | Behavior | When to use |
|---|---|---|
| `none` | Spans created (IDs flow to logs) but never exported. | Test suites — set by `tests/conftest.py`. |
| `console` (default) | Spans pretty-print to stdout. | Local dev. Pair with `jq` to filter. |
| `otlp` | OTLP/HTTP exporter — reads `OTEL_EXPORTER_OTLP_ENDPOINT`. | Production. |

To wire a real backend, set two env vars and restart — no code change:

```bash
# Honeycomb
export OTEL_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
export OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=YOUR_API_KEY

# Grafana Cloud Tempo
export OTEL_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=https://tempo-prod-04-prod-us-east-0.grafana.net/tempo
export OTEL_EXPORTER_OTLP_HEADERS=authorization=Basic\ <base64-encoded-user:token>

# Datadog (agent-side OTLP receiver)
export OTEL_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://datadog-agent:4318
```

OTEL's standard env vars (`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`) all work — they're read by the SDK at startup.

### What gets traced

Every request becomes a root span (created by the FastAPI instrumentation). Children:

- `pinecone.query` — vector retrieval, attributes: namespace, top_k, subject, role
- `rag.embed_query` — HuggingFace embedding step, attribute: query_length
- SQLAlchemy session execute spans — DB queries auto-tagged with statement + DB system
- httpx spans — every outbound request (Groq is httpx-based, so its chat-completions calls show up too)
- asyncpg spans — raw asyncpg calls (mostly under SQLAlchemy)

Health probes, `/metrics`, `/docs`, etc. are excluded — same `OBSERVABILITY_EXCLUDED_PATHS` set the access log uses.

### Triage workflow

1. Open the request in your tracing backend by `trace_id` from the access-log line you're triaging.
2. The waterfall surfaces what dominated latency: a slow Pinecone call, a slow Groq call (httpx span timing), an N+1 SQL pattern, etc.
3. Filter by `service.name=reved-backend` + `deployment.environment=staging|production` to scope.

---

## 11. Quick reference

```bash
# Build & run locally
docker build -f docker/Dockerfile -t reved-backend:local .
docker run --rm -p 8000:8000 --env-file .env reved-backend:local

# Or via compose (adds redis automatically)
docker compose -f docker/docker-compose.yml up --build

# Migrations
alembic upgrade head
alembic downgrade -1

# Health checks
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/health/ready

# Smoke an authenticated dev route (AUTH_ENABLED=false only)
curl -H "X-Dev-Role: teacher" http://localhost:8000/api/v1/teacher/class-progress
```
