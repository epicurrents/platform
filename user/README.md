# user

The custom `User` model and the session-based authentication API. Two responsibilities, intentionally small:

- Own the `AUTH_USER_MODEL` so future schema additions don't require a Django model swap.
- Serve the API endpoints for login, logout, profile, password change, password reset, user search, and group listing.

Permission tiers (`is_staff` / `is_superuser`) live on the model but the rules for *when* to require each are in [epicurrents/README.md](../epicurrents/README.md#permissions) — they apply across every app, not just here.

## Model

### `User`

Subclass of Django's `AbstractUser` with no extra fields. Lives in `user_user` (Django's standard `<app>_<model>` table-name convention). The subclass exists for one reason: setting `AUTH_USER_MODEL = "user.User"` from day one means that adding a profile field (avatar, locale, signature key, anything) later is a regular Django migration rather than a notoriously painful model-swap operation.

`is_staff` and `is_superuser` are the platform-wide access tiers:

| Tier | Meaning |
|---|---|
| `is_staff` | Admin-level read access — dashboards, batch operations, anything that requires visibility across all users' data. |
| `is_superuser` | Destructive / irreversible actions (epoch generation with `--clear`, future data-deletion flows). Strict subset of staff: anything a superuser can do, a staff user should also be able to do or see in read-only form. |

Use these directly when staff vs superuser expresses the distinction.

### `ExternalIdentity`

Links a `User` to an account at an external OpenID Connect provider. One user may have several (password plus one or more providers). Uniqueness is the `(provider, issuer, subject)` triple — `subject` (the OIDC `sub` claim) is the opaque, pairwise pseudonymous identifier the provider issues, unique only within an issuer. `email` / `email_verified` are cached for display and for the verified-email linking policy; the link works without them. See [External login (OpenID Connect)](#external-login-openid-connect) for how rows are created and consumed.

### `TwoFactorCredential`

A user's TOTP second factor, one-to-one with `User`. Fields: `secret` (base32), `confirmed_at`, `last_counter`, `backup_codes` (a list of SHA-256 hashes), `created_at`. See [Two-factor authentication](#two-factor-authentication-totp) for the flow and the reasoning.

It is a separate model rather than fields on `User` for two reasons. Every write to `User` serialises all of that model's concrete fields into `ObjectChangeLog`, so a secret living there would ride along on every unrelated profile edit. And keeping it separate leaves the secret out of `UserOut` and `AccountOut` by construction rather than by remembering to exclude it.

Both `secret` and `backup_codes` are registered with `register_masked_fields` and `register_subject_pii` in `UserConfig.ready`.

### `UserPreference`

A client's settings for one user, stored as an opaque JSON blob under a `scope` string. One row per `(user, scope)`, enforced by a uniqueness constraint.

The platform does not interpret `values` — it is a flat map of setting names to primitive values whose meaning belongs entirely to the client that wrote it. Keeping it opaque is the point: the viewer gains a user-definable setting and nothing here changes. The write endpoint still constrains the *shape* (setting-path-looking keys, primitives or short flat lists as values, bounded counts and lengths), because the blob is user-supplied, is rewritten on every settings change, and every version of it lands in the permanent audit trail. Without those bounds the field would be general-purpose storage with an unbounded change log behind it.

Today's only scope is `viewer`, written by the Epicurrents viewer's settings-backend client. Annotators do not always work from the same machine, so the viewer's own storage — session storage, plus local storage when the settings cookie is on — is the wrong place for a preference like the chosen montage to live alone. The viewer reads the account copy on top of the device copy at startup (account wins: a setting changed on another machine should be found that way here) and writes changes back with a short debounce. The platform SPA turns this on by passing `userSettingsBackend` in the viewer setup object, and only for a signed-in session; see `VIEWER_USER_SETTINGS_PATH` in [frontend/src/lib/viewerConfig.ts](../frontend/src/lib/viewerConfig.ts).

**Known limitation.** The accepted value shape (primitive, or a short flat list of primitives) is narrower than what the viewer declares as user-definable. `eeg.trends.aeeg.derivationColors` is declared `Object` and holds a map of derivation id to colour, which this endpoint rejects; the viewer's client drops such a value before sending rather than letting one setting fail the whole write, so that setting stays device-local. Nothing writes it through the settings mutation today, so this is latent. Widening the contract means letting caller-chosen key names into an audited field, which is the thing the shape check exists to prevent — worth a deliberate decision rather than a quiet relaxation.

`values` is registered for GDPR Art. 17 erasure in `UserConfig.ready`. It should never carry personal data and the endpoint's shape check makes that unlikely, but a client is free to name a setting badly and the audit trail keeps every version forever, so registering it is the cheaper side of the trade.

## API

Mounted at `/api/v1/user/`. Full request/response detail in [api/v1/ninja.py](api/v1/ninja.py).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/login` | Authenticate with username + password. Answers `{ authenticated, two_factor_required, user }`; opens a session cookie only when no second factor is owed. Rate-limited per username. |
| `POST` | `/login/2fa/setup` | Enrol from the pending-login state, for an account that a `TWO_FACTOR_REQUIRED_*` setting obliges to hold a factor it has not got yet. |
| `POST` | `/login/2fa` | Verify a TOTP or backup code against the login this session started, and open the session. Same envelope. |
| `POST` | `/logout` | Destroy the current session. |
| `GET` | `/me/2fa` | The caller's second-factor state: `{ enabled, confirmed_at, backup_codes_remaining }`. |
| `POST` | `/me/2fa` | Begin enrolment. Requires the caller's password; returns `{ secret, provisioning_uri }` and leaves the credential unconfirmed. |
| `POST` | `/me/2fa/confirm` | Activate a pending enrolment with a code from the authenticator. Returns the recovery codes, the only time they are shown. |
| `POST` | `/me/2fa/backup-codes` | Discard the caller's unused recovery codes and issue a fresh set. Requires the password. |
| `POST` | `/me/2fa/disable` | Remove the caller's second factor. Requires the password. |
| `GET` | `/me` | Auth-state probe: always HTTP 200 with `{ authenticated, user }` — `user` is the serialized profile when signed in, `null` otherwise. Returning logged-out as a 200 rather than a 401 keeps the SPA's per-boot probe out of the console error stream. |
| `PATCH` | `/me` | Update `email`, `first_name`, or `last_name`. |
| `POST` | `/me/change-password` | Change the current password. Keeps the session alive via `update_session_auth_hash`. |
| `POST` | `/reset-password` | Request a password reset link by email. Rate-limited per email address. |
| `POST` | `/reset-password/confirm` | Validate a reset token and set a new password. |
| `GET` | `/search?q=...` | Search active users by username / first name / last name. Used by sharing flows. Returns up to 20 matches; `q` must be at least 2 chars. |
| `GET` | `/groups` | List all Django groups (for `AccessRight` group targets). |
| `GET` | `/preferences?scope=viewer` | The caller's stored client settings for that scope, as `{ scope, settings }`. An unknown scope is not an error — it yields an empty map, which is what a client that has never saved anything should see. |
| `PUT` | `/preferences?scope=viewer` | Replace the caller's stored settings for that scope. The whole map is replaced rather than merged: the client owns the settings and sends a complete snapshot, so merging would resurrect settings the user has since cleared. Rejects keys that do not look like `<module>.<field>` setting paths, values that are not primitives or short flat lists of them, and blobs over 16 KiB serialized. The total-size cap is the bound that matters: the per-field limits multiply out to tens of megabytes, and every accepted blob is written to `ObjectChangeLog` twice (before and after state) and kept forever. |
| `GET` | `/auth-config` | Public list of external login providers the SPA should offer. Empty when OIDC is disabled. |
| `GET` | `/oidc/{provider}/start` | Begin the OIDC redirect flow. 404 when OIDC is disabled. |
| `GET` | `/oidc/{provider}/callback` | Complete the OIDC flow, open a session, and redirect back to the SPA. |

### `UserOut` payload shape

| Field | Notes |
|---|---|
| `id`, `username`, `email`, `first_name`, `last_name` | Standard. |
| `is_staff`, `is_superuser` | Both exposed so the frontend can gate UI elements. The auth store derives `isStaff` (true when either flag is set) and `isSuperuser` (true only when `is_superuser`). |
| `is_2fa_enabled` | Whether a confirmed second factor gates this account's password login. An unconfirmed enrolment reads as `false`. |
| `roles` | Project-supplied roles the user inherits through group membership, keyed by the role key the active project registered (e.g. a teaching project's `course_role` → `["instructor"]`). Read through the [project-role registry](#project-roles) — the user app imports no project. Empty map when the deployment defines no roles. |

`UserSearchOut` is a smaller shape returned by `/search`: `id`, `username`, `first_name`, `last_name`. No `email` is exposed (it's PII that doesn't need to leak through search auto-complete).

## Account administration

Mounted at `/api/v1/user/admin/`, implemented in [api/v1/accounts.py](api/v1/accounts.py). This is the in-app replacement for the Django admin's user and group forms.

Replacing the admin beat the two alternatives — an IP allowlist in front of it, or wiring it into an audited scope. Instrumenting means two mechanisms kept correct forever against one surface that inherits correctness: a second middleware beside the load-bearing `_API_PATH_RE`, which must not be widened because it would also open a scope for every admin CSS request, plus explicit `record_*_change` calls for the admin's bulk actions, which use `QuerySet.update()` / `delete()` and bypass the signals in any scope. The admin also ran Django's model-level permissions in parallel with the `_require_staff` / `_require_superuser` and `AccessRight` checks everything else uses.

Living under `/api/v1/` is the point. The path matches `_API_PATH_RE`, so every request opens an audited context, every model write inside it lands on the hash chain, and session writes pass the CSRF chokepoint — none of which happened for the same operations through `/admin/`.

No in-app client exists yet: the SPA carries no view, route or API module for these endpoints, so they are driven by hand — a session cookie plus the CSRF token — or the work goes through management commands. The UI is tracked in [ROADMAP.md](../ROADMAP.md) under *User — account and group management UI*.

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/admin/accounts?q=&limit=&offset=` | Staff | List accounts, inactive ones included. `limit` is capped at 500. |
| `GET` | `/admin/accounts/{id}` | Staff | One account, with group membership and project roles. |
| `POST` | `/admin/accounts` | Superuser | Create an account. |
| `PATCH` | `/admin/accounts/{id}` | Superuser | Edit account fields. Username is not editable; roles belong to groups. |
| `POST` | `/admin/accounts/{id}/password` | Superuser | Set another account's password. |
| `DELETE` | `/admin/accounts/{id}/2fa` | Superuser | Remove an account's second factor, for a lost authenticator. See [Two-factor authentication](#two-factor-authentication-totp). |
| `PUT` | `/admin/accounts/{id}/groups` | Superuser | Replace one account's group membership. |
| `GET` | `/admin/groups` | Staff | Groups with member and grant counts and their project roles. |
| `POST` | `/admin/groups` | Superuser | Create a group. |
| `PATCH` | `/admin/groups/{id}` | Superuser | Rename a group and/or set its [project roles](#project-roles). |
| `DELETE` | `/admin/groups/{id}` | Superuser | Delete a group, refused while grants target it. |
| `PUT` | `/admin/groups/{id}/members` | Superuser | Replace one group's membership. |
| `GET` | `/admin/roles` | Staff | Project-supplied roles this deployment defines. |

Four rules the surface enforces, each of which has a test that fails without it:

- **No account deletion.** [`erase_user`](#account-erasure-gdpr-art-17) is the only sanctioned path, because it unlinks owned recording and media files — something FK cascade never does. A CRUD delete would strand PHI on disk exactly as the admin's delete button did.
- **The last active superuser cannot be demoted or deactivated.** Writes here are superuser-only, so that change would lock every operator out of account administration; the way back in is a management command on the host, which needs shell access the operator may not have at that moment. Refused with a 409.
- **A group with live `AccessRight` rows cannot be deleted.** The rows would cascade away with it, revoking access for everyone in the group at once and leaving no record of what was withdrawn. The refusal reports the count, and `GET /admin/groups` carries it so the answer is visible before the attempt.
- **Passwords go through `AUTH_PASSWORD_VALIDATORS`.** Otherwise an operator-set password would face a lower bar than one a user sets for themselves.

Setting a password deliberately does **not** flush the account's sessions. An operator setting a password is usually helping somebody back in rather than responding to a compromise, and signing them out of a viewer session mid-review is its own harm. For a compromise, deactivate the account — that does end its sessions.

### Group membership and the audit trail

Membership is the one operation here the audit signals cannot see. `activity/signals.py` receives `post_save` / `pre_delete`; `groups.set()` emits `m2m_changed`, and the M2M rows are not concrete fields on the user, so they never reach `serialize_instance` either. Left alone, the most consequential thing this surface does would be the one thing that left no trace.

Both membership endpoints therefore call `record_modify_change` explicitly, with a digest of the resulting membership in `extra_payload` ([audit_digests.py](audit_digests.py)). The digest is mixed into the row's `after_hash`, so editing the M2M table directly and then editing the stored digest to match still breaks chain verification. `verify_derived_state` catches the direct edit on its own.

The digest is taken over group **primary keys**, not names. Groups can be renamed through this surface and nothing in the platform gates on a group's name, so a name-based digest would recompute differently after any rename and report every historical membership row as tampered.

`PUT /admin/groups/{id}/members` writes one audit row per *affected account* rather than one for the group. `erase_subject` reaches audit rows through their target, so a single row targeting the group would put one account's membership history beyond the reach of every other account's erasure request. Accounts whose membership is unchanged get no row.

## Project roles

A project may give groups a meaning core knows nothing about — an instructor / student distinction in a teaching project, say. Roles ride on **groups**: each group carries at most one role per provider, and a user holds whatever roles their groups carry. Membership is therefore the single assignment mechanism (managed through this surface, with explicit audit), one user can hold several roles through several groups, and a per-course group can both receive access grants and mark its members' role in that course.

[roles.py](roles.py) is the extension point: a project registers a `RoleProvider` from its `AppConfig.ready()` giving a key, a label, the accepted `(value, label)` choices, a batched group reader and a group writer.

```python
from user.roles import RoleProvider, register_role_provider

register_role_provider(
    RoleProvider(
        key="course_role",
        label="Course role",
        choices=tuple(GroupRole.ROLE_CHOICES),
        read_groups=_read_group_roles,  # Sequence[Group] -> {group_pk: role}
        write_group=_write_group_role,  # (Group, value | None) -> None
    )
)
```

Core calls the registry when serialising a user (`roles` on `UserOut` / `AccountOut`, the union over their groups) and when serialising or editing groups (`roles` on `GroupDetailOut`; `PATCH /admin/groups/{id}` carries a `roles` map, where a write outside the provider's declared choices is refused with a 400 before the provider is called and an explicit `null` clears the role). `read_groups` takes the groups of interest in one call so a roster page costs one provider query, not one per group. A provider that raises on read is reported as empty rather than propagating — a project whose role table has not migrated yet should not turn every user lookup into a 500.

`roles` in a group PATCH is a partial map: an absent key is left alone, so a client that does not know a project's role exists cannot clear it. There is no per-account role write — assigning a role is a membership change.

## Security mechanisms

### Login rate limiting

Failed login attempts are counted per username in the Django cache (Redis in production, LocMemCache in development).

| Constant | Value | Where |
|---|---|---|
| `_LOGIN_MAX_ATTEMPTS` | `10` | [api/v1/ninja.py](api/v1/ninja.py) |
| `_LOGIN_LOCKOUT_WINDOW` | `5 * 60` (5 min) | [api/v1/ninja.py](api/v1/ninja.py) |

After 10 consecutive failures the account is locked out for 5 minutes; subsequent login attempts (even with the correct password) return **429**. The counter resets on a successful login.

Cache keys:

- `login_attempts:<sha256(username.lower())>` — attempt counter, expires with the lockout window.
- `login_lockout:<sha256(username.lower())>` — set when the threshold is hit; presence of this key is what triggers the 429.

Keys are kept separate so the lockout can expire on its own schedule without interference from a stale counter, and so neither key contains a raw username.

### Two-factor authentication (TOTP)

Opt-in per account, from the profile page. RFC 6238 TOTP over `pyotp`, with hashed single-use recovery codes. Mechanism in [two_factor.py](two_factor.py), enrolment endpoints in [api/v1/two_factor.py](api/v1/two_factor.py), the login gate in [api/v1/ninja.py](api/v1/ninja.py).

**It applies to password login only, and that is a policy rather than an omission.** `django.contrib.auth.login` is called from three places and they authenticate differently:

| Path | Authenticates by | Policy |
|---|---|---|
| `login_endpoint` | Username + password | Where TOTP applies. |
| `oidc_callback` | An external provider's ID token | The provider owns the second factor. An account provisioned this way may have no usable password at all, so a local factor is not something it could supply; `_confirm_password` refuses enrolment with 409 for exactly that reason. |
| A project's token login (a teaching project's session join, say) | A session token in the URL, logging the browser into a **shared project account** | Exempt by design. A different authentication method with its own controls, not a password login missing its password — enrolling the shared account would break every student join. |

**Enrolment is two steps and cannot lock an account out.** `POST /me/2fa` mints a secret and returns it; `confirmed_at` stays null. Only `POST /me/2fa/confirm`, given a code proving the authenticator holds the same secret, sets it. Nothing on the login path looks at an unconfirmed row — `active_credential` is the single reader and filters on `confirmed_at`, so an abandoned or mis-scanned enrolment leaves the account signing in exactly as before.

**A code is consumed, not just checked.** A TOTP code is valid for its whole 30-second step and the server accepts one step of drift either way, so an observed code would otherwise replay for up to 90 seconds. `last_counter` records the step that was spent and any counter at or below it is refused. The claim is a conditional `UPDATE ... WHERE last_counter < %s`, atomic in one statement, so two concurrent requests carrying the same code cannot both pass. Confirmation spends its own step too, which is why signing out and back in within 30 seconds of enrolling needs the next code rather than the one just used.

**Recovery codes are hashed with SHA-256, deliberately not with the password hasher.** They are server-generated at 60 bits from a 32-symbol alphabet, so there is no dictionary to attack and no user-chosen weakness to stretch away. The hasher would also cost more than it buys in the other direction: verification compares against every unused code, so ten Argon2 verifications per attempt would make the endpoint a CPU amplifier reachable before authentication completes. The alphabet is RFC 4648 base32, which contains `O` and `I` but not `0` and `1` — so a code misread off paper is misread unambiguously, and `_normalise_backup` folds the digits onto the letters rather than rejecting them.

**The half-finished login lives on the session.** A correct password writes `pending_two_factor` — the account id, a timestamp, and the backend that verified it — and returns without calling `login()`. `_pending_two_factor` re-validates on every read, so an account deactivated or a factor reset in the intervening seconds stops the login rather than completing one that was already half-passed. The recorded backend matters: `authenticate` sets `user.backend`, a user re-read from the database in the second half has none, and `login()` refuses a user without one whenever more than one backend is configured.

Rate limiting reuses `_LOGIN_MAX_ATTEMPTS` / `_LOGIN_LOCKOUT_WINDOW` under `login_2fa_attempts:<pk>` and `login_2fa_lockout:<pk>`. Sharing the policy is deliberate — a separate budget for the code step would only widen the total number of guesses one login allows.

Three of the four management writes re-check the password (`_confirm_password`). A session cookie should not be enough to remove the control protecting the account or to mint fresh recovery codes; confirming an enrolment is the exception, since the code is itself the proof and the password was checked when the enrolment started.

**Operator recovery** is `DELETE /api/v1/user/admin/accounts/{id}/2fa` — superuser only, audited as `user.account.2fa.reset`, and emitting `auth.2fa_reset` to the security log. Someone will lose their phone, and without this the account is reachable only through a shell on the host, which makes a routine event an incident. It is not self-service: the account's own disable endpoint needs the password, which a person who lost only their phone still has.

**Two settings make a factor mandatory**, both defaulting off: `TWO_FACTOR_REQUIRED_FOR_STAFF` covers accounts with `is_staff` or `is_superuser`, and `TWO_FACTOR_REQUIRED_FOR_ALL` covers every account that authenticates by password. `FOR_ALL` dominates, so the two compose rather than conflict; `two_factor_required` in [two_factor.py](two_factor.py) resolves them.

Turning either on does not lock anybody out. The blocker they once had — that an account required to enrol cannot reach the session-authenticated endpoints above, because it has no session yet — is what `POST /login/2fa/setup` solves: it enrols from the pending-login state, and `POST /login/2fa` then completes the same login with the first code. Accounts with no usable password are exempt, since enrolment re-confirms the password and an externally-authenticated account could never satisfy that.

### Password validation

All new passwords (from `/me/change-password` and `/reset-password/confirm`) are run through Django's `validate_password` with the validators configured in `AUTH_PASSWORD_VALIDATORS`. Default validators (set in [epicurrents/settings/common.py](../epicurrents/settings/common.py)):

- `UserAttributeSimilarityValidator`
- `MinimumLengthValidator`
- `CommonPasswordValidator`
- `NumericPasswordValidator`

Validation failures raise `HttpError(400)` with a joined error string from `exc.messages`. The list above is the only place to extend — adding a custom validator means appending it to `AUTH_PASSWORD_VALIDATORS` in the relevant settings module (a project plugin's `settings.py` can extend the list since `AUTH_PASSWORD_VALIDATORS` is one of the list-append settings — see [epicurrents/README.md](../epicurrents/README.md#settings-architecture)).

### Password reset rate limiting

Per email address, one request per 5 minutes (`_RESET_RATE_WINDOW`). The rate-limit check runs **before** the user lookup, so the 429 response cannot be used to infer whether an address is registered.

The response is always `200 {"status": "ok"}` whether the address exists or not — user enumeration is prevented at the response level too. If the address exists and is active, a reset link is dispatched via `send_password_reset_email`.

Address resolution has three properties worth keeping. The address is validated before it reaches the query, because the user model does not require an email and blank is the stored default for admin-created and OIDC-provisioned accounts — an empty string otherwise matched all of them. The lookup uses `filter`, not `get`, because nothing constrains email to be unique: two active accounts can share one address, and `get` turned that into a 500, which both breaks reset for that address permanently and hands back the enumeration signal the 200-always rule exists to withhold. Every matching account gets its own link.

The audit row carries an `email_hash` only when it targets exactly one account. `log_activity` annotates the single row the middleware created for the request rather than appending, so there is one row per request no matter how many accounts matched — and `erase_subject` walks `Activity` by `target_content_type` / `target_object_id`. An untargeted row's hash is therefore unreachable by erasure, and a row that targets one of several matching accounts cannot be reached on behalf of the others. So the not-found branch records `found: False` alone, the shared-address branch records `found: True` plus `account_count`, and only the single-match branch keeps the hash, where erasure demonstrably reaches it.

Cache key: `pwd_reset_rate:<sha256(email.lower())>`. Email addresses are never stored in cache directly.

The matching client-side cooldown duration in `LoginView.vue` is hardcoded to the same window — change both together.

### Reset token

Standard Django `default_token_generator`. Links are `{FRONTEND_URL}/reset-password?uid=<b64>&token=<tok>`; the `uid` is a urlsafe-base64 of the user PK and the token expires after 3 days (Django default — overridable via `PASSWORD_RESET_TIMEOUT` setting if needed).

## External login (OpenID Connect)

Single-tenant Microsoft Entra ID login, alongside the password form. **Shipped as a non-operational scaffold**: every piece is present and tested, but the feature is off (`OIDC_ENABLED=false`) and the endpoints return 404 until an operator registers an app with Entra and supplies credentials. With no credentials it cannot function, so a default deployment behaves exactly as before.

The implementation lives in [oidc.py](oidc.py) (flow logic), [auth_backends.py](auth_backends.py) (the `OIDCBackend`), and the `/oidc/*` + `/auth-config` endpoints in [api/v1/ninja.py](api/v1/ninja.py).

### Flow

Backend-driven Authorization Code with PKCE — the backend-for-frontend pattern. The flow ends in a normal Django `login()`, so it reuses the existing session cookie and CSRF model and **no token ever reaches JavaScript**:

1. The login screen calls `/auth-config`; if a provider is configured it renders a "Sign in with Microsoft" button.
2. The button is a full-page navigation to `/oidc/entra/start`. That endpoint generates `state` + `nonce` + a PKCE verifier, stashes them in the session, and 302s to Entra.
3. The user authenticates at Entra and is returned to `/oidc/entra/callback`. The callback checks `state`, exchanges the code (with the client secret, server-to-server), validates the ID token, enforces the domain allowlist, resolves or provisions the user, and opens the session.
4. The callback redirects to the SPA. The SPA's `authStore.init()` picks up the session via `/me`.

The callback is a GET that writes (it can create a `User` + `ExternalIdentity`). That is deliberate and correct for the CSRF chokepoint: the chokepoint only acts on unsafe methods, and the OAuth `state` round-trip is the flow's anti-forgery control. User provisioning happens inside `transaction.atomic()`, and because the path is under `/api/v1/` the audit middleware records it.

### Identity validation

`validate_id_token` verifies the ID-token signature against the provider JWKS (via Authlib), then `_check_claims` enforces issuer, audience, **tenant (`tid`)**, and nonce. The tenant check is the single-directory lock — without it any Microsoft account would pass. These checks are the reason [oidc.py](oidc.py) is a load-bearing file (see AGENTS.md); the contract test is [tests/test_oidc.py](tests/test_oidc.py).

### Provisioning policy

On a first login (no matching `ExternalIdentity`):

- If `OIDC_LINK_BY_VERIFIED_EMAIL` and the token carries a verified email matching an active user, the identity links to that account.
- Otherwise, if `OIDC_AUTO_CREATE_USERS`, a password-less user is created (`set_unusable_password()` — these accounts authenticate only through the provider).
- Otherwise the login is refused (`auto_create_disabled`).

Returning logins match by `(provider, issuer, subject)` and skip all of the above.

### Restricting who can register — PHI containment

Limiting accounts to vetted hospital domains is the boundary that bounds PHI spread. Three controls, weakest to strongest; only #1 is in code here.

| # | Control | Where | Status |
|---|---|---|---|
| 1 | Email-domain allowlist (`OIDC_ENTRA_ALLOWED_DOMAINS`) | This app — `email_domain_allowed` in [oidc.py](oidc.py), enforced before any account is created and fails closed | **Implemented** |
| 2 | Entra "User assignment required" + per-hospital group assignment | Identity-provider side — Entra refuses to issue a token to an unassigned user | Documented, operator-configured |
| 3 | `groups`-claim gate (membership of a "signed the confidentiality agreement" group) | Would request the `groups` claim and check it in `resolve_identity` | Documented, not implemented |

Control #1 is enforced on every login (existing links included) and rejects guest / B2B accounts, whose UPN yields no home domain. It is a backstop, not the primary gate — a domain is a crude proxy for "vetted user". Prefer #2 (or #3) as the primary control and keep #1 as defence in depth. Implementing #3 means enabling the groups claim in the Entra app's token configuration and adding a membership check alongside the domain check.

### Settings

All read in [epicurrents/settings/common.py](../epicurrents/settings/common.py); env keys are in [.env.example](../.env.example).

| Setting | Purpose |
|---|---|
| `OIDC_ENABLED` | Master switch. Off by default. |
| `OIDC_ENTRA_TENANT_ID` | Entra directory (tenant) GUID. Builds the authority and locks the `tid` check. |
| `OIDC_ENTRA_CLIENT_ID` / `OIDC_ENTRA_CLIENT_SECRET` | From the Entra app registration. |
| `OIDC_ENTRA_ALLOWED_DOMAINS` | Comma-separated email-domain allowlist (control #1). Empty = any account in the tenant. |
| `OIDC_REDIRECT_URI` | Must match the redirect URI registered in Entra exactly. Defaults to `FRONTEND_URL` + `/api/v1/user/oidc/entra/callback`. |
| `OIDC_AUTO_CREATE_USERS` | Provision a new user on first login. |
| `OIDC_LINK_BY_VERIFIED_EMAIL` | Link a first login to an existing account by verified email. |

`AUTHENTICATION_BACKENDS` gains `user.auth_backends.OIDCBackend` after `ModelBackend`; it is inert for password logins (reachable only with an `oidc_identity` kwarg).

### Taking it into production

1. **Register the app in Entra.** Microsoft Entra ID → App registrations → New registration. Supported account types: *Accounts in this organizational directory only (single tenant)*. Add a **Web** redirect URI of `https://<host>/api/v1/user/oidc/entra/callback`.
2. **Collect credentials.** Note the Application (client) ID and Directory (tenant) ID. Under Certificates & secrets create a client secret and copy its value immediately.
3. **Configure claims and permissions.** Under API permissions keep the delegated `openid`, `profile`, `email` Microsoft Graph scopes (grant admin consent if your tenant requires it). If you rely on the email allowlist or email linking, add `email` as an optional token claim under Token configuration.
4. **Fill `.env`.** Set `OIDC_ENABLED=true` and the `OIDC_ENTRA_*` values; set `OIDC_REDIRECT_URI` to the exact URI registered in step 1; set `OIDC_ENTRA_ALLOWED_DOMAINS` to the vetted hospital domains.
5. **Install Authlib.** It is in [requirements.txt](../requirements.txt); rebuild the `web` and `celery` images so the dependency is present (it is imported lazily, so a missing install surfaces as a `provider_unavailable` login failure, not a boot crash).
6. **Apply the migration.** `docker compose run --rm web python manage.py migrate user` (Postgres in the stack — never run this against the host SQLite database).
7. **Recreate the containers** so the new environment is picked up. The production overlay drops the code bind mount and reads env at container creation, so use `--force-recreate`, not `restart`.
8. **Rebuild the frontend** so the login screen renders the button.
9. **Verify.** Load `/login`, complete the Microsoft flow, confirm a session is established and an `ExternalIdentity` row is created, and check the `epicurrents.security` log shows no `auth.oidc_denied` for the test user.

### Gotchas

- **The redirect URI must match Entra exactly** — scheme, host, and path, including the absence of a trailing slash. A mismatch fails at the provider before the callback runs.
- **Dev cross-origin.** The flow assumes the SPA and API share an origin (the production stack serves both from Django). With the Vite dev server on `:5173` and Django on `:8000`, the session cookie set by the callback is not sent back to the SPA origin. Test OIDC against the built, same-origin stack, not the dev server.
- **`sub` is pseudonymous but still GDPR personal data.** It re-identifies the same person on every login, so `ExternalIdentity` rows fall under the same retention / erasure rules as `User` — it is not anonymous data.
- **Provisioned users have no usable password.** They cannot use the password form or password reset; that is intended.
- **Guest / B2B accounts are rejected by a non-empty allowlist.** Their UPN is mangled (`user_home.com#EXT#@tenant.onmicrosoft.com`) and yields no home domain.

## Subject access export (GDPR Art. 15)

The mirror of [account erasure](#account-erasure-gdpr-art-17): erasure answers "remove what you hold about me", this answers "show me". Both are operator-mediated, and for the same reason — establishing that a requester is the subject is not a software problem, and an endpoint handing a complete personal-data dossier to whoever holds a session is a worse answer than a person doing it on request.

```bash
docker compose run --rm web python manage.py export_user --username <name>                  # to stdout
docker compose run --rm web python manage.py export_user --username <name> --output out.txt
docker compose run --rm web python manage.py export_user --username <name> --format json     # Art. 20
```

The default output is a plain-text document with a section per kind of data, because the recipient is a person exercising a right rather than a system consuming a feed — a JSON tree asks the subject to parse it before they can judge whether it is complete. `--format json` remains for an Art. 20 portability request. Both render the same classified payload, so the two cannot describe different sets of data.

The document names what it left out, so a reader can tell a deliberate omission from a gap, and empty sections say "Nothing held" rather than disappearing — an absent heading and an empty one mean different things to someone checking. The run is audited under `user.export` with the subject's id — never their name, per the [activity metadata rule](../AGENTS.md#activity-metadata-carries-identifiers-never-names).

### What decides the contents

[`export.py`](export.py)'s `RELATION_HANDLING` classifies **every** reverse relation pointing at the user model. That is the design: an export's failure mode is silent omission, and a model added later would otherwise join the schema without joining the export, with nothing to notice until a subject received an incomplete answer. A relation nobody classified is a `manage.py check` error, not a default.

The set depends on what is installed — the active project adds between one and eight relations of its own — so projects and plugins register theirs from `AppConfig.ready()`:

```python
from user.export import register_export_relation

register_export_relation("myproject.mymodel", "author", fields=("title", "created_at"))
register_export_relation("myproject.other", "actor", omit_reason="why these rows are not the subject's data")
```

Three rules decide what goes in, and all three are about what stays out:

- **Credentials never.** Excluded by reusing `activity.audit`'s masked-field registry rather than a second list — it already declares which fields must never reach the audit trail in the clear, and the same fields must never leave in an export. A project registering a new credential field gets it excluded here without knowing the export exists.
- **Other people's data never**, even when reachable from the subject's own rows. The sharp case is `ObjectChangeLog`: the subject's edits are theirs, but the `before_state` / `changes` payloads describe the object edited, which may be someone else's recording. So the change log exports the *fact* of each change and never its payloads — Art. 15(4), drawn concretely.
- **Recording and media file contents never.** The export is about the account holder; those files are clinical data about the people recorded, a different set of data subjects entirely. Metadata rows are exported so the subject sees what they uploaded; the bytes stay behind the ordinary download endpoints.

### Gotchas

- The registry is validated at `manage.py check` ([checks.py](checks.py)), because every way of getting it wrong is quiet: an unclassified relation returns less than it should, a misspelled field raises `FieldError` while a legal deadline runs, and a registration for an uninstalled model is inert in a way that looks like "we hold none of that".
- `register_export_relation` takes the **lowercase** `app_label.model_name`, as `register_subject_pii` does.
- There is no self-service endpoint yet. When one lands it should reuse `export_user_data` rather than reimplement the classification.

## Account erasure (GDPR Art. 17)

There is deliberately no self-service deletion endpoint — erasure requests are operator-mediated on a clinical platform. The fulfilment path is the ⚠️ load-bearing [`erase_user`](management/commands/erase_user.py) management command:

```bash
docker compose run --rm web python manage.py erase_user <username>          # inventory (dry run)
docker compose run --rm web python manage.py erase_user <username> --yes    # erase
```

The command, in order:

1. **Unlinks owned recording and media files.** The `author` FK cascade removes the DB rows but never touches the filesystem; a bare `User.delete()` (admin, shell) strands PHI-bearing files on disk. A failed unlink aborts before any DB deletion; re-runs are safe.
2. **Flushes the subject's DB sessions.**
3. **Deletes the `User` row**, cascading to owned recordings, media, collections, datasets, tags, annotations, access rights, subscriptions, and identities — each cascade-deleted row audited per the usual signals under a `user.account.erase` Activity.
4. **Scrubs the audit trail** via [`activity.erasure.erase_subject`](../activity/README.md#subject-erasure-gdpr-art-17): tombstones every change-log row carrying the subject's PII while preserving hash-chain verifiability, and appends a chained `erase` record.

For an account that was already deleted through another path, `erase_user --user-id <pk> --yes` runs the audit-trail scrub alone.

Erasing a user does **not** erase patient data inside recordings they uploaded — those are the recordings' own data subjects, handled by the soft-delete + purge pipeline ([recordings/README.md](../recordings/README.md#soft-delete-and-purge)). The command deletes the user's recordings outright because ownership dies with the account; trash them beforehand if the deployment needs the retention window instead.

## Email delivery

`send_password_reset_email` ([tasks.py](tasks.py)) is the only outbound email path. It is fired from `request_password_reset` via `.delay()` so the HTTP response returns immediately regardless of the SMTP backend's latency, and delivery goes through `_deliver`, which wraps `django.core.mail.send_mail` with Celery retry semantics: up to 3 retries on SMTP failure, 60-second delay between attempts, recipients hashed before anything reaches the log stream.

The task takes a user primary key, not a rendered message. It mints the reset token and reads the address inside the worker, because Celery arguments cross a broker that persists them to an append-only file — a payload holding the reset URL would leave a token valid for three days sitting on disk next to the recipient's address, until an AOF rewrite that may be months away. Add mail flows in the same shape; there is deliberately no generic send-this-text-to-this-address task to reach for.

Email settings come from environment variables documented in [epicurrents/README.md](../epicurrents/README.md#settings-the-core-app-consumes-directly).

## Settings consumed

None directly — the user app reads `FRONTEND_URL`, `DEFAULT_FROM_EMAIL`, and the standard Django `AUTH_PASSWORD_VALIDATORS` and email backend settings from `settings`, but doesn't own them. All are documented in [epicurrents/README.md](../epicurrents/README.md#settings-the-core-app-consumes-directly).

## Project plugin extension points

| Hook | How |
|---|---|
| Add fields to the user model | Don't subclass `User`. Add a `OneToOneField(User)` profile model in your project. Access via the reverse relation you name. |
| Add a password validator | Extend `AUTH_PASSWORD_VALIDATORS` from your project's `settings.py`. The list is one of the append-merged settings, so your additions sit after the built-in validators. |
| Add a project-specific role | Attach the role to a group (a `OneToOneField(Group)` carrier model in your project, `GroupRole` say) and register a `RoleProvider` for it — see [project roles](#project-roles). Registering is what makes the role readable on `UserOut` (inherited through membership) and settable on groups from the account surface without core importing the project. Don't conflate with `is_staff` / `is_superuser` unless the role really maps to one of those tiers — see [epicurrents/README.md](../epicurrents/README.md#cross-app-rules-this-app-enforces). |

## Tests

```bash
pytest user/tests/
```

The `make_user` / `make_superuser` fixtures live in the root [conftest.py](../conftest.py) and use an `itertools.count` counter to generate unique usernames when none is passed. Pass `username=` explicitly when the test depends on a specific name.

## Gotchas

- **`update_session_auth_hash` on password change.** `change_password_endpoint` calls this after `set_password` so the active session survives the change. Without it the user would be logged out immediately. Mirror the call in any future endpoint that mutates the password.
- **The 429 response on `/reset-password` is per-address, not per-IP.** Two different email addresses from the same IP are independent. Different addresses produce different cache keys.
- **The `_RESET_RATE_WINDOW` has a frontend twin.** `LoginView.vue` has the same duration baked in (countdown timer + error copy). Change both or the UX diverges.
- **Reset confirmation invalidates pre-existing sessions implicitly.** `set_password` rotates the session auth hash, and `django.contrib.auth.get_user` rejects any session carrying the old hash on its next request — no explicit session flush is needed. The behaviour is pinned by `test_reset_invalidates_existing_sessions`; a custom auth backend or session machinery that drops the hash check would silently reopen the gap.
- **Role reads swallow provider errors.** `read_roles` / `read_group_roles` report a provider that raises as empty rather than propagating — the user app should never crash because a project's role table is broken or unmigrated. The trade-off is that a genuine database error silently reads as "no roles" rather than surfacing.
- **`POST /login` no longer returns a `UserOut`.** It returns `{ authenticated, two_factor_required, user }`, because a correct password is not always a session. A client that reads `response.data.username` gets `undefined` rather than an error; the mock server in [frontend/mocks.ts](../frontend/mocks.ts) returns the same envelope for that reason, even though the mock account has no second factor.
- **A code that was just used to confirm an enrolment will not work at the login prompt.** Confirmation spends its own time step. Signing out and back in within 30 seconds of enrolling needs the next code, which looks like a rejected setup and is not.
- **Login rate limit is per username, not per IP.** A distributed attacker hitting one username from many IPs is still locked out after 10 attempts. The flip side: an attacker hitting many usernames from one IP is not rate-limited at all here — IP-level rate limiting belongs at the reverse proxy (SWAG / nginx).
- **`is_active=False` users are filtered everywhere.** Login auth fails, search excludes them, password reset returns `200` but doesn't dispatch an email. Deactivating a user is the recommended path for revoking access without losing their authored content (recordings, annotations) via `CASCADE` on user delete.
