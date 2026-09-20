"""Step-up confirmation: proving the caller is still the account's owner before a sensitive action.

A session cookie proves someone signed in once; it does not prove that the
person at the keyboard now is the one who did. Before an action that changes
the platform itself — requesting an update, rolling one back — the caller
confirms with a fresh credential: the account's password, plus its second
factor when one is enrolled. An account without a usable password, provisioned
through an external provider, confirms with the second factor alone; without
one it has nothing to confirm with, and :func:`step_up_method` says so ahead of
time so a UI can explain rather than fail.

Generalises the password re-check the two-factor management endpoints do.
Failures count towards a per-account lockout, mirroring the login lockout, and
are logged as ``auth.stepup_failed``.
"""

from django.core.cache import cache
from ninja.errors import HttpError

from epicurrents.security_log import get_client_ip, log_security_event
from user.two_factor import active_credential, consume_backup_code, consume_totp

MAX_FAILURES = 5
LOCKOUT_SECONDS = 5 * 60

METHOD_PASSWORD = "password"
METHOD_TOTP = "totp"
METHOD_PASSWORD_AND_TOTP = "password+totp"


def step_up_method(user) -> str | None:
    """Which credentials ``user`` confirms with, or ``None`` when the account has none it can present."""
    has_password = user.has_usable_password()
    has_factor = active_credential(user) is not None
    if has_password and has_factor:
        return METHOD_PASSWORD_AND_TOTP
    if has_password:
        return METHOD_PASSWORD
    if has_factor:
        return METHOD_TOTP
    return None


def _fail(request, user, reason: str, attempt_key: str, lockout_key: str) -> None:
    log_security_event("auth.stepup_failed", ip=get_client_ip(request), actor_id=user.pk, reason=reason)
    attempts = (cache.get(attempt_key) or 0) + 1
    if attempts >= MAX_FAILURES:
        cache.set(lockout_key, 1, timeout=LOCKOUT_SECONDS)
        cache.delete(attempt_key)
    else:
        cache.set(attempt_key, attempts, timeout=LOCKOUT_SECONDS)
    raise HttpError(400, "Confirmation failed.")


def confirm_step_up(
    request, user, *, password: str | None = None, totp_code: str | None = None, second_factor: bool = True
):
    """Confirm the caller's credentials or raise.

    ``password`` is required for an account that has one; ``totp_code`` (a TOTP
    or a backup code) is required when the account has a confirmed second factor
    and ``second_factor`` is on. An account with neither answers 409, since the
    state of the account rather than the request is the problem. A wrong or
    missing credential answers 400 without saying which, counts toward the
    lockout, and is logged; five failures lock the account out of step-up for
    five minutes and answer 429.
    """
    method = step_up_method(user)
    if method is None:
        raise HttpError(
            409,
            "This account signs in through an external provider and has no second factor; it cannot confirm.",
        )
    attempt_key = f"stepup_attempts:{user.pk}"
    lockout_key = f"stepup_lockout:{user.pk}"
    if cache.get(lockout_key):
        log_security_event("auth.stepup_failed", ip=get_client_ip(request), actor_id=user.pk, reason="locked_out")
        raise HttpError(429, "Too many failed confirmations. Try again later.")

    if user.has_usable_password() and (not password or not user.check_password(password)):
        _fail(request, user, "password", attempt_key, lockout_key)

    credential = active_credential(user)
    if credential is not None and second_factor:
        code = (totp_code or "").strip()
        if not code or not (consume_totp(credential, code) or consume_backup_code(credential, code)):
            _fail(request, user, "second_factor", attempt_key, lockout_key)

    cache.delete(attempt_key)
    cache.delete(lockout_key)
    return method
