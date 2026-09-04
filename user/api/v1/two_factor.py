"""Second-factor enrolment and management for the signed-in user.

Mounted at ``/api/v1/user/me/2fa/``. The other half of the feature — the code
prompt that stands between a correct password and a session — lives in
user/api/v1/ninja.py, because at that point there is no authenticated user for a
router like this one to key off.

Enrolment is two steps by necessity. ``POST /`` mints a secret and hands back
the ``otpauth://`` URI, but leaves ``confirmed_at`` unset; ``POST /confirm``
sets it, and only once the caller has produced a code proving the authenticator
holds the same secret. A one-step enrolment would let a mistyped or unscanned
secret lock the account out of its own login, which is the failure this flow
exists to prevent.

Three of the four writes here re-check the password. A session cookie is enough
to read data the account already owns; it should not be enough to *remove* the
control that protects the account, or to mint fresh recovery codes that would
serve as a lasting key. Confirming an enrolment is the exception — the code
itself is the proof, and the password was checked moments earlier when the
enrolment started.
"""

from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.security_log import get_client_ip, log_security_event
from user.models import TwoFactorCredential
from user.two_factor import (
    active_credential,
    build_provisioning_uri,
    generate_backup_codes,
    generate_secret,
    verify_totp_code,
)

router = Router()


class TwoFactorStatusOut(Schema):
    """Whether the caller's second factor is live, and how much recovery is left."""

    enabled: bool
    confirmed_at: str | None = None
    backup_codes_remaining: int = 0


class TwoFactorEnrolOut(Schema):
    """A freshly minted, not-yet-confirmed secret.

    ``secret`` accompanies ``provisioning_uri`` so the user can type it into an
    authenticator that cannot scan a QR code; both carry the same value.
    """

    secret: str
    provisioning_uri: str


class BackupCodesOut(Schema):
    """Recovery codes, in the only response that will ever contain them."""

    backup_codes: list[str]


class PasswordIn(Schema):
    """Password re-confirmation for a change to the second factor itself."""

    password: str


class ConfirmIn(Schema):
    """The code proving the authenticator holds the enrolled secret."""

    code: str


def _require_auth(request):
    """Return the authenticated user or raise 401.

    Routes the request through the session-CSRF chokepoint; see AGENTS.md →
    *Session-authenticated write CSRF*.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Not authenticated")
    enforce_session_csrf(request)
    return user


def _confirm_password(request, user, password: str) -> None:
    """Re-check the caller's password, or raise.

    An account with no usable password — provisioned through OIDC, where the
    provider owns authentication — cannot satisfy this and cannot manage a local
    second factor. Refusing with 409 rather than 400 says the state of the
    account is the problem, not the payload.
    """
    if not user.has_usable_password():
        raise HttpError(
            409,
            "This account signs in through an external provider, which owns its second factor.",
        )
    if not user.check_password(password):
        log_security_event(
            "auth.2fa_reauth_failed",
            ip=get_client_ip(request),
            actor_id=user.pk,
        )
        raise HttpError(400, "Password is incorrect")


def _status(user) -> dict:
    """Serialize the caller's second-factor state to ``TwoFactorStatusOut``."""
    credential = active_credential(user)
    if credential is None:
        return {"enabled": False, "confirmed_at": None, "backup_codes_remaining": 0}
    return {
        "enabled": True,
        "confirmed_at": credential.confirmed_at.isoformat(),
        "backup_codes_remaining": len(credential.backup_codes or []),
    }


@router.get("", response=TwoFactorStatusOut)
def get_status(request):
    """Report whether the caller has a confirmed second factor."""
    user = _require_auth(request)
    log_activity(verb="user.2fa.read", target=user)
    return _status(user)


@router.post("", response=TwoFactorEnrolOut)
def start_enrolment(request, payload: PasswordIn):
    """Mint an unconfirmed secret and return the URI an authenticator scans.

    Replaces any existing unconfirmed enrolment, and refuses while a confirmed
    one is live: re-enrolling on top of a working second factor would silently
    invalidate the authenticator entry the user still relies on. Disabling first
    makes that an explicit act.
    """
    user = _require_auth(request)
    _confirm_password(request, user, payload.password)
    if active_credential(user) is not None:
        raise HttpError(409, "Two-factor authentication is already enabled. Disable it first to re-enrol.")

    secret = generate_secret()
    TwoFactorCredential.objects.update_or_create(
        user=user,
        defaults={"secret": secret, "confirmed_at": None, "last_counter": 0, "backup_codes": []},
    )
    log_activity(verb="user.2fa.enroll", target=user)
    return {
        "secret": secret,
        "provisioning_uri": build_provisioning_uri(
            secret,
            account_name=user.get_username(),
            issuer=request.get_host(),
        ),
    }


@router.post("/confirm", response=BackupCodesOut)
def confirm_enrolment(request, payload: ConfirmIn):
    """Activate a pending enrolment and issue recovery codes.

    The codes are returned here and nowhere else; only their hashes are stored.
    """
    user = _require_auth(request)
    credential = getattr(user, "two_factor", None)
    if credential is None:
        raise HttpError(404, "No enrolment in progress.")
    if credential.confirmed_at is not None:
        raise HttpError(409, "Two-factor authentication is already enabled.")

    counter = verify_totp_code(credential.secret, payload.code)
    if counter is None:
        log_security_event(
            "auth.2fa_failed",
            ip=get_client_ip(request),
            actor_id=user.pk,
            phase="enrolment",
        )
        raise HttpError(400, "That code did not match. Check your authenticator and try again.")

    presented, stored = generate_backup_codes()
    credential.confirmed_at = timezone.now()
    # The confirming code is spent along with the enrolment, so it cannot be
    # replayed at the login prompt within its own validity window.
    credential.last_counter = counter
    credential.backup_codes = stored
    credential.save(update_fields=["confirmed_at", "last_counter", "backup_codes"])
    log_activity(verb="user.2fa.confirm", target=user)
    log_security_event("auth.2fa_enrolled", ip=get_client_ip(request), actor_id=user.pk)
    return {"backup_codes": presented}


@router.post("/backup-codes", response=BackupCodesOut)
def regenerate_backup_codes(request, payload: PasswordIn):
    """Discard the caller's unused recovery codes and issue a fresh set."""
    user = _require_auth(request)
    _confirm_password(request, user, payload.password)
    credential = active_credential(user)
    if credential is None:
        raise HttpError(409, "Two-factor authentication is not enabled.")

    presented, stored = generate_backup_codes()
    credential.backup_codes = stored
    credential.save(update_fields=["backup_codes"])
    log_activity(verb="user.2fa.backup.regenerate", target=user)
    return {"backup_codes": presented}


@router.post("/disable", response=dict)
def disable(request, payload: PasswordIn):
    """Remove the caller's second factor, live or half-enrolled."""
    user = _require_auth(request)
    _confirm_password(request, user, payload.password)
    credential = getattr(user, "two_factor", None)
    if credential is None:
        raise HttpError(409, "Two-factor authentication is not enabled.")

    was_active = credential.confirmed_at is not None
    credential.delete()
    log_activity(verb="user.2fa.disable", target=user)
    if was_active:
        log_security_event("auth.2fa_disabled", ip=get_client_ip(request), actor_id=user.pk)
    return {"status": "ok"}
