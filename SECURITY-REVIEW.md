# RevEd backend — security review

**Last automated pen-test run:** 2026-05-17
**Status:** ✅ No P0 / P1 findings open.

This document is the source of truth for the pre-launch pen-test pass
required by [`plan-to-implement.md`](../plan-to-implement.md) Phase 4.
The probes themselves live in
[`tests/integration/test_pen_test_pass.py`](tests/integration/test_pen_test_pass.py)
and run on every CI build — codified rather than manual so regressions
get caught the day they land.

## 1. Threat model in scope

Cross-tenant data leakage is the primary concern for a multi-school
B2B SaaS. Secondary concerns: authentication bypass, role-escalation,
SQL injection, payload-size denial of service, and information leak
through error responses.

Out of scope here (tracked separately):
* TLS / load-balancer / WAF posture (handled by the hosting platform).
* Supply-chain integrity (handled by Dependabot + CI lint).
* Insider-threat / key custody (covered by `DEPLOY.md` §2 rotation playbook).

## 2. Tests run and outcomes

### 2.1 Cross-school tenant isolation

| Probe | Test | Result |
|---|---|---|
| Admin in School A tries to provision a teacher in School B | `test_setup_teacher_in_another_school_returns_404` | ✅ 404 |
| Admin in School A rosters a student into School B's class | `test_update_roster_for_another_schools_class_returns_404` | ✅ 404 |
| Admin token with no `Admin` DB row attempts any school op | `test_setup_teacher_with_no_admin_row_returns_404` | ✅ 404 |

All cross-school attempts return **HTTP 404 with envelope `data.code=not_found`** — identical to "no such resource" so the response cannot be used as a probing oracle (cannot tell whether School B exists by reading the response).

### 2.2 Cross-user resource ownership

10 probes in [`test_resource_ownership.py`](tests/integration/test_resource_ownership.py): student-vs-student goals/study-groups/generations/conversations, teacher-vs-teacher generations, parent-vs-parent generations, notification mark-read by non-owner. All ✅ 404.

### 2.3 Wrong-role access (route-level RBAC)

`test_wrong_role_is_denied` parametrized across:

| Endpoint | Calling role | Result |
|---|---|---|
| `GET /parent/child-activity` | student | ✅ 403 |
| `GET /parent/child-activity` | teacher | ✅ 403 |
| `GET /teacher/class-progress` | student | ✅ 403 |
| `GET /teacher/class-progress` | parent | ✅ 403 |
| `GET /admin/usage-summary` | student / teacher / parent | ✅ 403 (×3) |
| `POST /admin/teachers/setup` | teacher | ✅ 403 |

All return `data.code=authorization_error` and emit an audit-log line via `reved.audit` (see Phase 2 Blocker 8).

### 2.4 JWT integrity (production-mode `AUTH_ENABLED=true`)

Verified by the `auth_enabled_client` fixture which flips the app into prod-auth mode without leaving the test environment.

| Probe | Test | Result |
|---|---|---|
| Expired token | `test_expired_jwt_is_rejected` | ✅ 401 |
| Wrong signature | `test_wrong_signature_jwt_is_rejected` | ✅ 401 |
| Wrong audience | `test_wrong_audience_jwt_is_rejected` | ✅ 401 |
| Missing `sub` claim | `test_missing_sub_jwt_is_rejected` | ✅ 401 |
| Non-UUID `sub` | `test_non_uuid_sub_jwt_is_rejected` | ✅ 401 |
| Garbage `Bearer <not.a.jwt>` | `test_garbage_authorization_header_is_rejected` | ✅ 401 |
| Missing `Authorization` header | `test_missing_authorization_header_is_rejected` | ✅ 401 |

Every failure also fires a `reved.audit` `event=jwt_decode outcome=failure` line with a stable `reason` code (`expired`, `invalid_signature`, `invalid_audience`, `missing_required_claim`, `missing_sub`, `non_uuid_sub`, `decode_error`).

### 2.5 SQL-meta-character injection

| Probe surface | Test | Result |
|---|---|---|
| UUID path parameter | `test_sql_meta_in_uuid_path_param_returns_4xx_not_500` (6 variants) | ✅ 404 / 422, never 500 |
| Repository filter argument | `test_sql_meta_in_repository_filter_is_parameterized` | ✅ Empty result — SQLAlchemy parameterizes |
| Validated body field (subject) | `test_subject_field_rejects_unknown_values_cleanly` | ✅ 422 (validator caught it before reaching DB) |

Probes covered: `' OR '1'='1`, `'; DROP TABLE students;--`, `1; SELECT * FROM users;--`, `%27%20OR%201=1--`, `../../etc/passwd`, `<script>alert(1)</script>`.

### 2.6 Oversized payload

| Probe | Test | Result |
|---|---|---|
| 50 KB question body | `test_oversized_ask_payload_does_not_crash` | ✅ < 500 |
| Goal title 10 KB (schema cap = 120) | `test_oversized_goal_title_is_rejected_cleanly` | ✅ 422 |

## 3. Findings

**None.** All 25 automated probes are green as of 2026-05-17.

Closed pre-launch:
* Resource ownership: 10 endpoints hardened in Phase 2 Blocker 4.
* Cross-school isolation: admin paths hardened in Phase 2 Blocker 5.
* JWT decode hooks + audit log: Phase 2 Blocker 8.
* Rate limiting (defense-in-depth against brute force): Phase 3 Blocker 2.

## 4. Known open issues (deferred — not P0/P1)

| Item | Severity | Tracking |
|---|---|---|
| Study groups are not school-scoped — cross-school discovery is possible via `GET /student/study-groups` | P3 | `plan-to-implement.md` Phase 2 follow-ups (data-model change) |
| `/admin/usage-summary` aggregates platform-wide rather than per-admin-school | P3 | Same |
| `setup_parent` does not auto-populate `student.school_id` from caller scope | P3 | Same |
| `prometheus-fastapi-instrumentator` `/metrics` adds before-launch | P3 | Phase 4 Observability sub-task |

## 5. Recurring controls

* CI runs `tests/integration/test_pen_test_pass.py` on every push (lint + test + build pipeline in `.github/workflows/ci.yml`).
* Annual manual pen-test by an external firm — out of scope here; recommend scheduling 30 days post-launch and then yearly.
* Audit logs (`reved.audit` JSON stream) shipped to SIEM. Recommended alerts already documented in `DEPLOY.md` §8.

## 6. How to reproduce locally

```bash
# All 25 probes
pytest tests/integration/test_pen_test_pass.py -v

# Plus the cross-school + cross-user negative tests
pytest tests/integration/test_pen_test_pass.py tests/integration/test_cross_school.py tests/integration/test_resource_ownership.py -v
```

The probes hit the real Supabase test DB (the `db_session` fixture wraps every test in a transaction that rolls back at teardown — no production data is touched).
