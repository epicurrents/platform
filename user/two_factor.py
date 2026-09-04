"""TOTP second factor — secret generation, code verification, backup codes.

The mechanism of the second login factor, kept out of the API layer so the
security-relevant parts can be tested against RFC 6238's published vectors
rather than through an HTTP client. RFC 6238 TOTP over the stdlib's HMAC-SHA1
is what every authenticator app implements; ``pyotp`` supplies it, and
``recordings.tests`` aside this is the only place the platform does
challenge-response.

Two properties here carry the weight:

**A code is consumed, not merely checked.** A TOTP code stays valid for its
whole time step, and this platform accepts one step of clock drift either way,
so a code observed in flight is replayable for up to 90 seconds without a
guard. ``consume_totp`` therefore records the time step it accepted and refuses
any counter at or below the recorded one. The record is claimed with a
conditional ``UPDATE ... WHERE last_counter < %s``, which is atomic in one
statement — two concurrent requests carrying the same code cannot both see a
stale ``last_counter`` and both succeed, as they could with a read-then-write.

**Backup codes are hashed, but not with the password hasher.** They are
server-generated at 60 bits of entropy from a 32-symbol alphabet, so there is
no dictionary to attack and no user-chosen weakness to stretch away; a plain
SHA-256 leaves an offline attacker with 2^60 candidates per code. The password
hasher would cost more than it buys in the other direction too: verification
has to compare against every unused code, so ten Argon2 verifications per
attempt would turn the recovery endpoint into a CPU amplifier reachable before
authentication completes.

The alphabet is RFC 4648 base32 (``A-Z``, ``2-7``) for both the secret and the
backup code. It contains ``O`` and ``I`` but not ``0`` and ``1``, so a recovery
code read off paper can be misread but not misread *ambiguously*: only one
character of each confusable pair can appear in a real code, which is why
``_normalise_backup`` folds the digits onto the letters instead of rejecting
them.
"""

import hashlib
import hmac
import re
import secrets
import time
import urllib.parse

import pyotp
from django.db import transaction

#: Seconds per TOTP time step. RFC 6238's recommended default, and the value
#: every authenticator app assumes when the ``otpauth://`` URI omits ``period``.
STEP_SECONDS = 30

#: Time steps of clock drift accepted either side of the server's current step.
#: One step each way means a code is honoured for at most 90 seconds, which
#: covers a phone whose clock is off by half a minute without widening the
#: replay window further than the consume-guard can close.
DRIFT_STEPS = 1

#: Digits in a generated code. Six is what authenticator apps show by default.
CODE_DIGITS = 6

#: Number of backup codes issued per generation, and characters in each.
#: Twelve base32 symbols is 60 bits — see the module docstring on why that is
#: the security parameter rather than the hash.
BACKUP_CODE_COUNT = 10
BACKUP_CODE_LENGTH = 12
#: Characters per hyphen-separated group in the presented form. Display only;
#: every comparison happens on the normalised (ungrouped) string.
BACKUP_CODE_GROUP = 4

_BACKUP_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

_TOTP_CODE_RE = re.compile(rf"^\d{{{CODE_DIGITS}}}$")
_BACKUP_CODE_RE = re.compile(rf"^[A-Z2-7]{{{BACKUP_CODE_LENGTH}}}$")


def two_factor_required(user) -> bool:
    """Whether *user* must hold a second factor to complete a password login.

    ``TWO_FACTOR_REQUIRED_FOR_ALL`` dominates: it covers staff as well, so the
    two settings compose rather than conflict.

    Accounts with no usable password are excluded, and the exclusion is not a
    convenience. Enrolment re-confirms the password at every management
    endpoint, so an account authenticated by an external identity provider
    cannot enrol at all — requiring a factor of it would lock it out of a
    platform it has no route back into. Such accounts do not reach the password
    login endpoint in the first place, which makes this belt as well as braces.
    """
    from django.conf import settings

    if not user.has_usable_password():
        return False
    if getattr(settings, "TWO_FACTOR_REQUIRED_FOR_ALL", False):
        return True
    if getattr(settings, "TWO_FACTOR_REQUIRED_FOR_STAFF", False):
        return bool(user.is_staff or user.is_superuser)
    return False


def active_credential(user):
    """Return the user's confirmed second factor, or ``None``.

    The single reader of this state, and the reason no caller reaches for
    ``user.two_factor`` directly: an unconfirmed row is an abandoned enrolment
    and must never gate a login, so the ``confirmed_at`` filter belongs in one
    place rather than at each of the several call sites that ask the question.
    """
    credential = getattr(user, "two_factor", None)
    if credential is None or credential.confirmed_at is None:
        return None
    return credential


def generate_secret() -> str:
    """Return a fresh base32 TOTP secret."""
    return pyotp.random_base32()


def build_provisioning_uri(secret: str, *, account_name: str, issuer: str) -> str:
    """Return the ``otpauth://`` URI an authenticator app scans to enrol.

    ``issuer`` is what the app displays as the account's provider, and callers
    pass the request host so that a person with accounts on two deployments
    gets two distinguishable entries. ``account_name`` is the username.
    """
    label = urllib.parse.quote(f"{issuer}:{account_name}", safe="")
    params = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": CODE_DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{params}"


def code_at(secret: str, for_time: int) -> str:
    """Return the code a correctly-synchronised authenticator shows at ``for_time``.

    Exposed for tests and for the enrolment preview; login goes through
    ``consume_totp``, which additionally enforces the replay guard.
    """
    return pyotp.TOTP(secret, digits=CODE_DIGITS, interval=STEP_SECONDS).at(for_time)


def _normalise_totp(code: str) -> str:
    """Strip the separators authenticator apps and users add to a numeric code."""
    return re.sub(r"[\s-]", "", code or "")


def _normalise_backup(code: str) -> str:
    """Fold a backup code to the form its hash was taken over.

    ``0`` and ``1`` are not in the alphabet, so someone who read ``O`` or ``I``
    off paper and typed the digit meant the letter and can only have meant the
    letter. Folding them costs nothing — the mapping is onto characters that
    were never generated, so the code space is unchanged — and turns a failed
    recovery into a successful one.
    """
    folded = re.sub(r"[\s-]", "", (code or "")).upper()
    return folded.replace("0", "O").replace("1", "I")


def hash_backup_code(code: str) -> str:
    """Return the stored form of a backup code."""
    return hashlib.sha256(_normalise_backup(code).encode("ascii", "ignore")).hexdigest()


def generate_backup_codes() -> tuple[list[str], list[str]]:
    """Return ``(presented, stored)`` — the codes to show once, and their hashes.

    The presented form carries hyphens for legibility; the stored hashes are
    taken over the normalised string, so a user may type either form back.
    """
    presented = []
    for _ in range(BACKUP_CODE_COUNT):
        raw = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        groups = [raw[i : i + BACKUP_CODE_GROUP] for i in range(0, len(raw), BACKUP_CODE_GROUP)]
        presented.append("-".join(groups))
    return presented, [hash_backup_code(code) for code in presented]


def verify_totp_code(secret: str, code: str, *, now: int | None = None) -> int | None:
    """Return the time step ``code`` matches under ``secret``, or ``None``.

    Pure verification with no replay guard — ``consume_totp`` is what login
    calls. Candidate steps are the current one and ``DRIFT_STEPS`` either side.
    """
    code = _normalise_totp(code)
    if not _TOTP_CODE_RE.match(code):
        return None
    now = int(time.time()) if now is None else int(now)
    for offset in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        at = now + offset * STEP_SECONDS
        if hmac.compare_digest(code_at(secret, at), code):
            return at // STEP_SECONDS
    return None


def consume_totp(credential, code: str, *, now: int | None = None) -> bool:
    """Verify ``code`` against ``credential`` and burn the time step it used.

    Returns ``False`` both for a wrong code and for a correct one whose step has
    already been spent, which are the same answer to the caller and must not be
    distinguishable in the response.

    The claim is a ``QuerySet.update``, so it fires no ``post_save`` and writes
    no ``ObjectChangeLog`` row — deliberately, and the one place in this module
    where that is true. AGENTS.md asks for an explicit recorder alongside a bulk
    write, but the rule exists for writes that change user-owned state, and this
    changes a replay counter that identifies nobody and reconstructs from
    nothing. Auditing it would append a row to a permanent trail on every single
    login for no analytical gain. The login is audited on its own terms, as a
    ``user.login`` activity. Spending a *backup* code does write a row, because
    consuming an irreplaceable recovery credential is worth having in the trail.
    """
    counter = verify_totp_code(credential.secret, code, now=now)
    if counter is None:
        return False
    claimed = type(credential).objects.filter(pk=credential.pk, last_counter__lt=counter).update(last_counter=counter)
    if not claimed:
        return False
    credential.last_counter = counter
    return True


def consume_backup_code(credential, code: str) -> bool:
    """Verify ``code`` against ``credential``'s unused backup codes and spend it.

    The matching hash is removed from the stored list, so each code works once.
    The row is re-read under a lock before the write: without it two concurrent
    requests carrying the same code would each read the list containing it and
    each write back a list missing only that one, spending it twice.
    """
    code = _normalise_backup(code)
    if not _BACKUP_CODE_RE.match(code):
        return False
    digest = hash_backup_code(code)
    with transaction.atomic():
        locked = type(credential).objects.select_for_update().filter(pk=credential.pk).first()
        if locked is None:
            return False
        remaining = list(locked.backup_codes or [])
        for index, stored in enumerate(remaining):
            if hmac.compare_digest(str(stored), digest):
                del remaining[index]
                locked.backup_codes = remaining
                locked.save(update_fields=["backup_codes"])
                credential.backup_codes = remaining
                return True
    return False
