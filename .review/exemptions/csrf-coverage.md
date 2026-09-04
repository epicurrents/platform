# CSRF-coverage exemptions

Exemption registry for the [`csrf-coverage` agent](../agents/csrf-coverage.md).
Sibling registries for other agents live next to this file in
[.review/exemptions/](.); see that directory's [README](README.md)
for the convention.

The CSRF-coverage rules (C1–C3 in the agent spec) are the strict
default: every session-authenticated unsafe-method request routes
through `enforce_session_csrf`. Listing an item here means the agent
treats its deviation as an audited intentional choice rather than a
finding.

## How to add an entry

1. Add a row with the precise locator — for an endpoint, the HTTP
   method + path exactly as written in the `@api.*` decorator; for a
   view, the file path + function name.
2. Name the exempt check(s) by C-code (`C1` / `C2` / `C3`).
3. Write a one-line reason explaining *why* the request can never be a
   CSRF vector — typically that no session-cookie caller reaches the
   write, so there is nothing for the chokepoint to protect.

## Current exemptions

| Locator | Exempt from | Reason |
|---|---|---|
| `POST /api/v1/user/login` | C1 | Pre-auth endpoint: it *establishes* the session rather than consuming one, so there is no authenticated session caller and no CSRF token to check. SameSite=Lax plus the per-username login rate limit are its cross-site defences. |
| `POST /api/v1/user/reset-password` | C1 | Pre-auth: the caller is unauthenticated by definition. SameSite=Lax plus the per-email reset cooldown are its defences. |
| `POST /api/v1/user/reset-password/confirm` | C1 | Pre-auth: authenticated by a single-use reset token carried in the body, not the session cookie. Not a browser-attached credential, so not a CSRF vector. |
| Annotation CRUD authenticated via `?share_token=` | C1 | Share-token callers authenticate by a query-param token a browser does not attach automatically; these requests never reach the session chokepoint. The session-cookie path on the same endpoints *is* covered (it routes through `_require_auth`). |
| FederatedBearer-authenticated endpoints (federation inbound, recording download/check) | C1 | FederatedBearer JWT is a header credential a browser does not attach automatically. The federated branch of `_require_auth_or_federated` is intentionally outside the chokepoint; the session branch is covered. |
| `POST /api/v1/user/logout` | C1 | The one row that is not of the no-session-caller shape (see below). A forged call ends the victim's session and writes the `user.logout` audit row for it; no data the user owns is touched, the `{"status": "ok"}` response tells the attacker nothing it did not already know, and the remedy is to sign in again. Enforcing here would also make signing out fail on a stale token, which is when signing out matters most. |

## When to add the next exemption

Two shapes are legitimate.

**No session-cookie caller can reach the write.** Either the endpoint
is pre-auth (it creates the session), or it authenticates by a
credential a browser does not attach automatically (a body token, a
query-param share token, a bearer JWT). Every row above except logout
is this shape.

**A forged call has nothing worth preventing.** Logout is the only
known instance: it ends the session the forgery was made against and
touches nothing beyond it, so success costs the victim a re-login and
yields the attacker nothing. The shape is narrow by construction — an
endpoint qualifies only if a forged call leaves every row the user owns
unchanged, returns nothing the attacker could not already predict, and
is undone by an action the user can take unaided. Writing the audit row
for the act itself does not disqualify it; writing, deleting or
modifying anything else does, however small.

If a session cookie *can* authenticate the request and neither shape
applies, it must route through `enforce_session_csrf` — convenience,
"the SPA always sends the token", or "it's read-mostly" are not
exemption shapes.

## Re-scoping when behaviour changes

An exemption is tied to the constraint that justifies it. For the
no-session-caller rows that constraint is unreachability: if a pre-auth
endpoint gains an authenticated mode, or a token-only endpoint starts
accepting the session cookie, revisit the exemption in the same change
pass and route the new session path through the chokepoint. For logout
it is emptiness of effect: if signing out grows a side effect — spending
a token, writing an audit-adjacent row of its own, revoking something
beyond the one session — the row no longer holds and the endpoint calls
`enforce_session_csrf` like any other write.

## Related

- Agent that consults this file:
  [.review/agents/csrf-coverage.md](../agents/csrf-coverage.md).
- Rule source: [AGENTS.md](../../AGENTS.md) →
  *Session-authenticated write CSRF*.
- Load-bearing chokepoint:
  [epicurrents/auth.py](../../epicurrents/auth.py).
