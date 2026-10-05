# RevEd Frontend ↔ AI Backend API Contract

_Last updated: 27 September 2026 — after the Teacher Dashboard term-based curriculum workflow._

Base URL: `https://reved-llm-backend-2.onrender.com/api/v1`
Auth: every call sends `Authorization: Bearer <Supabase access token>`. The role comes from the `user_role` claim set by the Supabase `reved_access_token_hook`. No `X-Dev-Role` in production.
Streaming endpoints use SSE. The RevEd grammar is `event: meta | chunk | done | error`. `/teacher/generate-content` uses the OpenAI-style `data: {choices:[{delta:{content}}]}` stream.
Error envelope expected on non-2xx responses: `{ status, data, message, role }`.

## Summary

| # | Method | Path | Role | Status |
|---|--------|------|------|--------|
| 1 | GET | `/health/ready` | public | Live |
| 2 | POST | `/student/ask` | student | Live |
| 3 | POST | `/student/ask/stream` | student | Live |
| 4 | GET | `/student/goals/{student_id}` | student | Live |
| 5 | POST | `/student/goals` | student | Live |
| 6 | PATCH | `/student/goals/{goal_id}/progress` | student | Live |
| 7 | GET | `/student/study-groups?student_class&subject` | student | Live |
| 8 | POST | `/student/study-groups` | student | Live |
| 9 | POST | `/student/study-groups/{id}/join` | student | Live |
| 10 | POST | `/student/study-groups/{id}/facilitate` | student | Live |
| 11 | POST | `/teacher/lesson-notes/stream` | teacher | Live — **new optional fields** |
| 12 | POST | `/teacher/generate-content` | teacher | Live — **new optional fields** |
| 13 | POST | `/teacher/quiz` | teacher | Live |
| 14 | POST | `/teacher/student-feedback` | teacher | Live |
| 15 | GET | `/teacher/class-progress?class_id&subject&term` | teacher | Live — **new optional filters** |
| 16 | GET | `/teacher/generations` | teacher | Live |
| 17 | GET | `/teacher/scheme-of-work?subject&student_class&term` | teacher | **NEW — must be built** |
| 18 | POST | `/parent/explain-topic/stream` | parent | Live |
| 19 | POST | `/admin/teachers/setup` | admin | Live |
| 20 | POST | `/admin/parents/setup` | admin | Live |
| 21 | POST | `/admin/classes/{class_id}/roster` | admin | Live |

Total: **21 endpoints** used by the frontend. 1 is new, 3 have new optional inputs.

## Build order for the backend team
1. **Check auth**: read the `user_role` claim from the Supabase token on every route (needed for all of the steps below).
2. **#17 `GET /teacher/scheme-of-work`**: blocking. Curriculum, Overview and Assignments need it before topics fill in automatically.
3. **#11 `POST /teacher/lesson-notes/stream`**: accept and store `term` and `week`.
4. **#12 `POST /teacher/generate-content`**: accept and store `term` and `week`.
5. **#15 `GET /teacher/class-progress`**: add the `subject` and `term` filters.
6. **#16 `GET /teacher/generations`**: optional. Return `term` and `week` for each item.
7. **Content review**: nothing to build. Optional later: `POST /admin/content-review/hints`.

## Required backend work (Teacher term workflow)

### 17. `GET /teacher/scheme-of-work` — NEW (blocking)
Gives the weekly topics for one subject, class and term from the NERDC (Primary/JSS) or WAEC/NECO (SSS) scheme of work. The Curriculum page, the "This week's lessons" card on the Overview, and the New Assignment title prefill all depend on it.

Query: `subject` (e.g. `Mathematics`), `student_class` (`Primary 1`–`Primary 6`, `JSS1`–`JSS3`, `SS1`–`SS3`), `term` (`1|2|3`).

Response (either a bare array or wrapped in `weeks`, `scheme` or `topics`; `weeks` is preferred):
```json
{ "weeks": [
  { "week": 1, "topic": "Whole numbers", "subtopics": ["Place value"], "objectives": ["Read numbers up to 1 billion"] }
] }
```
Rules: `week` starts at 1 and counts teaching weeks only (the frontend skips mid-term break weeks). Revision and exam weeks may come back as `topic: "Revision"`. Unknown subject/class should return `200` with `{ "weeks": [] }`, not 500. This is cacheable (the frontend caches it for 24h).
Status: **Live (AI-generated)**. Weeks are produced via RAG and model knowledge, with an optional `note`. The frontend shows a "check against your school's scheme of work" notice. No official NERDC/WAEC/NECO source is available yet. When one is provided, the backend swaps the data source without changing this contract.

### 11. `POST /teacher/lesson-notes/stream` — new optional fields
Adds `term?: number` and `week?: number` to the existing body `{subject, topic, student_class, duration_minutes?, curriculum_standard?, learning_objectives?}`. Use them to ground the note in the scheme-of-work week and store them on the generation record.

### 12. `POST /teacher/generate-content` — new optional fields
Adds `term?: number` and `week?: number` to `{contentType, subject, gradeLevel, topic, learningObjectives?, difficultyLevel?, curriculumStandard?, tone?}`. Same purpose as above.

### 15. `GET /teacher/class-progress` — **changed** (frontend adapted to the real shape)
Optional filters `class_id`, `subject`, `term` (term is accepted; currently a no-op on the backend).
Response the frontend now reads: `{teacher_user_id, period_start, period_end, total_student_questions, questions_by_subject, questions_by_class, top_topics, note, scope}`. Breakdowns may be `{label: count}` maps or `[{name|subject|class|topic, count}]` lists; `top_topics` may also be a list of strings. `note` is shown under the stats.
Future (separate feature, not scheduled): scores, engagement and at-risk students. These need real quiz-attempt and time-on-task signals first. Do not fill them with estimates.

### Recommended (not blocking)
- `GET /teacher/generations` could return `term` and `week` per item so the AI Generations tab can filter by week.

## Handled in Supabase, not the AI backend (for reference)
- `academic_terms` (term dates, per school): admin writes, school members read.
- `teacher_materials.term/week` and `assignments.term/week`: every saved lesson and assignment is filed under its week.
- `class_subjects.teacher_id`: the teacher's teaching load (class × subject).
- Storage bucket `teacher-materials` (private, 25 MB): uploaded files, stored in one folder per teacher.
- `submissions.grade/feedback`: grading from the Assignments page.

## Standard practice
Whenever the frontend adds or changes a backend call, update this file in the same change. List the method, path, role and body/response shape, and mark it **NEW** or **changed**.

## Content review and approval (Supabase only, no RevEd AI endpoints)
Teacher lesson content (`teacher_materials`) goes through `draft → pending → approved | changes_requested`.
- Teacher submits: `rpc submit_material(_id)`
- Admin decides: `rpc review_material(_id, _action: approve|request_changes|unpublish, _comment)`
- History: `material_reviews` table. Students and parents only receive `approved` rows.
- No new RevEd AI endpoints. Optional future endpoint: `POST /admin/content-review/hints`, which would check a material against the scheme of work.
