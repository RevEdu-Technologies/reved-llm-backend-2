# RevEd Webhooks (outbound event bus)

RevEd can push domain events to subscriber URLs over HTTP. Delivery is
HMAC-signed, persisted in a transactional outbox (no lost events), and
retried with exponential backoff.

## Event catalog

| Event | Fires when | `data` fields |
|---|---|---|
| `notification.created` | A notification row is inserted (`POST /admin/notifications`). | `notification_id`, `recipient_user_id`, `recipient_role`, `category`, `title` |
| `generation.completed` | An AI generation is persisted (student/teacher/parent). | `generation_id`, `user_id`, `role`, `generation_type`, `title`, `subject` |
| `goal.achieved` | A student goal reaches 100%. | `goal_id`, `student_id`, `title`, `subject`, `progress_percent` |

## Registering a subscription (admin only)

```
POST /api/v1/webhooks/subscriptions
{
  "url": "https://my-lms.example/reved-hook",
  "event_types": ["notification.created", "goal.achieved"],
  "school_id": "…optional; omit for a global subscriber…",
  "description": "LMS sync"
}
```

The response includes a one-time `secret` (shown **only** here). Store it —
you need it to verify the signature.

```
GET    /api/v1/webhooks/subscriptions          # list (admin)
DELETE /api/v1/webhooks/subscriptions/{id}      # deactivate (admin)
```

A subscription with `school_id` set receives matching events from that
school plus any global emit; a `school_id: null` subscription is a **global**
subscriber and receives matching events from every school.

## Delivery wire format

Each delivery is an HTTP `POST` with a JSON body:

```json
{
  "id": "<delivery uuid>",
  "event_id": "<groups all deliveries from one emit>",
  "event_type": "goal.achieved",
  "occurred_at": "2026-06-12T10:00:00+00:00",
  "data": { … event-specific … }
}
```

Headers:

| Header | Meaning |
|---|---|
| `X-RevEd-Event` | The event type. |
| `X-RevEd-Event-Id` | Groups every delivery fanned out from one emit. |
| `X-RevEd-Delivery-Id` | This individual attempt's id (use for idempotency). |
| `X-RevEd-Signature` | `hmac-sha256=<hexdigest>` over the **raw body bytes**, keyed by your secret. |

### Verifying the signature

Recompute the HMAC over the exact received bytes and compare in constant time:

```python
import hashlib, hmac

def verify(secret: str, body: bytes, header: str) -> bool:
    sig = header.removeprefix("hmac-sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
```

(`app/core/webhooks.py:verify_signature` is the canonical implementation.)

**Idempotency:** retries reuse the same `X-RevEd-Delivery-Id`. De-dupe on it.
Respond `2xx` to acknowledge; any non-2xx (or a timeout) triggers a retry.

## Retry policy

A delivery is attempted up to 6 times. After each failure it's rescheduled
with exponential backoff (`10s, 20s, 40s, …` capped at 1h). After the final
attempt it's marked `failed` and not retried.

## Running the dispatcher

Delivery runs out-of-band — register/emit only enqueue. Run the dispatcher as
a sidecar (or a scheduler tick):

```
python -m scripts.webhook_dispatcher                # loop forever (poll 2s)
python -m scripts.webhook_dispatcher --once          # one pass (cron-friendly)
python -m scripts.webhook_dispatcher --interval 5 --batch 50
```

It claims due rows with `SELECT … FOR UPDATE SKIP LOCKED`, so you can run
multiple dispatchers concurrently without double-delivery. Exits cleanly on
SIGINT/SIGTERM; an interrupted pass loses nothing (rows stay `pending`).

## Schema

`webhook_subscriptions` (registrations) and `webhook_deliveries` (the outbox)
— migration `a7c1e2f4b809_add_webhook_tables`. The secret is stored at rest
(plaintext in this MVP; envelope-encrypting it is a hardening follow-up).
