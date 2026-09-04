# notifications

Web Push (VAPID) — storing browser push subscriptions and delivering notifications to them. Three concerns, kept narrow:

- Persist subscriptions per (user, browser) via `PushSubscription`.
- Expose the VAPID public key and subscribe/unsubscribe API at `/api/v1/notifications/`.
- Send a notification to one user across all their active subscriptions via the `send_push_to_user` Celery task.

The app is intentionally small. It doesn't decide *when* to notify anyone — other apps (today only `recordings`) call `send_push_to_user.delay(...)` from their own Celery tasks or signal handlers. Frontend-side click handling lives in `frontend/public/push-sw.js`.

## How it fits together

```
Browser                                  Server
─────────                                ──────
1. fetch /vapid-public-key  ──────────→  return WEBPUSH_VAPID_PUBLIC_KEY
2. pushManager.subscribe()
   (asks user permission,
    contacts push service)
3. POST /subscribe ───────────────────→  validate endpoint URL (https + SSRF guard),
                                          then PushSubscription.objects.update_or_create
                                          (keyed on user + endpoint)

Later, when an event happens:
                                         some app calls:
                                         send_push_to_user.delay(
                                             user_id=...,
                                             title=..., body=...,
                                             data={"type": "..."})

                                         Celery worker:
                                         for sub in user's subscriptions:
                                             webpush(...)
                                         on 404/410:
                                             delete that subscription

   service worker (sw.js, importing push-handlers.js)
   receives event, calls
   self.registration.showNotification
   ← payload arrives over wire
```

## Model

### `PushSubscription`

One row per (user, browser, device) combination. A user can have many: desktop Chrome, mobile Firefox, etc. The `endpoint` is unique across the whole table. Re-subscribing with the same endpoint updates the row's keys via the ownership-scoped `update_or_create`; a fresh endpoint (e.g. after clearing browser data) creates a new row, and the stale one is reaped on its next failed send.

| Field | Notes |
|---|---|
| `user` | FK; cascades on user delete. |
| `endpoint` | Full URL provided by the browser's push service. Unique. Stable for the lifetime of a subscription. |
| `p256dh`, `auth` | Browser-generated encryption keys required to encrypt push payloads. Returned to the server as part of the `pushManager.subscribe()` response. |
| `created_at` | `auto_now_add`. |

Stale subscriptions (push service returns HTTP `404` or `410`) are deleted automatically inside `send_push_to_user` after a failed delivery attempt — no manual cleanup needed.

## API

Mounted at `/api/v1/notifications/`. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/vapid-public-key` | None | Returns `{"vapid_public_key": "..."}`. The browser needs this to set up the subscription. Intentionally unauthenticated — VAPID public keys are designed to be public. |
| `POST` | `/subscribe` | Required | Save or upsert a subscription. Body is the `pushManager.subscribe()` result (`endpoint`, `p256dh`, `auth`). The endpoint URL must be `https` and pass the SSRF guard (`federation.auth.check_url_is_safe`); rejections return 400 and emit a `notifications.subscription_rejected` security event. Upsert is keyed on `(user, endpoint)` — re-subscribing after a browser-side rotation silently replaces the caller's previous row, while an endpoint registered by a different user is acknowledged with 200 but left untouched (the response must not confirm the endpoint is known). |
| `DELETE` | `/subscribe` | Required | Remove a subscription. Body carries the `endpoint` to remove. Scoped to the authenticated user (you can only delete your own). |

## Sending a notification

The single delivery path is `send_push_to_user` ([tasks.py](tasks.py)):

```python
from notifications.tasks import send_push_to_user

send_push_to_user.delay(
    user_id=recording.author_id,
    title="Recording ready",
    body=f'"{recording.original_name}" has been processed and is ready.',
    data={"type": "recording_ready", "recording_id": recording.pk},
)
```

The task:

1. Reads `WEBPUSH_VAPID_PRIVATE_KEY` from settings. If unset (e.g. during local development without VAPID configured) the task logs at DEBUG level and returns `{"sent": 0, "stale": 0}` — never raises. Apps can call it unconditionally without checking whether push is configured.
2. Fetches every `PushSubscription` for the user.
3. Calls `pywebpush.webpush` for each, with VAPID claims built from `WEBPUSH_VAPID_SUBJECT`.
4. On `WebPushException` with status `404` or `410`, marks the subscription as stale; deletes them all in one query after the loop.
5. Returns `{"sent": int, "stale": int}`.

`data` is merged into the payload JSON alongside `title` and `body`. By convention every payload carries a `type` key (`"recording_ready"`, `"recording_failed"`, `"your_event"`) that the service worker uses to route click handling.

## Service worker

The push logic lives in [frontend/public/push-handlers.js](../frontend/public/push-handlers.js); the generated service worker (`sw.js`, built by `vite-plugin-pwa`) pulls it in with `importScripts`, so one worker owns the root scope and does both precaching and push. Receives push events, calls `self.registration.showNotification(title, options)`, and on `notificationclick` focuses an existing window or opens a new one. By default, clicking any notification opens `/`. Add a `notificationclick` branch keyed on `event.notification.data.type` if a notification should open a deep link instead.

The service worker is intentionally not in this app's tree because it's a frontend asset. Adding a new notification type without changing click behaviour requires no service-worker changes — the default open-`/` path handles it.

## Settings consumed

| Variable | Default | Notes |
|---|---|---|
| `WEBPUSH_VAPID_PUBLIC_KEY` | `""` | Base64url-encoded VAPID public key. Returned by `/vapid-public-key`; safe to expose. |
| `WEBPUSH_VAPID_PRIVATE_KEY` | `""` | Base64url-encoded VAPID private key. Server-side only. Empty disables push delivery (task is a no-op). |
| `WEBPUSH_VAPID_SUBJECT` | `mailto:admin@epicurrents.local` | Contact URI included in VAPID claims so push providers can reach you about delivery issues. Use a real mailto or HTTPS URL in production. |

Keys are generated by `init_env` ([epicurrents/management/commands/init_env.py](../epicurrents/management/commands/init_env.py)) and can be regenerated manually with `generate_vapid_keys` ([epicurrents/management/commands/generate_vapid_keys.py](../epicurrents/management/commands/generate_vapid_keys.py)).

## Adding a new push notification event

1. **Call the task** from wherever the event occurs (Celery task, signal, API endpoint):
   ```python
   from notifications.tasks import send_push_to_user

   send_push_to_user.delay(
       user_id=user.pk,
       title="<short title>",
       body="<one-line body>",
       data={"type": "your_event", **any_extra_context},
   )
   ```
2. **Handle the type in the service worker** ([frontend/public/push-handlers.js](../frontend/public/push-handlers.js)) only if you need custom click behaviour beyond opening `/`.
3. **No changes to this app are needed.** The task is generic over `(title, body, data)`.

## Project plugin extension points

| Hook | How |
|---|---|
| Send notifications from project code | Import `send_push_to_user` from `notifications.tasks` and call `.delay(...)`. No registration needed — any app can emit. |
| Custom subscription metadata | The model has no extension hook for per-subscription metadata today. If a project needs to e.g. tag a subscription with a device label, the cleanest path is a `OneToOneField(PushSubscription)` extension model in the project rather than modifying `PushSubscription` directly. |

## Tests

```bash
pytest notifications/tests/
```

### Mocking `webpush`

`pywebpush.webpush` is imported **inside** `send_push_to_user`, not at module level. This means tests must patch `pywebpush.webpush` directly — patching `notifications.tasks.webpush` does nothing because the name doesn't exist in the task module's namespace until the function runs.

```python
# Correct
with patch("pywebpush.webpush") as mock_send:
    send_push_to_user(user_id=user.pk, title="t", body="b")
    mock_send.assert_called_once()
```

```python
# Won't work — there is no notifications.tasks.webpush attribute
with patch("notifications.tasks.webpush") as mock_send:
    ...  # mock is never invoked
```

### Synchronous execution in tests

The test settings (`epicurrents.settings.test_platform`) set `CELERY_TASK_ALWAYS_EAGER=True`, so `send_push_to_user.delay(...)` runs synchronously and side effects (stale-subscription deletes, return values) are immediately assertable. Both `.delay()` and a direct call to the task function work.

## Gotchas

- **Subscription secrets never reach the audit trail in the clear.** `p256dh` and `auth` are registered as masked fields and the full row (including `endpoint`, a per-device identifier) is registered for GDPR Art. 17 scrubbing — both from [apps.py](apps.py) `ready()`; see [activity/README.md → Subject erasure](../activity/README.md#subject-erasure-gdpr-art-17). Keep the registrations in sync when adding fields to the model.
- **Empty `WEBPUSH_VAPID_PRIVATE_KEY` silently disables delivery.** The task returns `{"sent": 0, "stale": 0}` without warning. This is the right default for development (no VAPID setup needed) but means a misconfigured production deployment fails open — push events are issued but nothing is delivered. The `init_env` command sets keys automatically on first deployment, so this only happens if someone clears the values later.
- **Stale subscriptions are cleaned up on the next failed send, not proactively.** A subscription that's been revoked by the browser stays in the table until `send_push_to_user` runs against it once. For a user who never receives another notification, the stale row remains. Acceptable in practice — the table is small and the rows are harmless.
- **The unique constraint is on `endpoint`, but the upsert is ownership-scoped.** The subscribe endpoint refuses to reassign a row that belongs to another user: the request is acknowledged with 200 (so the response doesn't confirm the endpoint exists) but the existing row keeps its owner and keys, and a `notifications.subscription_rejected` security event is emitted. A concurrent-registration race is backstopped by the unique constraint inside a savepoint.
- **Endpoint URLs are SSRF-validated at subscribe time.** `send_push_to_user` makes an outbound request to the stored URL, so the subscribe path requires `https` and a globally-routable resolved address. The `FEDERATION_ALLOW_PRIVATE_PEER_URLS` development override applies here too (it lets a local dev push relay through); test settings enable it, so validation tests must explicitly disable it.
- **The push logic is at `/push-handlers.js`, served from `frontend/public/` and imported by the generated `/sw.js`.** The worker's scope is the entire site (Vite copies both to the build output root and Django serves them as static assets). Don't move either under a subdirectory — that would narrow the scope and break push handling. [main.ts](../frontend/src/main.ts) unregisters any lingering `push-sw.js` registration left from before the two workers were merged.
- **Click handling defaults to opening `/`.** Adding `data: {"type": "your_event"}` to a notification doesn't automatically route the click anywhere different. If you need deep linking, add a branch in `push-sw.js` keyed on the type.
