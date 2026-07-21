# RevEd Backend — Production Readiness Review

**Date:** 2026-05-16
**Repo audited:** `RedEd/reved-llm-backend` (FastAPI + SQLAlchemy async + Supabase + Groq + Pinecone)
**Secondary scaffold:** `reved-physics-mvp` — empty boilerplate, no Python code; archive or delete.

---

## TL;DR

**Overall: BETA-READY, NOT PRODUCTION-READY.**

| Question you asked | Answer |
|---|---|
| 1. Supports school admins, teachers, students, parents? | **Yes — all four roles modeled.** Tenant (school) structure exists, but query-level isolation is not enforced. |
| 2. Production-safe for many concurrent users? | **No — 5 critical blockers** (empty Dockerfile, no rate limiting, AUTH_ENABLED defaults to false, no resource-ownership checks, school_id not enforced in queries). |
| 3. Frontend dev can start plugging endpoints? | **Yes.** `FRONTEND_INTEGRATION.md` is accurate, Swagger at `/docs`, consistent response envelope, `/api/v1` versioned, CORS configurable. Start in dev mode with `X-Dev-Role` header. |
| 4. What else to add? | See §7 (Critical Blockers) and §8 (Nice-to-haves). |

Estimated work to be production-ready: **~4–6 weeks** of focused backend hardening.

---

## 1. Architecture Overview

- **Framework:** FastAPI 0.115+, SQLAlchemy async ORM, PostgreSQL via Supabase.
- **Purpose:** Role-aware grounded Q&A and tutoring API. Students ask questions; teachers generate lesson notes/quizzes/feedback; parents get child summaries and plain-language explanations; admins provision users and monitor usage.
- **Integrations:** Groq LLM (`llama-3.3-70b`), Pinecone vector DB (RAG), HuggingFace embeddings, Supabase Auth (JWT).
- **Layout:** `app/api/routes/`, `app/core/`, `app/db/`, `app/models/`, `app/schemas/`, `app/services/`, `app/guardrails/`. DI via cached singletons in `app/api/dependencies.py`.
- **Migrations:** Alembic; 7 versioned revisions present.

---

## 2. User Roles & Multi-Tenancy

### Roles modeled in code

| Role | Model file | Notes |
|---|---|---|
| Student | [student.py:50](RedEd/reved-llm-backend/app/models/student.py) | `id`, `supabase_user_id`, `school_id` (FK), `parent_id` (FK), `grade_level` |
| Teacher | [teacher.py:18](RedEd/reved-llm-backend/app/models/teacher.py) | `school_id` (FK), `subject_specialty`; related to `SchoolClass` |
| Parent | [parent.py:14](RedEd/reved-llm-backend/app/models/parent.py) | One-way link: backref is `Student.parent_id`; no `parent.students` collection |
| Admin | [migrations/.../5f6dd178f674:62](RedEd/reved-llm-backend/app/db/migrations/versions/5f6dd178f674_initial_schema.py) | Table exists, has `school_id` + `scope`, but **no ORM model file**; under-used |

### School/tenant structure

- `School` ([school.py:19](RedEd/reved-llm-backend/app/models/school.py)) is the top-level tenant.
- `SchoolClass` ([school.py:38](RedEd/reved-llm-backend/app/models/school.py)) is scoped to `school_id` with `teacher_id` and `grade_level`.
- `StudentClassMembership` joins students to classes.
- FKs use CASCADE/SET NULL properly (initial migration L71–L141).

### Parent ↔ Student linkage
Properly modeled via `Student.parent_id → Parent.id`. Service queries join correctly ([report_service.py:67](RedEd/reved-llm-backend/app/services/parent/report_service.py)).

### Teacher ↔ Class ↔ Student linkage
`Teacher.id → SchoolClass.teacher_id → StudentClassMembership.class_id → Student.id`. Rosters managed via `POST /admin/classes/{id}/roster`.

### ⚠️ Gaps — tenant enforcement

- Role gating works (router-level `require_role()` — see §3).
- **`school_id` is NOT enforced in service-layer queries.** Endpoints fetch by `student_id`/`class_id`/`generation_id` but don't verify the resource's `school_id` matches the caller's `school_id`. If a teacher in School A is given (or guesses) a UUID belonging to School B, current code does not block the read.
- **No resource-ownership checks.** E.g., `GET /teacher/generations/{id}` does not verify the generation's `user_id` matches the caller. A teacher can read another teacher's generations by UUID.
- **Admin scope not validated.** `/admin/teachers/setup` doesn't confirm the admin belongs to the school they're provisioning.

These are real multi-tenancy holes. They don't matter while you're testing internally; they will matter the moment two real schools share the system.

---

## 3. Authentication & Authorization

### Auth mechanism
Supabase JWT (HS256). Decoded in [security.py:96](RedEd/reved-llm-backend/app/core/security.py). Role pulled from `app_metadata.role` or `user_metadata.role`; defaults to `student`.

### Dev mode
`AUTH_ENABLED=false` ([config.py:191](RedEd/reved-llm-backend/app/core/config.py)) returns a stub user; `X-Dev-Role` header lets you switch roles. Great for frontend dev. **Dangerous if it leaks to prod** — see Blocker #3 below.

### Password handling
Delegated to Supabase. Backend stores no passwords. Correct for a headless API.

### RBAC enforcement
Router-level `require_role(...)` ([security.py:165](RedEd/reved-llm-backend/app/core/security.py)) is applied consistently:
- `app/api/routes/student.py:67` — student-only
- `app/api/routes/teacher.py:55` — teacher + admin
- `app/api/routes/admin.py:41` — admin-only

### ⚠️ Gaps
1. **No resource-level ownership checks** (cross-user leakage by UUID).
2. **No school-level filter** at the query layer (cross-tenant leakage by UUID).
3. **No token revocation/blacklist** — compromised JWTs valid until Supabase expiry.
4. **No audit log** for auth events (failed JWT, role mismatch, admin actions).
5. **`AUTH_ENABLED=false` by default** — if `.env` isn't set in prod, the API is open. No startup warning.

---

## 4. Production Readiness

### ✅ Strong

| Area | Status | Notes |
|---|---|---|
| DB & migrations | Excellent | Alembic, 7 revisions, FKs, indexes, async SQLAlchemy, proper pool size for Supabase Supavisor ([session.py:25](RedEd/reved-llm-backend/app/db/session.py)) |
| Error handling | Excellent | Central handlers ([error_handlers.py:48](RedEd/reved-llm-backend/app/api/error_handlers.py)); consistent envelope; typed `RevEdError` hierarchy |
| Config | Good | Env-driven via `python-dotenv`; required keys validated at startup ([config.py:25](RedEd/reved-llm-backend/app/core/config.py)) |
| CORS | Configured | `CORS_ALLOWED_ORIGINS` env-driven ([config.py:148](RedEd/reved-llm-backend/app/core/config.py), [main.py:42](RedEd/reved-llm-backend/main.py)) |
| Health checks | Present | `GET /api/v1/health` (liveness) and `GET /api/v1/health/ready` (DB readiness) |
| Tests | Partial | 17 test files; unit + some integration. `tests/conftest.py` is empty — fixtures likely incomplete |

### ❌ Missing / weak

| Area | Status | Impact |
|---|---|---|
| Docker | **Empty** | `docker/Dockerfile` is 0 bytes; `docker-compose.yml` empty. **Cannot deploy.** |
| Rate limiting | **None** | No `slowapi` or equivalent. `/student/ask` can be spammed → burn Groq credits |
| Structured logging | None | `logging.basicConfig` only; no JSON, no trace IDs, no request/response logs |
| Observability | None | No Prometheus metrics, OTEL traces, profiling |
| Secrets manager | None | `.env` only; no Vault/AWS SM/GCP SM integration |
| CI/CD | None | No GitHub Actions / GitLab CI / deployment runbook |
| E2E tests | None | No full user flows tested end-to-end |
| Load tests | None | No verification of concurrent-user behavior |

---

## 5. Frontend Integration Readiness

### ✅ Ready to plug in

- **Versioned URL:** all routes under `/api/v1`.
- **OpenAPI/Swagger:** auto-generated at `/openapi.json` and `/docs`.
- **Consistent envelope:** every response is `{status, data, message, role}` ([common.py:17](RedEd/reved-llm-backend/app/schemas/common.py)).
- **Error codes:** machine-readable; map cleanly to HTTP status.
- **Dev auth:** `AUTH_ENABLED=false` + `X-Dev-Role: teacher|student|parent|admin` lets the frontend run without Supabase initially.
- **`FRONTEND_INTEGRATION.md` is accurate.** Spot-checked against:
  - `POST /student/ask` ([student.py:70](RedEd/reved-llm-backend/app/api/routes/student.py)) ✅
  - `POST /teacher/lesson-notes` ([teacher.py:59](RedEd/reved-llm-backend/app/api/routes/teacher.py)) ✅
  - `GET /parent/child-activity` ([parent.py:71](RedEd/reved-llm-backend/app/api/routes/parent.py)) ✅
  - `POST /admin/teachers/setup` / `POST /admin/parents/setup` ([admin.py:48](RedEd/reved-llm-backend/app/api/routes/admin.py)) ✅
  - Dev-mode behavior matches [security.py:134](RedEd/reved-llm-backend/app/core/security.py) ✅

### Endpoint inventory (≈39 endpoints)

| Area | Endpoints | Count |
|---|---|---|
| Student | ask, conversations (list/history), learning-path, career-guidance, goals (CRUD), study-groups (CRUD + join + facilitate), generations (list/detail) | 14 |
| Teacher | lesson-notes, quiz, student-feedback, class-progress, generations (list/detail) | 8 |
| Parent | explain-topic, child-activity, generations (list/detail) | 4 |
| Admin | teachers/setup, parents/setup, classes/{id}/roster, usage-summary, content-stats, notifications:create | 6 |
| Notifications | list, mark-read, mark-all-read | 3 |
| Health | /api/v1/health, /api/v1/health/ready | 2 |

### ⚠️ Frontend caveats

- **No streaming** — long LLM responses come back in one payload; need loading UI, not progress UI.
- **No file upload** — corpus ingestion is a server-side script.
- **No push notifications** — frontend must poll `/notifications`.
- **Limit-only pagination** — no cursor pagination yet; fine for MVP, will hurt at scale.

**Verdict:** Frontend can start now. Use dev mode. Don't ship to real users until §7 blockers are resolved.

---

## 6. The Other Scaffold (`reved-physics-mvp`)

Empty boilerplate. `apps/api/` has no Python files; `services/` subfolders empty. Archive or delete to avoid confusion.

---

## 7. Critical Blockers (must-fix before production)

Ranked by severity.

### 🔴 BLOCKER 1 — Empty Docker setup
- `docker/Dockerfile` is 0 bytes. Cannot build or deploy.
- **Fix:** Write Dockerfile (python:3.11-slim, install requirements, `uvicorn main:app --host 0.0.0.0 --port 8000`), populate `docker-compose.yml` with Postgres, add `.dockerignore`.
- **Effort:** ~3h.

### 🔴 BLOCKER 2 — No rate limiting
- LLM-backed endpoints (`/student/ask`, `/teacher/lesson-notes`, etc.) have no per-user/IP limits. A malicious or buggy client can burn through Groq credits.
- **Fix:** Add `slowapi` with Redis backend; e.g., 10 req/min per user_id on LLM endpoints, 60 req/min for reads.
- **Effort:** ~6h.

### 🔴 BLOCKER 3 — `AUTH_ENABLED` defaults to `false`
- [config.py:191](RedEd/reved-llm-backend/app/core/config.py). If prod `.env` is misconfigured, API is wide open.
- **Fix:** Either default to `true`, or add a startup assertion that fails fast when `ENVIRONMENT=production` and `AUTH_ENABLED=false`.
- **Effort:** 30min.

### 🔴 BLOCKER 4 — No resource-ownership checks
- `GET /teacher/generations/{id}`, student goals, study-group operations all accept UUIDs without verifying the caller owns the resource. Cross-user data leakage.
- **Fix:** Add `user_id` check in every service `get_by_id` / `update` / `delete` method. Add negative tests (caller A cannot access caller B's resource).
- **Effort:** ~12h across services.

### 🔴 BLOCKER 5 — No `school_id` enforcement in queries
- Multi-tenant model exists but service queries don't filter by `school_id`. Cross-school leakage by UUID.
- **Fix:** Pass caller's `school_id` into every service method that touches students/teachers/classes; add `WHERE school_id = :caller_school_id`. Add cross-school negative tests.
- **Effort:** ~16h.

### 🟠 HIGH 6 — Confirm `.env.example` is committed
- `FRONTEND_INTEGRATION.md` instructs `cp .env.example .env`. Verify it's tracked in git with placeholders for every required key.
- **Effort:** 15min.

### 🟠 HIGH 7 — No deployment runbook
- No `DEPLOY.md`, no CI/CD config. SRE will reverse-engineer.
- **Fix:** `DEPLOY.md` covering Supabase setup, Groq/Pinecone keys, `alembic upgrade head`, Docker build/push, health checks, log/alert wiring.
- **Effort:** ~6h.

### 🟡 MEDIUM 8 — No auth audit logging
- No structured log for failed JWT validation, role mismatches, admin actions. Forensics-blind.
- **Fix:** JSON logs for auth events with user_id, role, endpoint, outcome.
- **Effort:** ~4h.

### 🟡 MEDIUM 9 — `tests/conftest.py` is empty
- Likely missing fixtures (test DB, mocked LLM clients, FastAPI TestClient). Tests may be fragile.
- **Fix:** Populate conftest with shared fixtures; verify CI runs the full suite.
- **Effort:** ~4h.

### 🟡 MEDIUM 10 — No secrets manager
- `.env` only. Rotation/audit harder.
- **Fix:** Optional Vault/AWS-SM/GCP-SM loader. Document in `DEPLOY.md`.
- **Effort:** ~6h.

**Total blocker effort: ~57h ≈ 1.5–2 engineer-weeks of focused work**, plus 2–3 weeks of integration testing and QA.

---

## 8. Nice-to-haves (post-production)

1. Structured JSON logging with trace IDs (`python-json-logger`).
2. OpenTelemetry instrumentation (Groq, Pinecone, DB).
3. Request/response middleware for latency + error visibility.
4. Prometheus `/metrics` endpoint.
5. Index review — `ChatMessage.conversation_id`, `AIGeneration.user_id+role`.
6. Redis cache for hot reads (rosters, conversations, child summaries).
7. Tiered rate limits (free vs. paid).
8. Webhooks / event bus instead of polled notifications.
9. Cursor pagination for all list endpoints.
10. HMAC-signed webhooks for future LMS integrations.
11. i18n for error messages and user-facing strings.
12. Streaming LLM responses (SSE) for `/student/ask`, `/teacher/lesson-notes`.

---

## 9. Recommended next steps

**Week 1 — unblock frontend integration**
- Confirm `.env.example` committed (Blocker 6).
- Fix `AUTH_ENABLED` default + prod assertion (Blocker 3).
- Frontend starts wiring endpoints using `X-Dev-Role`.

**Weeks 2–3 — security hardening**
- Resource-ownership checks (Blocker 4).
- `school_id` enforcement everywhere (Blocker 5).
- Audit logging (Blocker 8).
- Populate `conftest.py` + add cross-user / cross-school negative tests (Blocker 9).

**Week 4 — operational readiness**
- Dockerfile + compose + `.dockerignore` (Blocker 1).
- Rate limiting via `slowapi` + Redis (Blocker 2).
- `DEPLOY.md` + CI pipeline (Blocker 7).
- Structured logging baseline.

**Weeks 5–6 — pre-launch validation**
- Load test (k6 or Locust) for concurrent students + teachers.
- End-to-end tests for the four-role flows.
- Pen-test pass focused on cross-tenant access.
- Observability: `/metrics`, dashboards, alerts.

After this sequence, the backend is production-ready for multi-school, multi-role usage.

---

## Status summary

| Dimension | Status |
|---|---|
| Multi-tenancy (structure) | ✅ Designed |
| Multi-tenancy (enforcement) | ❌ Not enforced in queries |
| Four-role support | ✅ All present |
| Auth | ✅ JWT, ⚠️ insecure default |
| RBAC (route level) | ✅ Working |
| RBAC (resource level) | ❌ Missing |
| DB & migrations | ✅ Excellent |
| Error envelope | ✅ Excellent |
| CORS | ✅ Configured |
| Rate limiting | ❌ Missing |
| Docker | ❌ Empty |
| CI/CD | ❌ None |
| Logging | ⚠️ Basic |
| Observability | ❌ None |
| Tests | ⚠️ Partial |
| API docs | ✅ Excellent |
| **Frontend can start now** | ✅ Yes (dev mode) |
| **Production-ready** | ❌ Not yet |

— End of review.
