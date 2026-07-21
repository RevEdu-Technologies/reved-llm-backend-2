# RevEd backend load tests

Three [k6](https://k6.io) scenarios for capacity planning before launch.
The scripts live in `loadtest/k6/`. Results land in `DEPLOY.md` §10
(Performance & capacity).

## Install k6

```bash
# macOS
brew install k6
# Linux (apt)
sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69 \
  && echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list \
  && sudo apt-get update && sudo apt-get install k6
# Windows
choco install k6
```

## Target

By default scripts hit `http://localhost:8000` in dev-mode auth
(`X-Dev-Role` header). To run against staging:

```bash
BASE_URL=https://reved-staging.example.com AUTH_MODE=bearer TOKEN=<jwt> \
  k6 run loadtest/k6/reads.js
```

When `AUTH_MODE=dev`, all virtual users share one rate-limit key (the
role string). Use bearer mode with per-VU tokens to get a clean LLM-write
run — otherwise slowapi will throttle most calls.

## Scenarios

| File | What it does | Duration | Target |
|---|---|---|---|
| `reads.js` | 100 VUs × ~1 req/s round-robin across 3 read endpoints | 10 min | p95 < 1s |
| `llm-writes.js` | 20 VUs × 1 req/min on `/student/ask` | 10 min | p95 < 5s |
| `mixed.js` | 100 req/s reads + 1 req/s writes (constant arrival rate) | 30 min | p95 < 1s reads, < 5s writes |

## Run

```bash
# Smoke (10 s) — sanity check the script loads + endpoint responds.
k6 run --duration 10s --vus 5 loadtest/k6/reads.js

# Full scenarios — note: these are non-trivial. Run against staging,
# not local, unless you've capped LLM keys to a low budget.
k6 run loadtest/k6/reads.js
k6 run loadtest/k6/llm-writes.js
k6 run loadtest/k6/mixed.js

# Output JSON for downstream ingestion (Grafana, post-mortem).
k6 run --out json=results.json loadtest/k6/mixed.js
```

## What to capture

k6 prints summary stats at the end of each run. Record these in
`DEPLOY.md` §10 results table:

* `http_req_duration` — p50, p95, p99
* `http_req_failed` — error rate
* `iterations` — total throughput
* For each tagged endpoint (use `--summary-trend-stats` to get per-tag)

Plus from the server side (read off Grafana / metrics in Phase 4d):

* DB connection-pool saturation (`asyncpg`-level metric)
* Redis hit/miss ratio (if `CACHE_BACKEND=redis`)
* CPU + memory of the API container
* Groq token-spend rate over the test window

## Tuning knobs

When the mixed scenario blows a threshold, the usual suspects are:

| Symptom | Likely cause | Knob |
|---|---|---|
| Read p95 > 1s, low CPU | DB pool too small | `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` |
| Read p95 > 1s, high CPU | Worker count too low | uvicorn `--workers` (set via `WEB_CONCURRENCY` or compose) |
| Write timeouts | Groq client timeout | configurable in `app/llm/client.py` |
| Mass 429s on writes | Rate limit too tight | `LLM_LIMIT` in `app/core/rate_limit.py` |
| Redis miss > 90% (when caching expected) | TTL too short or cold cache | `CACHE_DEFAULT_TTL_SECONDS` |
