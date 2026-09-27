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
Failures count towards a per-account lockout and are logged as
``auth.stepup_failed``. The lockout shares its failures with the login form and
the login code prompt through :mod:`user.lockout`, so a session holder cannot
alternate between surfaces to get more guesses than either allows.
"""

from ninja.errors import HttpError

from epicurrents.security_log import get_client_ip, log_security_event
from user import lockout
from user.two_factor import active_credential, consume_backup_code, consume_totp

MAX_FAILURES = lockout.STEPUP_MAX_ATTEMPTS
LOCKOUT_SECONDS = lockout.LOCKOUT_SECONDS

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


def _fail(request, user, reason: str) -> None:
    attempts = lockout.record_stepup_failure(user, second_factor=reason == "second_factor")
    log_security_event(
        "auth.stepup_failed", ip=get_client_ip(request), actor_id=user.pk, reason=reason, attempts=attempts
    )
    raise HttpError(400, "Confirmation failed.")


def check_stepup_lockout(request, user) -> None:
    """Raise 429 while ``user`` is locked out of step-up, logging the refused attempt.

    Exposed for the password re-checks outside this module — the two-factor
    management endpoints and change-password — which draw on the same budget.
    """
    if lockout.stepup_locked(user):
        log_security_event("auth.stepup_failed", ip=get_client_ip(request), actor_id=user.pk, reason="locked_out")
        raise HttpError(429, "Too many failed confirmations. Try again later.")


def record_password_recheck_failure(user) -> None:
    """Charge a wrong password at a re-check outside this module to the shared budget."""
    lockout.record_stepup_failure(user, second_factor=False)


def confirm_step_up(
    request, user, *, password: str | None = None, totp_code: str | None = None, second_factor: bool = True
):
    """Confirm the caller's credentials or raise.

    ``password`` is required for an account that has one; ``totp_code`` (a TOTP
    or a backup code) is required when the account has a confirmed second factor
    and ``second_factor`` is on, and regardless of ``second_factor`` for an
    account whose factor is its only credential. An account with neither
    answers 409, since the state of the account rather than the request is the
    problem. A wrong or
    missing credential answers 400 without saying which, counts toward the
    lockout — and toward the login lockout guarding the same credential — and is
    logged; five failures lock the account out of step-up for five minutes and
    answer 429.
    """
    method = step_up_method(user)
    if method is None:
        raise HttpError(
            409,
            "This account signs in through an external provider and has no second factor; it cannot confirm.",
        )
    check_stepup_lockout(request, user)

    if user.has_usable_password() and (not password or not user.check_password(password)):
        _fail(request, user, "password")

    credential = active_credential(user)
    # An account without a password has only its factor to confirm with, so a
    # caller waiving the second factor still gets the code checked there;
    # otherwise the waiver would confirm with nothing at all.
    factor_checked = credential is not None and (second_factor or not user.has_usable_password())
    if factor_checked:
        # The login code prompt guards the same factor, so its lockout holds
        # here too; otherwise its ten guesses would come on top of these five.
        if lockout.login_2fa_locked(user.pk):
            log_security_event("auth.stepup_failed", ip=get_client_ip(request), actor_id=user.pk, reason="locked_out")
            raise HttpError(429, "Too many failed confirmations. Try again later.")
        code = (totp_code or "").strip()
        if not code or not (consume_totp(credential, code) or consume_backup_code(credential, code)):
            _fail(request, user, "second_factor")

    # Clear only what was proven. A confirmation that waived an enrolled second
    # factor proved the password alone, and the step-up counter also holds
    # failed codes: resetting it there would let a caller who knows the
    # password interleave waived confirmations with guessed codes indefinitely.
    if credential is None or factor_checked:
        lockout.clear_stepup(user.pk)
    if user.has_usable_password():
        lockout.clear_login(user.get_username())
    if factor_checked:
        lockout.clear_login_2fa(user.pk)
    return method
