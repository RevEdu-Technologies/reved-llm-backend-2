# RevEd LLM Backend — Application Overview

**What it is:** A role-aware, retrieval-grounded AI API for the RevEd learning platform. It serves four user roles — student, teacher, parent, admin — each with distinct AI capabilities, all grounded against a curriculum corpus (not open-domain generation) and gated by Supabase-issued auth.

**Live URL:** `https://reved-llm-backend-2.onrender.com` (base path `/api/v1`)
**Stack:** FastAPI (async) · PostgreSQL/Supabase · Pinecone (vector search) · Groq (LLM inference) · Redis (cache/rate limiting) · SQLAlchemy + Alembic · deployed as a Docker container.

---

## 1. What problem it solves

RevEd is an education platform. This backend is the AI layer behind it: it answers student questions grounded in actual curriculum content (not hallucinated), generates teaching materials for teachers, explains topics to parents in plain language, and gives admins usage/content analytics — all backed by a RAG (retrieval-augmented generation) pipeline over a Pinecone-indexed textbook/curriculum corpus, with Groq as the LLM.

The **frontend is a separate project** (built in Lovable) that consumes this API. This repo is backend-only — no UI.

---

## 2. Architecture at a glance

```
Frontend (Lovable)
   │  Supabase JWT (Authorization: Bearer ...)
   ▼
FastAPI app (main.py)
   ├─ CORS → RequestLog → Language → RequestId → RateLimit  (middleware stack)
   ├─ /api/v1/{student,teacher,parent,admin,notifications,webhooks}/*
   │
   ├─ Auth: decode Supabase JWT → resolve role from custom claim (app/core/security.py)
   ├─ Guardrails: role-boundary + hallucination + content filters (app/guardrails/)
   ├─ RAG pipeline (app/rag/): embed query → Pinecone retrieval → rerank → build prompt
   ├─ LLM client (app/llm/): Groq chat completions, streaming + non-streaming
   └─ Services (app/services/{student,teacher,parent,admin}/): business logic per role

Postgres (via Supabase)        Pinecone                Groq
 - users/roles mirror            - curriculum chunk       - llama-3.3-70b-versatile
 - conversations, goals,           vectors (768-dim,          (chat completions,
   generations, notifications      cosine, bge-base            streaming + JSON)
   study groups, webhooks          embeddings)
```

---

## 3. Auth model

- **No login/signup/token endpoints here.** All authentication is delegated to Supabase Auth on the frontend side; this backend only ever verifies an already-issued JWT.
- **Role resolution:** the backend reads a `user_role` custom claim from the JWT (set via a Supabase **Custom Access Token Hook**, `reved_access_token_hook`, that copies the role from the app's `user_roles` table into the token at mint time). Priority order: top-level `user_role`/`reved_role` claim → `app_metadata.role` → `user_metadata.role` → falls back to `"student"` if none present.
- **Two modes**, controlled by `AUTH_ENABLED`:
  - `false` (dev only): every request auto-authenticates as a stub user; an `X-Dev-Role` header can override the stub's role for local testing. The app **refuses to start** with this off if `ENVIRONMENT` is `production`/`staging`.
  - `true` (production): every request needs a valid `Authorization: Bearer <supabase_jwt>`; missing/invalid → `401`.
- Verification is **HS256 with a static shared secret** (`SUPABASE_JWT_SECRET`, from Supabase's *Legacy JWT Secret*) — not the newer asymmetric JWT Signing Keys scheme.
- Role gates use an allow-list (`require_role("teacher", "admin")` = either role, not both) — admins are treated as super-users who can act on behalf of any role.

**Current live status:** `AUTH_ENABLED=true`, `ENVIRONMENT=production` on Render. The `reved_access_token_hook` is installed and enabled in Supabase (confirmed via dashboard), and the `SUPABASE_JWT_SECRET` values are confirmed matching between Supabase and Render.

---

## 4. API surface (by role)

Every path is relative to `/api/v1`. All non-streaming responses use one envelope:

```json
{ "status": "success" | "error", "data": {...} | null, "message": "...", "role": "student" }
```

| Area | Endpoints |
|---|---|
| **Health** | `GET /health` (liveness), `GET /health/ready` (DB + cache readiness) |
| **Student** | `POST /ask` (+ `/ask/stream`), `GET /conversations`, `GET /conversations/{id}/history`, `POST /learning-path`, `POST /career-guidance`, `POST /goals`, `GET /goals/{student_id}`, `PATCH /goals/{goal_id}/progress`, `POST /study-groups` (+ `/join`, browse, `/facilitate`), `GET /generations` (+ `/{id}`) |
| **Teacher** | `POST /lesson-notes` (+ `/stream`), `POST /generate-content` (OpenAI-style SSE), `POST /quiz`, `POST /student-feedback`, `GET /class-progress`, `GET /generations` (+ `/{id}`) |
| **Parent** | `POST /explain-topic` (+ `/stream`), `GET /child-activity`, `GET /generations` (+ `/{id}`) |
| **Admin** | `POST /teachers/setup`, `POST /parents/setup`, `POST /classes/{id}/roster`, `GET /usage-summary`, `GET /content-stats`, `POST /notifications` |
| **Cross-role** | `GET /notifications`, `PATCH /notifications/{id}/read`, `PATCH /notifications/mark-all-read` |
| **Webhooks (admin)** | `POST /webhooks/subscriptions`, `GET /webhooks/subscriptions`, `DELETE /webhooks/subscriptions/{id}` |

Two SSE grammars are in play:
1. **RevEd `meta`/`chunk`/`done`** custom event stream — used by `/student/ask/stream`, `/teacher/lesson-notes/stream`, `/parent/explain-topic/stream`.
2. **OpenAI-style chat-completions SSE** (`data: {"choices":[{"delta":{...}}]}`, terminated by `data: [DONE]`) — used only by `/teacher/generate-content`.

First-token/full-response latency can be 15–60s on cold model load; every AI call needs a loading state on the client.

There is deliberately **no generic CRUD** for users/students/teachers/parents — those rows are only created via `/admin/*/setup` and read back through role-scoped aggregate endpoints (`class-progress`, `child-activity`, etc.), not a REST resource collection.

---

## 5. RAG pipeline (`app/rag/`)

- **Ingestion** (`ingestion/`): loads source docs (PyMuPDF for PDFs, OCR via pytesseract/pdf2image for scans), chunks text, classifies/tags by subject and content type, tracks what's been ingested.
- **Embedding** (`embedding/`): local sentence-transformers (`BAAI/bge-base-en-v1.5`, 768-dim) by default, or HuggingFace's hosted API (`EMBEDDING_BACKEND=hf_api`).
- **Vector store** (`vectorstore/`): Pinecone index management, upsert, indexing.
- **Retrieval** (`retrieval/`): query embedding → Pinecone similarity search (cosine) → filtering (by subject/role) → reranking.
- **Query engine** (`query_engine/`): routes a request to the right prompt/retrieval strategy and drives the grounded-answer generation (streaming and non-streaming), producing an answer plus cited sources (`AnswerSource`: file, subject, chunk id/index).

## 6. Guardrails (`app/guardrails/`)

Applied around every grounded generation:
- **Role validator** — rejects cross-role requests before they hit the LLM (e.g., a student asking for a full teacher lesson plan) and checks LLM output didn't drift into another role's voice.
- **Content filter** — strips "according to the text/passage/material" style meta-language and internal debug language from student-facing answers.
- **Hallucination checker** — flags large verbatim overlap between the answer and retrieved chunks (signal for over-quoting vs. actually explaining).
- **Output formatter** — normalizes formatting (removes stray numbered lists, bolded section labels the model sometimes adds despite instructions).

If a streamed answer trips a guard, the client is expected to discard the streamed text and render the guard's fallback (`StreamDone.was_modified_by_guard`).

## 7. LLM integration (`app/llm/`)

- **`client.py`** — Groq chat-completions client (default model `llama-3.3-70b-versatile`), used both for the main grounded answer and a cheaper "preflight" check.
- **`streaming.py`** — streaming response handling shared by the RevEd-grammar SSE endpoints.
- **`fallback.py`** — fallback behavior when the primary model call fails.

## 8. Data model (`app/models/`, Postgres via SQLAlchemy)

Core tables: `School`, `Teacher`, `Parent`, `Student` (+ `StudentClassMembership`), `Class`, chat `Conversation`/turns, `Goal`, `StudyGroup`, `AIGeneration` (student-facing) / `TeacherGeneration`, `Notification`, `WebhookSubscription`/`WebhookDelivery`. Admin provisioning (`/admin/teachers/setup`, `/admin/parents/setup`) is what actually creates the `Teacher`/`Parent`/`Class`/`Student` rows that role-scoped aggregate endpoints depend on — without provisioning, `class-progress` falls back to a global (non-class-scoped) view and `child-activity` returns an empty list.

Migrations are managed by Alembic (`app/db/migrations/versions`); always run `alembic upgrade head` before deploying new app code.

## 9. Cross-cutting infrastructure

- **Rate limiting** — `slowapi`, per-caller-key, tiered by subscription tier (`free`/`premium`/`unlimited`), Redis-backed in production (in-memory per-worker fallback otherwise — wrong with >1 worker).
- **Caching** — pluggable backend (`memory` or `redis`), used for response caching.
- **i18n** — `Accept-Language`-driven localization of **error messages only** (`en`/`fr` today).
- **Secrets** — pluggable loader (`env`/`aws`/`gcp`/`vault`) via `app/core/secrets.py`; `groq_api_key`, `pinecone_api_key`, `supabase_jwt_secret` are already migrated to it.
- **Logging** — structured JSON on stdout, correlated by `request_id`/`trace_id`/`span_id`; a dedicated `reved.audit` logger for auth/role/admin events and `reved.access` for one line per HTTP request.
- **Observability** — Prometheus `/metrics` (via `prometheus-fastapi-instrumentator`) with a shipped Grafana dashboard + alert rules; OpenTelemetry tracing (FastAPI/SQLAlchemy/httpx/asyncpg auto-instrumented, exporter swappable via `OTEL_EXPORTER`).
- **Webhooks** — outbound event delivery (`notification.created`, `generation.completed`, `goal.achieved`) to admin-registered subscriber URLs, HMAC-signed; delivered by a separate long-running dispatcher process (`scripts/webhook_dispatcher.py`), not the API process itself.

## 10. Deployment

- Single Docker image (`docker/Dockerfile`), built and pushed to GHCR by CI on every push to `main`.
- Health probes: `GET /health` (liveness, never touches DB) and `GET /health/ready` (readiness — gates on Postgres; Redis-down degrades but doesn't fail the probe).
- Startup hard-fails (refuses to bind) if `ENVIRONMENT` is production-like and `AUTH_ENABLED=false`, or if `AUTH_ENABLED=true` and `SUPABASE_JWT_SECRET` is missing — this is a deliberate safety rail, not a bug.
- Migrations must run **before** deploying new app code; the additive-then-destructive-across-two-deploys convention keeps single-step rollback safe.
- Currently deployed on **Render** (`srv-d9gidcvlk1mc73fs5cp0`), `ENVIRONMENT=production`, `AUTH_ENABLED=true` as of this integration pass.

---

## 11. Current integration status (as of this work)

The frontend (Lovable) originally consumed only 2 of ~40 endpoints (`/teacher/generate-content`, `/student/ask`, plus the health badge). An integration plan is underway to wire the rest, phased:

- **Phase A** (client foundation): shared SSE reader for the `meta`/`chunk`/`done` grammar, generated TS types from `/openapi.json`, centralized error handling — **in progress**.
- **Decisions locked in:** backend is the source of truth for goals/study groups (not Supabase tables); admin provisioning should move to `/admin/*/setup` early (Phase A/B) rather than late, since class-progress and child-activity structurally depend on it; build order is student tutor streaming first (de-risks the shared SSE grammar for teacher/parent streaming too), then teacher quiz/feedback.
- **Auth hardening — done:** `reved_access_token_hook` installed and enabled in Supabase; `SUPABASE_JWT_SECRET` confirmed matching between Supabase and Render; `AUTH_ENABLED` flipped to `true` in production.
- **Open issue under investigation:** `POST /teacher/generate-content` (Curriculum Studio page) was tested post-auth-flip and the UI spun and then silently stopped with no error or output. Root cause not yet isolated — pending a Network-tab capture (status code, whether `content-type: text/event-stream` came back, whether any bytes streamed) and a Render log check for the same request window, to determine whether this is a frontend stream-handling gap or a backend failure mid-stream (e.g., the Groq call failing after SSE headers were already sent, which can't surface as a normal HTTP error status).

---

## 12. Key docs in this repo

| Doc | Covers |
|---|---|
| `DEPLOY.md` | Full deployment runbook: env vars, secrets rotation, migrations, rollback, CI, observability, load testing |
| `FRONTEND_INTEGRATION.md` / `FRONTEND_HANDOFF.md` | Full API contract for frontend engineers — auth, envelope, every endpoint, SSE grammars |
| `WEBHOOKS.md` | Webhook subscriber contract, retry policy, event catalog |
| `SECURITY-REVIEW.md` | Security review findings |
| `.env.example` | Canonical environment variable list |
