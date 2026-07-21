# Frontend Integration — Handoff Note

**Status:** ✅ Ready for frontend integration in **dev mode**.
**Companion doc:** [`FRONTEND_INTEGRATION.md`](./FRONTEND_INTEGRATION.md) — the full endpoint reference. This file is the short orientation.

---

## Quick start (5 minutes)

1. **Clone the backend repo and install:**
   ```bash
   cd RedEd/reved-llm-backend
   cp .env.example .env
   # Fill in DATABASE_URL, GROQ_API_KEY, PINECONE_API_KEY at minimum
   pip install -r requirements.txt
   alembic upgrade head
   uvicorn main:app --reload
   ```
2. **Verify it's up:**
   ```bash
   curl http://localhost:8000/api/v1/health
   # → {"status":"success","data":{"status":"ok"},"message":"Service is healthy.","role":"system"}
   ```
3. **Open Swagger:** http://localhost:8000/docs

---

## Base URL & versioning

- **Base URL (local):** `http://localhost:8000`
- **API prefix:** `/api/v1` — every endpoint lives under this prefix, including health.
- **Health checks:** `GET /api/v1/health` (liveness), `GET /api/v1/health/ready` (readiness + DB/cache check).

When deploying, expect the prod base URL to be passed via env (`VITE_API_BASE_URL` or equivalent). Do not hardcode.

---

## Auth — dev mode vs. prod

The backend has **two auth modes** controlled by `AUTH_ENABLED` in `.env`:

| Mode | When to use | What the frontend sends |
|---|---|---|
| **Dev (`AUTH_ENABLED=false`)** | Local development. **Default.** | `X-Dev-Role: student` (or `teacher` / `parent` / `admin`) — no token needed. |
| **Prod (`AUTH_ENABLED=true`)** | Staging and production. | `Authorization: Bearer <supabase-jwt>` |

**Important:** The backend now refuses to start if `ENVIRONMENT=production|staging|prod` while `AUTH_ENABLED=false`. So you cannot accidentally ship dev mode to prod.

### Dev-mode example
```http
POST /api/v1/student/ask
Content-Type: application/json
X-Dev-Role: student

{ "question": "What is Newton's first law?" }
```

### Prod-mode example
```http
POST /api/v1/student/ask
Content-Type: application/json
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...

{ "question": "What is Newton's first law?" }
```

---

## Response envelope — every response, every time

Both success and error responses follow this shape:
```json
{
  "status": "success" | "error",
  "data":   <object | array | null>,
  "message": "<human-readable, safe to display>",
  "role":   "student" | "teacher" | "parent" | "admin"
}
```

Error responses additionally include a machine-readable `code` (e.g., `"validation_error"`, `"not_found"`, `"role_violation"`, `"upstream_error"`, `"rate_limited"`).

Build one shared client function for unwrapping this envelope. Never assume the payload sits at the top level.

---

## CORS

Configured via `CORS_ALLOWED_ORIGINS` in the backend `.env`. Dev defaults now cover the common ports, including the RevEd web app's Vite server: `localhost:3000`, `localhost:8080`, `localhost:5173` (+ `127.0.0.1` variants). Set the env var explicitly for staging/prod (it replaces the defaults).

---

## What's stable vs. what's coming

### ✅ Stable — wire up freely
- All endpoints listed in `FRONTEND_INTEGRATION.md` §9–§14.
- Response envelope.
- Role gating (`X-Dev-Role` / Bearer; prod role via the `user_role` JWT claim — see §3.1).
- **Streaming** — `/student/ask/stream`, `/teacher/lesson-notes/stream`, `/parent/explain-topic/stream` (RevEd `meta`/`chunk`/`done` SSE) and `/teacher/generate-content` (OpenAI-style SSE).
- **`POST /teacher/generate-content`** — frontend-compatible markdown generator (numeric grade, free-text subject, all 5 content types). See `FRONTEND_INTEGRATION.md` §10.1b.
- **Cursor pagination** — every list carries `next_cursor` (§8a); `limit`-only still works.

### 🚧 Coming soon — design with this in mind
- **Rate limiting** (Phase 3, Blocker 2): 60 req/min default per user, 10 req/min on LLM endpoints. Plan for `429` responses with `code: "rate_limited"` — show a friendly toast and a short cooldown.
- **Resource-ownership errors** (Phase 2, Blocker 4): if you guess another user's UUID, you'll get a `404`. Don't show "forbidden" UI — treat as "not found."
- **Cross-school errors** (Phase 2, Blocker 5): same — `404` not `403`.
- **Audit logging** (Phase 2, Blocker 8): no frontend impact, just heads-up that admin actions get logged.

### ⏳ Not yet — don't depend on them
- **Push notifications** — pull-only; poll `GET /notifications` (e.g., every 30s while the app is foregrounded).
- **File uploads** — corpus ingestion is server-side only; no API for uploading PDFs etc.
- **Real-time collaboration** on lesson notes / quizzes — generations are single-author for now.

> First LLM response can still be 15–60s on cold model load — show a loading/typing state. Streaming endpoints exist (above) so you can render progressively.

---

## Endpoints by role — at a glance

| Role | Key endpoints | Count |
|---|---|---|
| **Student** | `/student/ask`, `/student/conversations`, `/student/learning-path`, `/student/career-guidance`, `/student/goals` (CRUD), `/student/study-groups` (CRUD + join), `/student/generations` | 14 |
| **Teacher** | `/teacher/generate-content` (markdown, FE-compatible), `/teacher/lesson-notes`, `/teacher/quiz`, `/teacher/student-feedback`, `/teacher/class-progress`, `/teacher/generations` | 9 |
| **Parent** | `/parent/explain-topic`, `/parent/child-activity`, `/parent/generations` | 4 |
| **Admin** | `/admin/teachers/setup`, `/admin/parents/setup`, `/admin/classes/{id}/roster`, `/admin/usage-summary`, `/admin/content-stats`, `/admin/notifications` | 6 |
| **Shared** | `/notifications` (list, mark-read, mark-all-read) | 3 |
| **Ops** | `/api/v1/health`, `/api/v1/health/ready` | 2 |

Full request/response shapes: `FRONTEND_INTEGRATION.md` §9–§14.
Auto-generated reference: http://localhost:8000/docs.

---

## Smoke test — runs in any role in <30 seconds

```bash
# Liveness
curl -s http://localhost:8000/api/v1/health | jq

# Student ask (dev mode)
curl -s -X POST http://localhost:8000/api/v1/student/ask \
  -H "Content-Type: application/json" \
  -H "X-Dev-Role: student" \
  -d '{"question":"What is gravity?"}' | jq

# Teacher lesson notes (dev mode) — structured JSON
curl -s -X POST http://localhost:8000/api/v1/teacher/lesson-notes \
  -H "Content-Type: application/json" \
  -H "X-Dev-Role: teacher" \
  -d '{"subject":"physics","topic":"Newton laws","student_class":"SS1"}' | jq

# Teacher content (dev mode) — frontend payload, OpenAI-style markdown stream
curl -sN -X POST http://localhost:8000/api/v1/teacher/generate-content \
  -H "Content-Type: application/json" \
  -H "X-Dev-Role: teacher" \
  -d '{"contentType":"lesson_plan","subject":"Science","gradeLevel":9,"topic":"Photosynthesis"}'

# Parent child activity (dev mode)
curl -s http://localhost:8000/api/v1/parent/child-activity \
  -H "X-Dev-Role: parent" | jq
```

If these return `"status":"success"` (or, for `/generate-content`, a stream of `data: {...}` lines ending in `data: [DONE]`), you're good to wire up the real UI.

---

## Who to ping

- Backend bugs / endpoint questions → backend lead.
- Schema or contract changes → request via PR to `FRONTEND_INTEGRATION.md` (it is the single source of truth).
- New endpoint requests → open an issue with proposed request/response shape; align before implementing.
