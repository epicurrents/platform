"""One failure budget for every surface that checks an account's password or second factor.

Three places verify an account's credentials: the login form, keyed on the
username typed into it; the login code prompt, keyed on the account the
password step identified; and step-up confirmation (:mod:`user.stepup`), keyed
on the signed-in account. Each has its own lockout — ten failures per five
minutes at login and at the code prompt, five at step-up — and with separate
counters an attacker holding a session could spend every budget on one account
and get fifteen password guesses where either surface alone allows ten. So a
step-up failure charges the counter of the credential that failed as well as
its own, a wrong code at the login prompt charges step-up too, and step-up
refuses while the login form is locked for the account's username: the login
threshold bounds the total.

A wrong password at the login form deliberately does not charge step-up. The
login form is open to anyone, and step-up's threshold is the lower one, so
charging it would let a stranger lock a superuser out of confirming a
rollback with five anonymous guesses; honouring the login lockout instead
gives a stranger nothing beyond the login lockout they could always cause.

A success clears only the counters of the credentials it proved. Clearing more
would reopen the gap from the other side: an attacker who knows the password
but not the second factor could otherwise sign in with the password between
batches of guessed codes and start every batch with a fresh budget.

Each counter is incremented atomically (``cache.add`` then ``cache.incr``) so
concurrent failures cannot overwrite each other's count, which a
read-modify-write ``get`` / ``set`` pair allowed: ten parallel wrong passwords
all read the same count and each wrote back one more than it.

The keys match the ones the login endpoint has always used, so a lockout set
before this module existed is still honoured.
"""

import hashlib

from django.core.cache import cache

#: Failures, on any surface, before the login form refuses the username.
LOGIN_MAX_ATTEMPTS = 10
#: Failures, on any surface, before step-up confirmation refuses the account.
STEPUP_MAX_ATTEMPTS = 5
#: Both lockouts, and the counters leading up to them, last this long.
LOCKOUT_SECONDS = 5 * 60


def username_key(username: str) -> str:
    """The cache-key form of a username: a SHA-256 of its stripped, lower-cased text, never the text itself."""
    return hashlib.sha256(username.strip().lower().encode()).hexdigest()


def login_keys(key: str) -> tuple[str, str]:
    """``(attempt_key, lockout_key)`` for the login counter of a hashed username."""
    return f"login_attempts:{key}", f"login_lockout:{key}"


def login_2fa_keys(user_pk: int) -> tuple[str, str]:
    """``(attempt_key, lockout_key)`` for the login code prompt's counter of an account."""
    return f"login_2fa_attempts:{user_pk}", f"login_2fa_lockout:{user_pk}"


def stepup_keys(user_pk: int) -> tuple[str, str]:
    """``(attempt_key, lockout_key)`` for the step-up counter of an account."""
    return f"stepup_attempts:{user_pk}", f"stepup_lockout:{user_pk}"


def _increment(attempt_key: str, lockout_key: str, threshold: int) -> int:
    """Count one failure on a counter and lock it once ``threshold`` is reached; return the new count."""
    cache.add(attempt_key, 0, timeout=LOCKOUT_SECONDS)
    try:
        attempts = cache.incr(attempt_key)
    except ValueError:
        # The key expired between the add and the incr. Starting again at one
        # is the right answer: the window it belonged to has closed.
        cache.add(attempt_key, 1, timeout=LOCKOUT_SECONDS)
        attempts = 1
    if attempts >= threshold:
        cache.set(lockout_key, 1, timeout=LOCKOUT_SECONDS)
        cache.delete(attempt_key)
    return attempts


def login_locked(key: str) -> bool:
    """Whether the login form currently refuses the hashed username ``key``."""
    return bool(cache.get(login_keys(key)[1]))


def stepup_locked(user) -> bool:
    """Whether step-up confirmation currently refuses ``user``: its own lockout, or the login lockout of its username."""
    return bool(cache.get(stepup_keys(user.pk)[1])) or login_locked(username_key(user.get_username()))


def login_2fa_locked(user_pk: int) -> bool:
    """Whether the login code prompt currently refuses the account."""
    return bool(cache.get(login_2fa_keys(user_pk)[1]))


def record_login_failure(username: str) -> int:
    """Count a wrong password at the login form; return the login counter's new value.

    Only the login counter: step-up honours its lockout instead of sharing its
    count (see the module docstring).
    """
    return _increment(*login_keys(username_key(username)), LOGIN_MAX_ATTEMPTS)


def record_code_failure(user) -> int:
    """Count a wrong code at the login code prompt; return that prompt's counter's new value.

    Also charges the account's step-up counter, where the same second factor is
    guessed.
    """
    attempts = _increment(*login_2fa_keys(user.pk), LOGIN_MAX_ATTEMPTS)
    _increment(*stepup_keys(user.pk), STEPUP_MAX_ATTEMPTS)
    return attempts


def record_stepup_failure(user, *, second_factor: bool) -> int:
    """Count a failed step-up confirmation; return the step-up counter's new value.

    Also charges the counter of the surface that guards the credential that
    failed: the login counter under the account's own username for a password,
    the login code prompt's for a second factor.
    """
    attempts = _increment(*stepup_keys(user.pk), STEPUP_MAX_ATTEMPTS)
    if second_factor:
        _increment(*login_2fa_keys(user.pk), LOGIN_MAX_ATTEMPTS)
    else:
        _increment(*login_keys(username_key(user.get_username())), LOGIN_MAX_ATTEMPTS)
    return attempts


def clear_login(username: str) -> None:
    """Forget the login counter and lockout for ``username``, after that username's password was proven."""
    for cache_key in login_keys(username_key(username)):
        cache.delete(cache_key)


def clear_login_2fa(user_pk: int) -> None:
    """Forget the login code prompt's counter and lockout, after the account's second factor was proven."""
    for cache_key in login_2fa_keys(user_pk):
        cache.delete(cache_key)


def clear_stepup(user_pk: int) -> None:
    """Forget the step-up counter and lockout, after a step-up confirmation passed in full."""
    for cache_key in stepup_keys(user_pk):
        cache.delete(cache_key)
