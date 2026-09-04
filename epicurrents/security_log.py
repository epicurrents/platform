"""Structured logger for security-relevant events.

⚠️ LOAD-BEARING — SIEM rule surface.
Three pieces of this module are operator-visible API that SIEM rules
pivot on:
1. The logger name ``"epicurrents.security"`` (line below) — log shippers
   filter on this name.
2. The structured-log key ``"security_event_type"`` (passed via
   ``extra=...``) — alert rules filter on this field.
3. The well-known set of ``event_type`` tokens enumerated below — rules
   match specific tokens like ``auth.login_failed``.
A silent rename of any of these — without coordinating the SIEM
configuration — leaves the application logging happily while every
downstream rule stops matching.  See AGENTS.md → *Load-bearing files*
before modifying.  The contract tests are
``epicurrents/tests/test_security_log_taxonomy.py`` (event-type set)
and ``epicurrents/tests/test_security_log_emission.py`` (logger name
and extra-key shape).

Emits WARNING entries to the ``epicurrents.security`` logger so that operators
can route them to a SIEM / log shipper (Loki, ELK, Sentry, Datadog, …) and
build alerting on top. The format is intentionally narrow: a stable
``event_type`` token plus structured ``extra`` fields. Avoid free-form
messages — pattern-matching downstream depends on the ``event_type`` set
remaining well-known.

Event types in current use:

- ``auth.login_failed`` — single failed login attempt (one entry per attempt).
- ``auth.login_lockout`` — failure threshold reached; the account is now in
  the lockout window.
- ``auth.login_blocked`` — login attempt during an active lockout.
- ``auth.password_reset_rate_limited`` — repeated reset requests for one email.
- ``auth.2fa_failed`` — a second-factor code was rejected. ``actor_id``
  identifies the account; ``attempts`` counts consecutive failures at the login
  prompt, and ``phase="enrolment"`` marks the confirmation step instead, where
  a wrong code is an unscanned QR rather than a guess.
- ``auth.2fa_blocked`` — a code was submitted during an active lockout, the
  second-factor counterpart of ``auth.login_blocked``. The lockout itself is
  reported by the last ``auth.2fa_failed`` before it, whose ``attempts`` field
  reaches the threshold.
- ``auth.2fa_backup_used`` — a login consumed a recovery code rather than a
  generated one. Expected occasionally and worth watching in bulk: ``remaining``
  carries how many are left, and a run of these across accounts is what
  authenticator-stripping looks like.
- ``auth.2fa_enrolment_required`` — a correct password was refused a session
  because the deployment requires a second factor and the account has none.
  ``actor_id``. Not a failure: the login continues into enrolment. Worth a rule
  only as a volume signal — a sustained rise after either
  ``TWO_FACTOR_REQUIRED_*`` setting is switched on means accounts are meeting
  the requirement and not completing it.
- ``auth.2fa_enrolled`` — an account confirmed a second factor for itself.
  ``actor_id`` identifies it.
- ``auth.2fa_disabled`` — an account removed its own second factor, having
  re-confirmed its password. ``actor_id`` identifies it.
- ``auth.2fa_reset`` — an operator removed an account's second factor through
  the administration surface. ``actor_id`` is the operator, ``target_id`` the
  account. The intended use is a lost authenticator, and the reason to alert on
  it is that it is also how an attacker with an operator session would disarm
  an account before taking it over.
- ``auth.2fa_reauth_failed`` — a wrong password at the re-confirmation step
  guarding a change to an account's own second factor. ``actor_id`` identifies
  the session's account; a run of these means a hijacked session probing for
  the password rather than a user mistyping it.
- ``auth.oidc_denied`` — an OpenID Connect (external) login was rejected. The
  ``reason`` field carries the cause: ``provider_error``, ``state_mismatch``,
  ``invalid_token``, ``issuer_mismatch``, ``audience_mismatch``,
  ``tenant_mismatch``, ``nonce_mismatch``, ``domain_not_allowed``,
  ``auto_create_disabled``, ``inactive_user``, or ``provider_unavailable``.
  The ``provider`` field names the identity provider (e.g. ``entra``). No
  email / subject is logged — the reason token is sufficient.
- ``permission.denied`` — a centralised ``ensure_*`` permission check refused
  the request. The specific permission (read / write / modify / annotate)
  is carried in the ``permission`` field.
- ``permission.grant_amplification_refused`` — a grant-creation request tried
  to confer rights beyond those the grantor holds on the object (write without
  write, raw bytes without raw access, or an expiry outliving the grantor's
  own share grant). ``actor_id``, ``object_type``, ``object_id``, ``reason``
  (the refused dimension). A run of these from one actor is what probing the
  delegation cap looks like.
- ``permission.author_grant_revoke_refused`` — a share-holder tried to revoke
  the object author's own access-right row, which would lock the author out
  of read paths that resolve through it. ``actor_id``, ``object_type``,
  ``object_id``.
- ``federation.auth_failed`` — inbound FederatedBearer JWT rejected at
  ``parse_federation_auth``. The ``reason`` field carries the specific cause
  (malformed, expired, unknown peer, signature, replay, audience, …).
- ``notifications.subscription_rejected`` — a push-subscription registration
  was refused. The ``reason`` field carries the cause: ``non_https_endpoint``
  or ``unsafe_endpoint`` (the endpoint URL failed the SSRF guard), or
  ``endpoint_owned_by_other_user`` (an attempt to re-register an endpoint
  already bound to another account; the request is acknowledged but the
  existing row is left untouched). ``actor_id`` identifies the caller.
- ``audit.hash_verification_failed`` — an ``ObjectChangeLog`` row's stored
  ``after_hash`` does not match a fresh recompute of its contents. Emitted
  from the rollback path when refusing to restore a tampered (or corrupted)
  row. ``change_id``, ``content_type``, ``object_id``, ``action``, and
  ``actor_id`` fields carry the locator.
- ``audit.chain_break`` — periodic ``verify_audit_integrity`` walked a
  per-content_type chain and found a row whose ``after_hash`` no longer
  recomputes (or whose ``prev_hash`` does not match the previous row's
  ``after_hash``). Fields: ``content_type``,
  ``first_break_sequence_no``, ``downstream_break_count``.
- ``audit.chain_gap`` — periodic check found missing ``sequence_no``
  values inside an otherwise-populated chain shard. Fields:
  ``content_type``, ``missing_sequence_nos``.
- ``audit.genesis_invalid`` — the first row of a v3 chain shard does
  not reference the per-shard genesis sentinel; either the chain was
  lifted from another shard or the first row's ``prev_hash`` was
  tampered with. Field: ``content_type``.
- ``audit.hash_key_missing`` — periodic check could not verify a chain
  because the row's ``hash_key_version`` is absent from
  ``settings.ACTIVITY_HASH_KEYS``. Distinct from
  ``audit.hash_verification_failed``: that one means the row's hash
  didn't recompute; this one means the key needed to recompute was
  unavailable. Fields: ``content_type``, ``reason``.
- ``audit.derived_state_mismatch`` — periodic check recomputed a
  derived-row digest (e.g. ``signal_info_digest`` for a ``Recording``)
  and the result diverged from the value stored in
  ``ObjectChangeLog.extra_payload``. Fields: ``change_id``,
  ``content_type``, ``object_id``, ``digest_key``.
- ``audit.derived_state_no_digester`` — an ``extra_payload`` key has
  no registered digester so the value cannot be recomputed at check
  time. Operational signal, not a tamper alarm: usually means a
  module deregistered a digester or a row predates a digester
  rename. Fields: ``change_id``, ``content_type``, ``digest_key``.
- ``throttle.rate_limited`` — an API request exceeded the global
  per-identity request-rate ceiling and was rejected with 429. Fields:
  ``scope`` (the throttle scope the path mapped to), ``identity_kind``
  (``user`` / ``token`` / ``session`` / ``ip``), ``limit``, ``count``,
  ``path``. The raw identity (user PK, hashed token, session key, IP)
  is deliberately not logged here — only its kind — so the stream
  carries no per-subject identifier.

- ``system.heartbeat`` — periodic liveness signal, emitted by
  ``epicurrents.tasks.emit_security_heartbeat`` on a schedule. Alone among
  these it reports that nothing is wrong, and it exists because an
  off-host log sink can only alert on *absence*: a stream carrying
  security events alone is silent on a healthy system, which is
  indistinguishable from a stream that has been cut. Its absence is the
  alarm. What it proves is the whole path — beat scheduler, worker,
  logging configuration, shipper, network — not merely that a host is
  powered on; what it does not prove is that the ``web`` container is
  serving, which is an uptime check's job. Fields: ``interval_seconds``,
  so a receiver can derive its own alerting window rather than having one
  hard-coded on both sides. Carries no identifier of any kind.

Add new event types by extending this docstring and using the helper —
do not log security events through ad-hoc ``logger.warning`` calls so the
downstream rule set stays maintainable.
"""

import ipaddress
import logging
from typing import Any

logger = logging.getLogger("epicurrents.security")


def log_security_event(event_type: str, **fields: Any) -> None:
    """Emit a structured WARNING for a security-relevant event.

    ``event_type`` must come from the well-known set documented in the module
    docstring. ``fields`` may include any of: ``actor_id``, ``ip``, ``path``,
    ``method``, ``permission``, ``reason``, ``object_type``, ``peer_url``,
    ``peer_id``. Unknown fields are accepted and emitted as-is.

    **Caller responsibility — no raw PII in the log stream.** Per AGENTS.md
    → *Log security-related activity*, callers must hash usernames, email
    addresses, and other personal identifiers before passing them in
    ``fields`` (the login + password-reset endpoints in
    ``user/api/v1/ninja.py`` are the canonical pattern).  ``actor_id``
    (an integer FK) is fine; ``username`` / ``email`` are not.  The
    helper does not enforce this; the rule is at the call site.

    The human-readable message is a single line ``security: <event_type>
    actor=... ip=... ...`` so ``grep epicurrents.security`` over a log file
    produces a parseable stream even without a SIEM.
    """
    parts = [f"{key}={value!r}" for key, value in fields.items() if value is not None]
    summary = " ".join(parts) if parts else "-"
    logger.warning(
        "security: %s %s",
        event_type,
        summary,
        extra={"security_event_type": event_type, **fields},
    )


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    """Parse ``settings.TRUSTED_PROXIES`` into ``ipaddress`` network objects.

    Invalid CIDR entries are skipped with a logged warning rather than
    raising — a misconfigured entry should disable that single trust hop,
    not crash the request. Empty / unset setting returns an empty list,
    which makes ``get_client_ip`` ignore ``X-Forwarded-For`` entirely.
    """
    from django.conf import settings

    networks: list[ipaddress._BaseNetwork] = []
    for entry in getattr(settings, "TRUSTED_PROXIES", []) or []:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except (ValueError, TypeError):
            logger.warning(
                "Invalid TRUSTED_PROXIES entry %r (not a valid CIDR); this hop will be treated as untrusted.",
                entry,
            )
    return networks


def _is_trusted_proxy(remote_addr: str | None) -> bool:
    if not remote_addr:
        return False
    try:
        addr = ipaddress.ip_address(remote_addr)
    except (ValueError, TypeError):
        return False
    return any(addr in net for net in _trusted_proxy_networks())


def get_client_ip(request) -> str | None:
    """Best-effort extraction of the client IP from a Django request.

    Trust model: ``X-Forwarded-For`` is honoured only when the immediate hop
    (``REMOTE_ADDR``) falls inside one of the CIDR ranges in
    ``settings.TRUSTED_PROXIES``. Otherwise ``REMOTE_ADDR`` is returned and
    the header is ignored — even if the caller supplied one.

    When ``TRUSTED_PROXIES`` is empty (default), every ``X-Forwarded-For``
    header is treated as caller-supplied and discarded. Operators behind
    nginx / Traefik / Cloudflare must opt in by adding the proxy's source
    network to ``TRUSTED_PROXIES`` (CSV in .env). Until they do,
    security-log IPs will be the proxy's address rather than the original
    client — visible degradation, no spoofing surface.

    Returns ``None`` if no source address is available at all.
    """
    remote_addr = request.META.get("REMOTE_ADDR")
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded and _is_trusted_proxy(remote_addr):
        return forwarded.split(",")[0].strip()
    return remote_addr
