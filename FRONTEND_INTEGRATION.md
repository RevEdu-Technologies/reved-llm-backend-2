# RevEd LLM Backend — Frontend Integration Guide

This document is the contract between the RevEd backend and any frontend client
(web, mobile, admin console). It covers base URL, auth, the response envelope,
error handling, CORS, and every available endpoint with its request/response
shape and a runnable example.

The single source of truth for request/response types is the live OpenAPI
schema served at `/openapi.json`. **Generate your TypeScript types from that
file** — don't hand-maintain them. See §13 for the recommended workflow.

---

## 1. Base URL & environment

| Environment | Base URL |
|-------------|----------|
| Local dev   | `http://localhost:8000` |
| Staging     | _TBD — set when deployed_ |
| Production  | _TBD — set when deployed_ |

All endpoints are under the `/api/v1` prefix.

### Running the backend locally

```bash
# from the repo root
pip install -r requirements.txt
cp .env.example .env   # then fill in real keys
uvicorn main:app --reload --port 8000
```

Interactive API docs (auto-generated from the schema):
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

---

## 2. CORS

Set `CORS_ALLOWED_ORIGINS` in `.env` (comma-separated). Local defaults now
cover the common dev ports out of the box (including the RevEd web app's
Vite server on `:8080`):

```
http://localhost:3000,http://127.0.0.1:3000,
http://localhost:8080,http://127.0.0.1:8080,
http://localhost:5173,http://127.0.0.1:5173
```

For staging/production set `CORS_ALLOWED_ORIGINS` to your deployed frontend
origin(s) — the env var **replaces** the defaults, so include every origin
you need.

Allowed methods: `GET, POST, PUT, PATCH, DELETE, OPTIONS`. All headers
permitted. Credentials (cookies) allowed, though the recommended auth pattern
uses `Authorization: Bearer <jwt>`.

---

## 3. Authentication

Two modes, controlled by `AUTH_ENABLED`:

| `AUTH_ENABLED` | Behavior |
|----------------|----------|
| `false` (default in dev) | Every request is auto-authenticated as a stub user. Frontend can call any endpoint without a token. **`X-Dev-Role` header** flips the stub's role for testing. |
| `true` (production)      | Every request must include a valid Supabase JWT in `Authorization: Bearer <token>`. Missing/invalid → `401`. `X-Dev-Role` is **ignored**. |

### 3.1 Production: Supabase JWT

```ts
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);

const { data: { session } } = await supabase.auth.getSession();
const token = session?.access_token;
```

The backend resolves the RevEd role from the JWT in this priority order:

1. A **top-level custom claim** — `user_role` or `reved_role`.
2. `app_metadata.role` / `app_metadata.user_role`.
3. `user_metadata.role` / `user_metadata.user_role`.

Valid values: `student | teacher | parent | admin`. Default if none is
present: `student`.

**Important for this codebase:** the RevEd web app keeps a user's role in
the Supabase **`user_roles`** table (mirrored on `profiles.role`), *not* in
`app_metadata`. A plain Supabase access token therefore carries no RevEd
role the backend can see (its top-level `role` claim is always
`"authenticated"`), so everyone would resolve to `student`.

Bridge it with a Supabase **custom access token hook** that copies the role
into the `user_role` claim the backend reads (option 1 above):

```sql
-- supabase/migrations: register a custom access token hook
create or replace function public.reved_access_token_hook(event jsonb)
returns jsonb
language plpgsql
stable
as $$
declare
  claims jsonb := event->'claims';
  resolved_role text;
begin
  select role::text into resolved_role
  from public.user_roles
  where user_id = (event->>'user_id')::uuid
  order by case role
    when 'admin' then 0 when 'teacher' then 1
    when 'parent' then 2 else 3 end
  limit 1;

  if resolved_role is not null then
    claims := jsonb_set(claims, '{user_role}', to_jsonb(resolved_role));
  end if;

  return jsonb_set(event, '{claims}', claims);
end;
$$;
```

Then enable it under **Authentication → Hooks → Customize Access Token**
(or in `supabase/config.toml`). After that, every issued JWT carries
`user_role`, and the backend role gates (`/teacher/*`, `/admin/*`, …) work.
Alternatively, provision the role into `app_metadata.role` server-side with
the service-role key (option 2) — never set it from the client.

### 3.2 Dev mode: `X-Dev-Role` header

When `AUTH_ENABLED=false`, send the role as a request header:

```bash
curl -H "X-Dev-Role: teacher" http://localhost:8000/api/v1/teacher/class-progress
```

In Swagger UI, click **Authorize** and paste the role string (just `teacher`,
no `Bearer` prefix) into the `DevRole` field. It applies to every "Try it out"
call until you log out.

Role-scoped routes (e.g. `/teacher/*`) reject mismatched roles with `403`.

---

## 4. The RevEd response envelope

**Every endpoint returns the same envelope** — success and error alike.

```ts
type APIResponse<T> = {
  status: "success" | "error";
  data: T | ErrorDetails | null;
  message: string;
  role: "student" | "teacher" | "parent" | "admin" | "system";
};

type ErrorDetails = {
  code: string;
  details?: unknown;
};
```

### Success

```json
{
  "status": "success",
  "data": { "answer": "Photosynthesis is...", "conversation_id": "fbb4d55f-..." },
  "message": "Answer generated.",
  "role": "student"
}
```

### Error

```json
{
  "status": "error",
  "data": { "code": "validation_error", "details": { "errors": [ ... ] } },
  "message": "Your request contains invalid data.",
  "role": "student"
}
```

---

## 5. Error codes & HTTP status

| `code`                 | HTTP | When |
|------------------------|------|------|
| `validation_error`     | 422  | Body failed Pydantic validation. `details.errors` has per-field errors. |
| `not_found`            | 404  | Referenced resource (goal, conversation, generation) does not exist or isn't owned by the caller. |
| `role_violation`       | 403  | Query is outside the role's allowed domain. |
| `authentication_error` | 401  | Missing or invalid Bearer token (only when `AUTH_ENABLED=true`). |
| `authorization_error`  | 403  | Token valid but the user's role isn't allowed on this route. |
| `rate_limited`         | 429  | Caller exceeded the per-minute cap. Response carries a `Retry-After: 60` header. See §5.1. |
| `upstream_error`       | 503  | Groq / Pinecone / HuggingFace upstream failure. Render a "try again in a moment" UI. |
| `configuration_error`  | 503  | Server misconfigured. |
| `http_error`           | 4xx  | Generic HTTP error (e.g. 404 for unknown paths). |
| `internal_error`       | 500  | Unhandled server error. |

Render `message` to users; use `code` for branching. **`code` is
locale-independent — always branch on `code`, never on `message`.**

### 5.0 Localized messages (`Accept-Language`)

The `message` field is localized. Send a standard `Accept-Language` header and
the backend returns the error `message` in the best supported language
(currently **`en`** and **`fr`**), falling back to English for anything else:

```
Accept-Language: fr            → "Votre requête contient des données invalides."
Accept-Language: fr-FR,fr;q=0.9 → French
(no header / Accept-Language: de) → English
```

Quality weights are honored (`en;q=0.4, fr;q=0.9` → French). Only the
human-readable `message` is translated; `code`, `status`, and `data` are
identical across locales. Validation `details.errors` (Pydantic's per-field
text) stay English — branch on the field path, not its message.

### 5.1 Rate limits (per caller, tiered)

Every endpoint is capped at **60 requests/minute** per caller. The
LLM-backed endpoints (`POST /student/ask`, `/teacher/lesson-notes`,
`/teacher/quiz`, `/teacher/student-feedback`, `/parent/explain-topic`, and
their `/stream` variants) have a tighter cap that depends on the caller's
**subscription tier**:

| Tier        | LLM cap (default) |
|-------------|-------------------|
| `free`      | 10/minute         |
| `basic`     | 20/minute         |
| `premium`   | 60/minute         |
| `unlimited` | 1000/minute       |

(Operators can retune these per deploy via `RATE_LIMIT_LLM_TIERS`.)

**Where the tier comes from:**
- **Production** (`AUTH_ENABLED=true`): a verified JWT claim. Add a
  `subscription_tier` (or `tier`) custom claim alongside `user_role` in the
  same Supabase access-token hook (§3.1) — copy it from `schools.tier`. A
  token with no tier claim falls back to `free`. The tier is **advisory**:
  it only widens the LLM cap; it never grants extra authorization.
- **Dev** (`AUTH_ENABLED=false`): send an `X-Dev-Tier: premium` header
  (or set the `DevTier` field in Swagger's Authorize dialog) to exercise the
  paid caps locally.

**On a 429:** parse `Retry-After` (seconds) and show a soft "you're going a
bit fast — try again in a moment" message. The envelope's `code` is
`rate_limited`. Don't hard-fail the UI; the cap resets on the next window.

---

## 6. Supported subjects (canonical form)

All `subject` fields accept short forms and typos (auto-normalised), but the
**canonical** snake_case values used in responses and filters are:

```
biology, chemistry, physics,
mathematics, further_mathematics,
english_language, literature_in_english,
economics, government, civic_education,
commerce, accounting, office_practice,
computer, history, religious_studies,
hausa, igbo, yoruba
```

Inputs like `bio`, `math`, `econ`, `english`, `gov`, `crs`, `ICT`, `further maths`
are normalised by the backend.

---

## 7. `student_class` format

Validated against:

```
Primary 1–6  |  JSS1–3  |  SS1–3
```

Examples: `"Primary 5"`, `"JSS2"`, `"SS3"`. Case-insensitive. Anything else → `422`.

---

## 8. Health endpoints

| Path | Purpose |
|---|---|
| `GET /api/v1/health` | Liveness — 200 if process is up |
| `GET /api/v1/health/ready` | Readiness — DB + cache + model config |

---

## 8a. List pagination (cursor-based)

The following list endpoints support cursor pagination:

* `GET /notifications`
* `GET /student/generations`
* `GET /teacher/generations`
* `GET /parent/generations`

### Wire shape

Every paginated list response carries a `next_cursor` field on `data`:

```ts
type PaginatedList<TItem, TKey extends string> = {
  // ...the existing fields (e.g. `generations`, `notifications`, `unread_count`)
  next_cursor: string | null;   // null on the last page
};
```

Two query parameters control paging:

| Param | Default | Range | Description |
|---|---|---|---|
| `limit`  | 50 | 1–200 | Max rows per page. Clamped server-side. |
| `cursor` | (omitted) | opaque | Pass back the previous response's `next_cursor` to fetch the next page. |

### How to walk a list

```ts
let cursor: string | null = null;
const all: Item[] = [];
do {
  const url = new URL("/api/v1/teacher/generations", BASE_URL);
  url.searchParams.set("limit", "50");
  if (cursor) url.searchParams.set("cursor", cursor);
  const res = await fetch(url, { headers });
  const env = await res.json();
  all.push(...env.data.generations);
  cursor = env.data.next_cursor;
} while (cursor);
```

### Notes

* **The cursor is opaque.** Don't decode it, don't construct one client-side — its wire format is a backend implementation detail and may change without a versioned API bump.
* **Sort order is fixed at `created_at DESC`** (newest first). The cursor implicitly carries the sort position; you cannot reverse the order through query params.
* **Page boundaries are stable across paging** (cursors include a UUID tie-breaker), but rows newer than the first cursor will appear at the start on a re-walk — handle "row inserted while paging" the same way you'd handle any concurrent insert.
* **Back-compat:** the older `limit`-only contract still works. Omit `cursor` and the response shape matches the pre-N9 surface plus `next_cursor: null` (or a non-null value if there are >`limit` rows). Frontends can adopt cursor paging endpoint-by-endpoint.
* **Malformed cursors** return `400 { code: "validation_error" }` (treated like any client-side query error). Drop the cursor and refetch from the start.

---

## 9. Student endpoints (14)

Prefix `/api/v1/student`. Require `role=student` (or `admin`).

### 9.1 `POST /student/ask` — grounded multi-turn Q&A

```ts
type StudentQuestionRequest = {
  question: string;
  student_class: string;                  // Primary/JSS/SS
  subject?: string;                       // any subject from §6 (aliases OK)
  history?: { role: "user" | "assistant"; content: string }[];
  conversation_id?: string;               // UUID; resume a saved thread
  learning_state?: {
    understanding_level?: "low" | "medium" | "high";
    previous_attempt_correct?: boolean;
    attempt_count?: number;
  };
};

type StudentAnswerResponse = {
  status: "answered" | "needs_clarification";
  answer: string;                         // empty when status="needs_clarification"
  student_class: string;
  subject: string | null;                 // canonical snake_case
  original_question: string | null;       // populated when auto-corrected
  corrected_question: string | null;
  original_subject: string | null;
  clarifying_question: string | null;     // populated when status="needs_clarification"
  conversation_id: string;                // always returned; carry forward
};
```

**Multi-turn pattern**

Two modes, choose one:
1. **Client-side**: keep the `history` array in component state, append each turn, replay on every request.
2. **Server-side**: on first call omit `conversation_id`. The response carries one. On follow-ups send only `conversation_id` — the backend rehydrates history from `chat_messages`. This is what survives across sessions / device switches.

### 9.1a `POST /student/ask/stream` — same answer, streamed (SSE)

Same body schema, same role gate, same rate limit as `/student/ask`. Response is `text/event-stream` so the UI can render the answer as it's generated. Use this for the chat textarea; keep `/student/ask` for cases where you need the full structured payload in one shot (e.g. background generation).

**Event grammar.** Three event types, in this order:

| Event | Frequency | Payload | Use it for |
|---|---|---|---|
| `meta` | exactly 1, first | Everything in `StudentAnswerResponse` except `answer` (status, subject, conversation_id, did-you-mean fields, clarifying_question) | Render shell UI: subject pill, did-you-mean banner, clarifier card. If `status="needs_clarification"`, expect zero `chunk` events. |
| `chunk` | 0..N, after `meta` | `{ "text": string }` | Append `text` to the visible answer as each frame arrives. |
| `done` | exactly 1, last | `{ "final_answer": string \| null }` | When `final_answer` is non-null, **replace** the streamed text with this value — the guard layer modified the answer. When null, the streamed text is the final answer. |

An `error` event (single, terminal) may appear in place of `done` if the backend hits an unrecoverable failure mid-stream. Payload: `{ code: "stream_failed", message: string }` — render a toast and stop reading.

**Wire format.** Standard SSE: `event:` line, single-line `data:` JSON, blank line terminator. Frames are guaranteed not to contain raw newlines inside `data`. Example:

```
event: meta
data: {"status":"answered","student_class":"JSS2","subject":"biology","conversation_id":"7f...","original_question":null,"corrected_question":null,"original_subject":null,"clarifying_question":null}

event: chunk
data: {"text":"Plants "}

event: chunk
data: {"text":"convert sunlight..."}

event: done
data: {"final_answer":null}
```

**Browser snippet (EventSource doesn't support POST — use fetch + ReadableStream).**

```ts
async function askStream(req, { onMeta, onChunk, onDone, onError }) {
  const ctrl = new AbortController();
  const resp = await fetch(`${BASE_URL}/api/v1/student/ask/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(req),
    signal: ctrl.signal,
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by blank lines.
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const eventMatch = frame.match(/^event:\s*(.+)$/m);
      const dataMatch = frame.match(/^data:\s*(.+)$/m);
      if (!eventMatch || !dataMatch) continue;
      const data = JSON.parse(dataMatch[1]);
      const type = eventMatch[1];
      if (type === "meta") onMeta(data);
      else if (type === "chunk") onChunk(data.text);
      else if (type === "done") onDone(data.final_answer);
      else if (type === "error") onError(data);
    }
  }
  return () => ctrl.abort();   // cancellation handle
}
```

**Cancellation.** Call `controller.abort()` (or close the tab). The backend's tutor service catches the disconnect, closes the upstream Groq stream so we stop paying for tokens you won't receive, and skips persisting a partial answer. There's nothing the frontend needs to do beyond aborting the fetch.

**Persistence contract.** Same as `/ask` — `conversation_id` from the `meta` event should be carried into the next request. Server-side persistence happens *after* the stream completes; partial streams are not stored.

### 9.2 `GET /student/conversations` — list threads

```ts
type ConversationSummary = {
  conversation_id: string;
  subject: string | null;
  message_count: number;
  last_question_preview: string;          // first ~120 chars of latest Q
  started_at: string;                     // ISO datetime
  last_active_at: string;
};

type ConversationListResponse = { conversations: ConversationSummary[] };
```

Renders a "Recent chats" sidebar.

### 9.3 `GET /student/conversations/{id}/history` — replay turns

```ts
type ConversationHistoryResponse = {
  conversation_id: string;
  turns: { role: "user" | "assistant"; content: string }[];
};
```

When the user opens an old thread, fetch the history and hydrate the chat UI.

### 9.4 `POST /student/learning-path` — personalized pathway

```ts
type LearningPathRequest = {
  student_class: string;
  subject: string;
  topic: string;                          // min length 2
  current_understanding?: "low" | "medium" | "high";
  weekly_study_hours?: number;            // 1..40
};

type LearningPathResponse = {
  topic: string;
  subject: string;
  student_class: string;
  overview: string;
  steps: { order: number; title: string; focus: string; suggested_activity: string; estimated_hours: number }[];
  encouragement: string;
};
```

### 9.5 `POST /student/career-guidance`

```ts
type CareerGuidanceRequest = {
  student_class: string;
  favorite_subjects: string[];            // 1..10
  strengths?: string[];                   // 0..10
  interests?: string[];                   // 0..10
  long_term_dream?: string;
};

type CareerGuidanceResponse = {
  student_class: string;
  overview: string;
  suggestions: { career: string; why_it_fits: string; recommended_subjects: string[]; next_steps: string[] }[];
  encouragement: string;
};
```

### 9.6 Goals

| Method | Path | Body |
|---|---|---|
| `POST` | `/student/goals` | `{ student_id, title, description?, subject?, target_date? }` |
| `GET` | `/student/goals/{student_id}` | — |
| `PATCH` | `/student/goals/{goal_id}/progress` | `{ progress_percent, note? }` |

### 9.7 Study groups

| Method | Path | Body |
|---|---|---|
| `POST` | `/student/study-groups` | `{ creator_student_id, name, subject, topic, student_class }` |
| `POST` | `/student/study-groups/{group_id}/join` | `{ student_id }` |
| `GET` | `/student/study-groups?student_class=...&subject=...` | — |
| `POST` | `/student/study-groups/{group_id}/facilitate` | `{ focus_question }` |

### 9.8 Saved AI generations

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/student/generations?generation_type=&limit=&cursor=` | List learning-paths + career-guidance the student has generated. Response carries `next_cursor` — see §8a. |
| `GET` | `/student/generations/{id}` | Full request + response payload to re-render |

See §13 for the shared `AIGenerationSummary` and `AIGenerationDetail` shapes.

---

## 10. Teacher endpoints (8)

Prefix `/api/v1/teacher`. Require `role=teacher` (or `admin`).

Teacher generations run with `role=teacher` retrieval — the assistant draws on
both student-visible and teacher-only material (marking guides, etc.).

### 10.1 `POST /teacher/lesson-notes`

```ts
type LessonNotesRequest = {
  subject: string;
  student_class: string;
  topic: string;
  learning_objectives?: string[];
  duration_minutes?: number;              // 10..240
  include_examples?: boolean;             // default true
  conversation_id?: string;               // optional grouping for related generations
};

type LessonNotesResponse = {
  topic: string;
  subject: string;
  student_class: string;
  learning_objectives: string[];
  overview: string;
  sections: { heading: string; body: string; examples: string[] }[];
  teacher_tips: string[];
  misconceptions_to_address: string[];
  sources: string[];                      // source filenames the notes grounded on
  generation_id: string;                  // persisted; use with /teacher/generations/{id}
  conversation_id: string;
};
```

### 10.1a `POST /teacher/lesson-notes/stream` — same notes, streamed (SSE)

Same body schema, same role gate, same rate limit as `/teacher/lesson-notes`. Lesson notes return **structured JSON**, so streaming raw token-by-token means raw JSON characters land in `chunk` events. Two ways to consume:

1. **Ignore chunks; use `done.result`.** The terminal `done` event carries the full `LessonNotesResponse` (identical to what the non-streaming endpoint returns, including `generation_id`). Show a spinner / typing animation while chunks arrive, swap to the structured doc when `done` lands. **This is the recommended path.**
2. **Render chunks as a preview.** Stream the raw text into a "generating..." pane (it'll be JSON characters, not pretty). Replace the pane with the structured doc when `done` arrives. Lower-effort UX trade-off; only useful if a long generation needs more visible progress than a spinner.

| Event | Frequency | Payload | Use it for |
|---|---|---|---|
| `meta` | exactly 1, first | `{topic, subject, student_class, conversation_id}` | Render shell UI title + start the spinner. |
| `chunk` | 0..N | `{text: string}` | Optional preview; safe to ignore. |
| `done` | exactly 1, last | `{result: LessonNotesResponse}` | Render the structured doc. |
| `error` | replaces `done` | `{code: "stream_failed", message: string}` | Render a toast and stop. |

Wire format is identical to `/student/ask/stream` — see §9.1a for the browser fetch+ReadableStream snippet. Persistence (and `generation_id` minting) happens **before** `done` is emitted so the frontend doesn't need a follow-up round trip to discover the saved generation.

### 10.1b `POST /teacher/generate-content` — markdown materials (OpenAI-style SSE)

A **frontend-compatible** content generator that accepts the same payload
the web app already sends to its `generate-lesson-content` function and
streams the result as an **OpenAI-style** SSE response — the drop-in
replacement for the old Supabase/Lovable function, now grounded in the
teacher corpus via RAG.

```ts
type TeacherContentRequest = {
  contentType: "lesson_plan" | "quiz" | "notes" | "slides" | "study_guide";
  subject: string;                         // free-text OK ("Science", "Mathematics") — normalised, never 422
  gradeLevel: number | string;             // 1-6 Primary, 7-9 JSS, 10-12 SS; or "JSS2"
  topic: string;                           // min length 2
  learningObjectives?: string;             // free text
  difficultyLevel?: "beginner" | "intermediate" | "advanced";  // default "intermediate"
  curriculumStandard?: string;             // e.g. "WAEC", "NERDC"
  tone?: "professional" | "engaging" | "simplified";           // default "engaging"
};
```

**Response wire format.** Standard OpenAI chat-completions stream — *not*
the RevEd envelope and *not* the `meta/chunk/done` grammar:

```
data: {"choices":[{"delta":{"content":"# Lesson"},"index":0}]}

data: {"choices":[{"delta":{"content":" Plan\n..."},"index":0}]}

data: [DONE]
```

On failure mid-stream a single `data: {"error":"..."}` line is emitted in
place of `[DONE]`. The body is one markdown document; concatenate every
`choices[0].delta.content` to assemble it.

This is exactly what the web app's existing `teacherMaterialsService
.generateAIContent` parser expects — so adopting it is just a URL + auth
swap (see the frontend handoff doc). `gradeLevel` is mapped to the
canonical `student_class` server-side; `subject` is normalised to the
canonical taxonomy (umbrella values like "Science" fall back to general
retrieval rather than failing).

> Note: this endpoint streams raw markdown and does **not** persist a
> `generation_id` (the web app saves the artefact to its own store). Use
> the structured `/teacher/lesson-notes` and `/teacher/quiz` endpoints when
> you want a persisted, re-renderable artefact with sources + `generation_id`.

### 10.2 `POST /teacher/quiz`

```ts
type QuizRequest = {
  subject: string;
  student_class: string;
  topic: string;
  num_questions?: number;                 // 3..30, default 10
  difficulty_mix?: { easy?: number; medium?: number; hard?: number };
  question_types?: ("mcq" | "short_answer" | "numeric" | "derivation")[];
  conversation_id?: string;
};

type QuizQuestion = {
  question_number: number;
  question: string;
  question_type: string;
  difficulty: "easy" | "medium" | "hard";
  options: string[] | null;               // 4 strings for MCQ; null otherwise
  marking_guide: string;                  // teacher-only
  points: number;
};

type QuizResponse = {
  topic: string; subject: string; student_class: string;
  questions: QuizQuestion[];
  total_points: number;
  suggested_duration_minutes: number | null;
  sources: string[];
  generation_id: string;
  conversation_id: string;
};
```

### 10.3 `POST /teacher/student-feedback`

```ts
type FeedbackRequest = {
  subject: string;
  student_class: string;
  question: string;
  student_answer: string;
  rubric?: string;
  conversation_id?: string;
};

type FeedbackResponse = {
  overall_score_band: "excellent" | "good" | "fair" | "needs_improvement";
  summary: string;
  strengths: string[];
  areas_for_improvement: string[];
  specific_corrections: string[];
  next_steps: string[];
  generation_id: string;
  conversation_id: string;
};
```

### 10.4 `GET /teacher/class-progress`

Aggregate recent student activity scoped to the teacher's classes.

```ts
type ClassProgressResponse = {
  teacher_user_id: string;
  period_start: string;                   // ISO datetime
  period_end: string;
  total_student_questions: number;
  questions_by_subject: Record<string, number>;
  questions_by_class: Record<string, number>;
  top_topics: string[];
  scope: "teacher_classes" | "global_fallback";
  note: string;
};
```

**Scope semantics:**
- `teacher_classes` — Teacher row is linked and we filtered by their classes' (subject, grade_level) pairs and/or roster.
- `global_fallback` — No Teacher row linked yet (onboarding). Returns platform-wide aggregate so dashboard isn't blank. Surface a banner: "complete teacher onboarding to see class-scoped progress."

### 10.5 Saved AI generations

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/teacher/generations?generation_type=&conversation_id=&limit=&cursor=` | List the teacher's lesson_notes / quiz / student_feedback artefacts. Response carries `next_cursor` — see §8a. |
| `GET` | `/teacher/generations/{id}` | Full request + response payload to re-render or edit |

---

## 11. Parent endpoints (4)

Prefix `/api/v1/parent`. Require `role=parent` (or `admin`).

Parents see only `student_ok` material — teacher-only content is filtered out
at retrieval time.

### 11.1 `POST /parent/explain-topic`

```ts
type ExplainTopicRequest = {
  subject: string;
  student_class: string;                  // child's class
  topic: string;
  child_question?: string;                // optional: address the child's specific ask
};

type ExplainTopicResponse = {
  topic: string;
  subject: string;
  student_class: string;
  explanation: string;                    // 2-4 plain-language paragraphs
  everyday_analogy: string;
  things_to_try_at_home: string[];
  sources: string[];
  generation_id: string;
  conversation_id: string;
};
```

### 11.1a `POST /parent/explain-topic/stream` — same explanation, streamed (SSE)

Same body schema, role gate, and rate limit as `/parent/explain-topic`. Same streaming contract as `/teacher/lesson-notes/stream` (§10.1a) — the output is structured JSON, so frontends should consume `done.result` and treat `chunk` as optional preview material.

| Event | Frequency | Payload | Use it for |
|---|---|---|---|
| `meta` | exactly 1, first | `{topic, subject, student_class}` | Render shell UI title. |
| `chunk` | 0..N | `{text: string}` | Optional preview. |
| `done` | exactly 1, last | `{result: ExplainTopicResponse}` | Render the structured explanation. |
| `error` | replaces `done` | `{code: "stream_failed", message: string}` | Render a toast and stop. |

Wire format and browser snippet same as §9.1a.

### 11.2 `GET /parent/child-activity`

```ts
type ChildActivitySummary = {
  student_id: string;
  student_name: string;
  grade_level: string | null;
  period_start: string;
  period_end: string;
  total_questions: number;
  questions_by_subject: Record<string, number>;
  recent_questions: string[];             // up to 10 question previews
};

type ChildActivityResponse = {
  parent_user_id: string;
  children: ChildActivitySummary[];
  note: string;
};
```

A parent with no linked Parent row (or no children) gets `children: []`.
Frontend should surface an onboarding prompt in that case.

### 11.3 Saved AI generations

| Method | Path |
|---|---|
| `GET` | `/parent/generations?generation_type=&limit=&cursor=` | List the parent's explain-topic artefacts. Response carries `next_cursor` — see §8a. |
| `GET` | `/parent/generations/{id}` |

---

## 12. Admin endpoints (7)

Prefix `/api/v1/admin`. Require `role=admin`.

### 12.1 Provisioning

#### `POST /admin/teachers/setup` — one-shot teacher + school + classes

```ts
type TeacherSetupRequest = {
  supabase_user_id: string;               // UUID; link target
  full_name: string;
  email?: string;
  subject_specialty?: string;
  school_name: string;
  school_country?: string;
  classes: { name: string; subject?: string; grade_level?: string }[];
};

type TeacherSetupResponse = {
  school_id: string;
  teacher_id: string;
  class_ids: string[];
  linked_user_id: string;
  message: string;
};
```

Idempotent — re-running with the same `supabase_user_id` updates the existing
teacher; classes match by `(school_id, teacher_id, name)`.

#### `POST /admin/parents/setup`

```ts
type ParentSetupRequest = {
  supabase_user_id: string;
  full_name: string;
  email?: string;
  phone?: string;
  children: { full_name: string; grade_level?: string; email?: string; supabase_user_id?: string }[];
};

type ParentSetupResponse = {
  parent_id: string;
  linked_user_id: string;
  student_ids: string[];
  message: string;
};
```

#### `POST /admin/classes/{class_id}/roster` — enrol students

```ts
type ClassRosterRequest = {
  student_ids?: string[];                 // direct Student.id values
  student_supabase_user_ids?: string[];   // OR resolve from Supabase user_ids
};

type ClassRosterResponse = {
  class_id: string;
  added: string[];                        // student_ids actually added this call
  total_in_class: number;
  message: string;
};
```

After enrolment, `GET /teacher/class-progress` for the class's teacher uses
the roster (not just subject+grade) for scoping.

### 12.2 Platform stats

#### `GET /admin/usage-summary`

```ts
type UsageSummaryResponse = {
  period_start: string;                   // ISO datetime
  period_end: string;
  total_student_questions: number;
  total_ai_generations: number;
  questions_by_subject: Record<string, number>;
  generations_by_type: Record<string, number>;   // lesson_notes, quiz, ...
  generations_by_role: Record<string, number>;   // teacher, student, parent
  distinct_student_users: number;
  distinct_generating_users: number;
  schools: number;
  teachers: number;
  parents: number;
  students: number;
};
```

#### `GET /admin/content-stats`

```ts
type ContentStatsResponse = {
  pinecone_index: string;
  pinecone_namespace: string;
  pinecone_dimension: number;
  pinecone_vector_count: number;
  on_disk_chunk_files: number;
  on_disk_chunks_total: number;
  chunks_by_content_type: Record<string, number>;   // textbook | teacher_guide | syllabus
  chunks_by_subject: Record<string, number>;
};
```

### 12.3 Notifications

#### `POST /admin/notifications` — deliver a notification to a user

```ts
type CreateNotificationRequest = {
  recipient_user_id: string;              // Supabase user_id
  recipient_role: "student" | "teacher" | "parent" | "admin";
  category: string;                       // e.g. "progress_alert"
  title: string;
  body?: string;
  payload?: Record<string, unknown>;      // arbitrary JSON for the frontend
};
```

The recipient sees this via `GET /notifications` (§14).

---

## 13. AI generation browsing — shared shape

Each role's `GET /<role>/generations` and `GET /<role>/generations/{id}` use
these common types:

```ts
type AIGenerationSummary = {
  generation_id: string;
  generation_type: string;                // role-specific: lesson_notes, quiz, learning_path, explain_topic, ...
  role: "student" | "teacher" | "parent";
  title: string;                          // short label for UI listing
  subject: string | null;
  student_class: string | null;
  topic: string | null;
  conversation_id: string | null;
  sources: string[];
  created_at: string;
};

type AIGenerationListResponse = { generations: AIGenerationSummary[] };

type AIGenerationDetail = AIGenerationSummary & {
  request_payload: Record<string, unknown>;
  response_payload: Record<string, unknown>;
  updated_at: string;
};
```

**Re-rendering past artefacts**: `response_payload` contains exactly what the
generator returned on the original call. Type-cast it back to the role-specific
shape (`LessonNotesResponse`, `QuizResponse`, etc.) to render in the UI without
re-running the model.

**Cross-role isolation**: each `/<role>/generations` route returns rows where
`role` matches the URL prefix. A teacher fetching a student-role generation by
id gets `404`.

---

## 14. Notifications (cross-role)

Prefix `/api/v1/notifications`. Any authenticated user can list and mark their
own notifications. Creation is admin-only (§12.3).

### `GET /notifications?unread_only=&limit=&cursor=`

```ts
type NotificationOut = {
  id: string;
  recipient_user_id: string;
  recipient_role: "student" | "teacher" | "parent" | "admin";
  category: string;
  title: string;
  body: string | null;
  payload: Record<string, unknown> | null;
  is_read: boolean;
  read_at: string | null;                 // ISO datetime
  created_at: string;
};

type NotificationListResponse = {
  notifications: NotificationOut[];
  unread_count: number;                   // total unread, not just in this page
  next_cursor: string | null;             // see §8a for pagination
};
```

### `PATCH /notifications/{id}/read`

Returns the updated `NotificationOut`. Idempotent.

### `PATCH /notifications/mark-all-read`

```ts
type MarkAllReadResponse = { marked: number };
```

Use the `unread_count` field as the bell-icon badge.

---

## 15. TypeScript client quickstart

### Generate types from OpenAPI (recommended)

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o ./src/api/types.ts
```

Re-run whenever the backend schema changes. Commit the generated file.

### Minimal fetch wrapper

```ts
// lib/api.ts
const BASE_URL = process.env.NEXT_PUBLIC_REVED_API_URL ?? "http://localhost:8000";

export type APIResponse<T> = {
  status: "success" | "error";
  data: T | { code: string; details?: unknown } | null;
  message: string;
  role: "student" | "teacher" | "parent" | "admin" | "system";
};

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public httpStatus: number,
    public details?: unknown,
  ) { super(message); }
}

type ApiInit = RequestInit & {
  token?: string | null;                  // Supabase access_token in prod
  devRole?: "student" | "teacher" | "parent" | "admin"; // dev only
};

export async function apiFetch<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { token, devRole, headers, ...rest } = init;
  const res = await fetch(`${BASE_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(devRole ? { "X-Dev-Role": devRole } : {}),
      ...(headers ?? {}),
    },
  });
  const json = (await res.json()) as APIResponse<T>;
  if (json.status !== "success") {
    const err = json.data as { code: string; details?: unknown };
    throw new ApiError(err.code, json.message, res.status, err.details);
  }
  return json.data as T;
}
```

### Usage examples

```ts
// Student tutor — server-side history rehydration
const r1 = await apiFetch<StudentAnswerResponse>("/api/v1/student/ask", {
  method: "POST",
  body: JSON.stringify({ question: "I don't understand mechanics", student_class: "SS2", subject: "physics" }),
  token,
});
// Save r1.conversation_id locally; on the next ask:
const r2 = await apiFetch<StudentAnswerResponse>("/api/v1/student/ask", {
  method: "POST",
  body: JSON.stringify({
    question: "Tell me about Newton's second law",
    student_class: "SS2",
    subject: "physics",
    conversation_id: r1.conversation_id,
  }),
  token,
});

// Teacher generation, then re-render later
const gen = await apiFetch<LessonNotesResponse>("/api/v1/teacher/lesson-notes", {
  method: "POST",
  body: JSON.stringify({ subject: "physics", student_class: "SS2", topic: "Kinematics" }),
  token,
});
// later, fetch it back to re-render
const saved = await apiFetch<AIGenerationDetail>(`/api/v1/teacher/generations/${gen.generation_id}`, { token });

// Notifications bell badge
const inbox = await apiFetch<NotificationListResponse>("/api/v1/notifications?unread_only=true", { token });
setBadgeCount(inbox.unread_count);
```

---

## 16. Checklist for the frontend engineer

- [ ] Add `NEXT_PUBLIC_REVED_API_URL` (or equivalent) to the frontend env.
- [ ] Add backend dev URL to `CORS_ALLOWED_ORIGINS` on the backend.
- [ ] Install `@supabase/supabase-js`; wire sign-in/sign-out; pass `session.access_token`.
- [ ] Generate types: `npx openapi-typescript http://localhost:8000/openapi.json -o src/api/types.ts`.
- [ ] Build `apiFetch` (§15). Centralise the envelope unwrap + error throwing.
- [ ] Build a global error toast that reads `message` and branches on `code`.
- [ ] During development, keep `AUTH_ENABLED=false` and use `X-Dev-Role` to test
      each role's UI without provisioning real users.
- [ ] On 401 → redirect to sign-in. On 403 → show "not permitted". On 503 →
      show retry UI; many 503s are transient upstream rate limits, not bugs.
- [ ] For chat UIs (student tutor), store `conversation_id` in URL or local
      storage so the user can reopen the same thread on refresh / device switch.
- [ ] For teacher / parent generation UIs, render from `response_payload` of
      `GET /<role>/generations/{id}` — don't re-call the LLM to display a saved
      artefact.
- [ ] Surface `total_ai_generations` and `unread_count` in admin dashboards.

---

## 17. What's not yet in the API

The frontend can plan for these but shouldn't expect them today:

- **File uploads / corpus management via API**. Adding new PDFs to the index
  happens through `scripts/ingest_new_corpus.py` server-side.
- **Webhooks / push notifications**. Notifications are pull-only via `GET
  /notifications`. Poll periodically (e.g., every 30s while the app is open) or
  on user interaction.
- **Real-time collaboration** on lesson notes / quizzes. Generations are
  single-author for now.

> **Already available (don't be misled by older notes):**
> - **Streaming** — `/student/ask/stream`, `/teacher/lesson-notes/stream`,
>   `/parent/explain-topic/stream` (RevEd `meta`/`chunk`/`done` SSE), and
>   `/teacher/generate-content` (OpenAI-style SSE). First response can still
>   be 15–60s on cold model load; use loading/typing states.
> - **Cursor pagination** — see §8a; all list endpoints carry `next_cursor`.

---

## 18. Contact

If a contract looks wrong or a field is missing, open an issue in the backend
repo with the endpoint path, the request you sent, and the response you got.
