# Observability assets

* [`dashboard.json`](dashboard.json) — Grafana dashboard (6 panels: request rate, error rate, p95 latency, LLM token spend, auth events, rate-limit hits). Import via *Dashboards → New → Import → upload JSON*. The panels assume the standard Prometheus metric names emitted by `prometheus-fastapi-instrumentator` plus the `reved_*` custom counters.
* [`alerts.yml`](alerts.yml) — Three Prometheus alert rules: `HighFiveXxRate`, `HighP95LatencyStudentAsk`, `AuthFailureSpike`. Drop into your rules tree (Prometheus `rule_files:` or Grafana Cloud / Mimir alerting UI).

## Metrics surface

`GET /metrics` exposes Prometheus text format. Highlights:

| Metric | Type | Labels | Source |
|---|---|---|---|
| `http_requests_total` | Counter | handler, method, status | `prometheus-fastapi-instrumentator` |
| `http_request_duration_seconds_*` | Histogram | handler, method, status | same |
| `reved_auth_events_total` | Counter | event, outcome, reason | `app/core/audit.py` (mirrors every audit line) |
| `reved_llm_tokens_total` | Counter | provider, model, kind=prompt/completion | `app/llm/client.py` |

## Verify locally

```bash
# Boot the app
uvicorn main:app --reload

# In another terminal — generate some traffic + scrape /metrics
curl -H "X-Dev-Role: student" http://localhost:8000/api/v1/student/conversations
curl http://localhost:8000/metrics | grep -E "^(reved_|http_requests_total)" | head -20
```

## Tying it together

1. Backend pods expose `/metrics` (port 8000).
2. Prometheus / Grafana Agent scrapes them every 15-30 s.
3. The dashboard reads from that data source.
4. The alert rules evaluate against the same data and fire into your alertmanager (PagerDuty / Slack / etc.).

For metric backends that prefer push (Datadog, NewRelic), use their OTel collector or a vendor-specific exporter — the metric names above are stable and vendor-agnostic.
