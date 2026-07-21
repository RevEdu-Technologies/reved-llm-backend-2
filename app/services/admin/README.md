# Admin services — cross-school authorization policy

## Why this doc exists

Every admin endpoint is gated by `require_role("admin")`, which proves the
caller has the *admin* privilege but says nothing about *which* school
they administer. Without an explicit per-school check, a school-scoped
admin from School A could provision teachers, classes, and rosters in
School B simply by passing School B's identifiers in the request body or
URL.

`app/services/admin/_authz.py` resolves the caller's `Admin.school_id`
at request time and lets services compare it against the operation's
target. Admin services consume this helper instead of trusting input.

## The two scopes

| `Admin.scope` | Meaning |
|---|---|
| `school` (default) | The admin may only touch resources whose `school_id` equals their own `Admin.school_id`. Cross-school attempts return **404** with the standard error envelope. |
| `global` | The admin may touch any school. Today this is reserved for platform operators; no provisioning flow uses it yet. |

## Why 404, not 403

Returning `403` ("forbidden") on a cross-school attempt would tell the
caller "the resource exists, you just can't reach it." That hands an
attacker a yes/no oracle over every UUID they probe. `404` is identical
to "no such resource" and gives an attacker no signal that the resource
exists. The trade-off is slightly worse self-service debugging — an
admin who genuinely needs cross-school access sees "not found" instead
of "you need elevated scope." This is the right trade-off for a
multi-tenant system: support tickets are recoverable; data leakage is
not.

## Dev-mode bypass

When `AUTH_ENABLED=false` (local development only), the route handler
flags the caller as a dev stub. The authz helper returns a synthetic
`AdminScope(scope='global')` for stubs so local devs can exercise admin
flows without seeding an `Admin` row. The prod-mode guard in
`app/core/config.py:validate()` refuses to start the server with
`AUTH_ENABLED=false` in any non-development environment, so this branch
is unreachable in production.

## Which endpoints are checked

| Endpoint | Check | Notes |
|---|---|---|
| `POST /admin/teachers/setup` | `caller_scope.school_id == school.id` (after lookup-or-create) | School-scoped admins cannot create new schools. |
| `POST /admin/classes/{class_id}/roster` | `caller_scope.school_id == class.school_id` | The class is looked up first; ownership is checked before any roster mutation. |
| `POST /admin/parents/setup` | **Not yet** | Parents have no `school_id` of their own; children created here also lack one. This is a separate data-modeling fix (Blocker 5 follow-up). |
| `GET /admin/usage-summary` | **Not scoped** | Aggregates across the whole platform. Should be scoped to caller's school in a follow-up; today it's an information-only endpoint with no row-level details. |
| `GET /admin/content-stats` | **Not scoped** | Returns Pinecone vector counts and on-disk chunk counts — global by nature. |
| `POST /admin/notifications` | **Not scoped** | Admin can deliver to any user. Acceptable for now (cross-role broadcast is a feature) but should be tightened when admin scope can be relied on universally. |

## Future work (tracked in `plan-to-implement.md`)

* Add `school_id` to study groups so cross-school discovery is blocked.
* Scope `usage-summary` and `notifications` delivery by admin school.
* Auto-populate `student.school_id` in `setup_parent` from the caller's
  admin scope, so parent-provisioned children land in the right school.
* Replace dev-mode bypass with a proper "platform-operator" provisioning
  flow that seeds a `scope='global'` Admin row.
