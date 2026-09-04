"""User API — session login/logout, profile management, and password reset.

Endpoints
---------
POST   /login                  Authenticate and open a session, or ask for a second factor.
POST   /login/2fa              Verify the second factor and finish opening the session.
POST   /logout                 Destroy the current session.
GET    /me                     Return the authenticated user's profile.
PATCH  /me                     Update email, first name, or last name.
POST   /me/change-password     Change the current password; keeps the session alive.
POST   /reset-password         Send a password reset link (rate-limited per email).
POST   /reset-password/confirm Validate reset token and set a new password.
GET    /preferences            Return the caller's stored client settings for a scope.
PUT    /preferences            Replace the caller's stored client settings for a scope.

Account and group administration is mounted under ``/admin`` from
user/api/v1/accounts.py; second-factor enrolment under ``/me/2fa`` from
user/api/v1/two_factor.py.
"""

import hashlib
import json
import re
import time
import urllib.parse

from django.conf import settings
from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout,
    update_session_auth_hash,
)
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import (
    url_has_allowed_host_and_scheme,
    urlsafe_base64_decode,
)
from ninja import Field, NinjaAPI, Schema
from ninja.errors import HttpError

from activity.audit import log_activity
from epicurrents.auth import enforce_session_csrf
from epicurrents.security_log import get_client_ip, log_security_event
from user.api.v1.accounts import router as accounts_router
from user.api.v1.two_factor import router as two_factor_router
from user.models import TwoFactorCredential, UserPreference
from user.oidc import (
    OIDCAuthError,
    OIDCConfigError,
    OIDCError,
    available_providers,
    build_authorization_url,
    exchange_code,
    generate_nonce,
    generate_pkce,
    generate_state,
    get_provider,
    oidc_enabled,
    resolve_identity,
    validate_id_token,
)
from user.roles import read_roles
from user.two_factor import (
    active_credential,
    build_provisioning_uri,
    consume_backup_code,
    consume_totp,
    generate_backup_codes,
    generate_secret,
    two_factor_required,
)

api = NinjaAPI(
    title="User API",
    version="1",
    urls_namespace="user-api-v1",
    docs_url=settings.API_DOCS_URL,
    openapi_url=settings.API_OPENAPI_URL,
)

# Account and group administration. Imported here rather than in urls.py so the
# whole user API keeps one mount point; see user/api/v1/accounts.py.
api.add_router("/admin", accounts_router)
# Second-factor enrolment and management for the signed-in user. The other half
# of the flow — the code prompt during login — stays in this module, since it
# shares the login lockout policy and has no authenticated user to hang off.
api.add_router("/me/2fa", two_factor_router)

# Max failed login attempts before a 5-minute lockout per username. The second
# factor reuses both, keyed on the pending account instead of the username: it
# is the same policy applied to the same login, and giving the code prompt its
# own budget would only widen the total number of guesses a login allows.
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_LOCKOUT_WINDOW = 5 * 60

# Session key holding the account that passed the password step and now owes a
# code, and how long it may sit there. Five minutes is long enough to fetch a
# phone and short enough that an abandoned half-login is not left standing.
_PENDING_2FA_SESSION_KEY = "pending_two_factor"
_PENDING_2FA_TTL = 5 * 60

# Generous next to a 6-digit TOTP or a 14-character grouped backup code, and
# finite, which is the point on an endpoint reachable before authentication.
_MAX_CODE_LENGTH = 64

# Seconds a user must wait between password reset requests.
# If you change this, update the matching copy in LoginView.vue (setTimeout duration and error message).
_RESET_RATE_WINDOW = 5 * 60

# Bounds on a stored preference blob. These are deliberately generous for a settings map and
# deliberately finite: the blob is caller-supplied, it is rewritten on every settings change, and
# every version of it is kept in the permanent audit trail, so an unbounded field would let one
# account inflate the change log without limit.
_PREFERENCE_MAX_KEYS = 500
_PREFERENCE_MAX_KEY_LENGTH = 128
_PREFERENCE_MAX_VALUE_LENGTH = 1024
_PREFERENCE_MAX_ITEMS_PER_VALUE = 64
# The per-field bounds above multiply out to tens of megabytes, which is not a bound at all once
# you remember that every accepted blob is written to ``ObjectChangeLog`` twice (before and after
# state) and kept forever. This is the one that actually holds: 16 KiB is roomy for a settings map
# — the viewer's full user-definable set is well under 2 KiB — and caps what a single account can
# append to the permanent trail per write.
_PREFERENCE_MAX_TOTAL_BYTES = 16 * 1024
# A scope names the client that owns the blob, not anything user-supplied.
_PREFERENCE_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Setting names are `<module>.<field>` paths (`eeg.defaultMontage`, `app.userName`). Restricting
# keys to that shape is what keeps the blob a settings map rather than free-form storage.
_PREFERENCE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class LoginIn(Schema):
    """Credentials payload for session login."""

    username: str
    password: str


class TwoFactorCodeIn(Schema):
    """A second-factor code: either a six-digit TOTP or a backup code.

    Bounded because this endpoint answers before authentication: the real
    values are six and twelve characters, and without a cap a caller could make
    every rejected attempt carry a megabyte of string through two regex passes.
    """

    code: str = Field(max_length=_MAX_CODE_LENGTH)


class UserOut(Schema):
    """User info returned after login, me check, or profile update.

    ``roles`` carries any project-supplied roles the user inherits through
    group membership, keyed by the role key the active project registered.
    The user app does not import any project —
    the values arrive through the group-role registry in user/roles.py, which
    a project populates from its own ``AppConfig.ready()``, so this endpoint
    works regardless of which project is active; a deployment that registers
    no roles serves an empty map.
    """

    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_staff: bool
    is_superuser: bool
    is_2fa_enabled: bool = False
    roles: dict[str, list[str]] = {}


class LoginResultOut(Schema):
    """Outcome of a login attempt, which may be "not finished yet".

    A correct password is not always a session, so the state is carried in the
    body rather than implied by the response shape. Mirrors ``AuthStateOut`` on ``GET /me``: the state is in the body and
    ``user`` is populated only once a session actually exists.
    """

    authenticated: bool
    two_factor_required: bool = False
    # Set when the password was correct, the deployment requires a second factor
    # and the account has none. The caller must enrol before a session exists,
    # via POST /login/2fa/setup. Distinct from two_factor_required rather than
    # folded into it because the two lead to different screens, and only one of
    # them can be answered by an authenticator the user already holds.
    two_factor_enrolment_required: bool = False
    # Returned exactly once, when a login completes a first-time enrolment.
    # Only their hashes are stored, so a caller that discards them cannot be
    # given them again — the account can only regenerate a fresh set.
    backup_codes: list[str] | None = None
    user: UserOut | None = None


class AuthStateOut(Schema):
    """Auth-state probe result for ``GET /me``.

    Always returned with HTTP 200 so a logged-out probe is a normal answer, not
    a console-polluting error: ``authenticated`` carries the state and ``user``
    is the serialized account when signed in, ``None`` otherwise.
    """

    authenticated: bool
    user: UserOut | None = None


class UserSearchOut(Schema):
    """Minimal user representation for search results (used by sharing flows)."""

    id: int
    username: str
    first_name: str
    last_name: str


class ProfileIn(Schema):
    """Payload for updating profile fields. All fields are optional."""

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class ChangePasswordIn(Schema):
    """Payload for changing the current user's password."""

    current_password: str
    new_password: str


class PasswordResetRequestIn(Schema):
    """Email address to send a password reset link to."""

    email: str


class PasswordResetConfirmIn(Schema):
    """Token + new password payload for completing a password reset."""

    uid: str
    token: str
    new_password: str


def _require_auth(request):
    """Return authenticated user or raise 401."""

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise HttpError(401, "Not authenticated")
    enforce_session_csrf(request)
    return user


def _serialize_user(user) -> dict:
    """Serialize a user to a dict matching ``UserOut``.

    ``roles`` is read through the group-role registry in user/roles.py, so
    core never imports a project — the registry is the same mechanism the
    account surface reads and writes group roles through, rather than a
    second one beside it.
    """
    return {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_2fa_enabled": active_credential(user) is not None,
        "roles": read_roles(user),
    }


def _pending_two_factor(request, *, require_credential: bool = True):
    """Return the user awaiting a second factor on this session, or ``None``.

    *require_credential* is ``True`` for the ordinary code step, where a marker
    without a live credential means the factor was reset mid-flow and the login
    must not complete. It is ``False`` only for first-time enrolment under an
    enforcement setting, where by definition there is no credential yet. The
    default keeps every existing caller strict.

    The marker is written by ``login_endpoint`` once the password checks out and
    read here by ``login_two_factor_endpoint``; it is the only thing carrying
    identity between the two halves of a login, so it is deliberately short-
    lived and deliberately re-validates the account on every read. An account
    deactivated, or its second factor reset, in the seconds between the two
    requests must not complete the login it had already half-passed.

    The returned user carries the ``backend`` attribute ``authenticate`` set on
    it in the first half. ``django.contrib.auth.login`` refuses a user without
    one whenever more than one backend is configured, and this deployment
    configures two whenever OIDC is available — so a user re-read from the
    database here has to have it restored, from the recorded backend rather
    than a guess, since it is the one that actually verified the password.
    """
    pending = request.session.get(_PENDING_2FA_SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    started = pending.get("started")
    if not isinstance(started, int) or int(time.time()) - started > _PENDING_2FA_TTL:
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        return None
    backend = pending.get("backend")
    if backend not in settings.AUTHENTICATION_BACKENDS:
        # The backend was removed from the configuration mid-flow, or the
        # marker predates a deployment that changed them.
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        return None
    User = get_user_model()
    user = User.objects.filter(pk=pending.get("user_id"), is_active=True).first()
    if user is None or (require_credential and active_credential(user) is None):
        request.session.pop(_PENDING_2FA_SESSION_KEY, None)
        return None
    user.backend = backend
    return user


@api.post("/login", response=LoginResultOut)
def login_endpoint(request, credentials: LoginIn):
    """Authenticate with username and password, and open a session or ask for a code.

    Failed attempts are counted per username. After ``_LOGIN_MAX_ATTEMPTS``
    consecutive failures the account is locked out for ``_LOGIN_LOCKOUT_WINDOW``
    seconds. The counter resets on a successful login.

    An account with a confirmed second factor does **not** get a session here.
    The password result is recorded as a pending marker on the session and the
    caller is sent to ``POST /login/2fa``; ``django.contrib.auth.login`` is
    reached only after the code verifies. Returning 200 with
    ``two_factor_required`` does tell a caller holding the correct password that
    the account has 2FA enabled, which is unavoidable — the code prompt itself
    says as much — and is not disclosed to anyone who fails the password.
    """

    username_key = hashlib.sha256(credentials.username.strip().lower().encode()).hexdigest()
    lockout_key = f"login_lockout:{username_key}"
    attempt_key = f"login_attempts:{username_key}"

    if cache.get(lockout_key):
        # Hashed username only — never write the raw input to the log stream.
        log_security_event(
            "auth.login_blocked",
            ip=get_client_ip(request),
            username_hash=username_key,
        )
        raise HttpError(429, "Too many failed login attempts. Please wait before trying again.")

    user = authenticate(request, username=credentials.username, password=credentials.password)
    if user is None:
        attempts = (cache.get(attempt_key) or 0) + 1
        if attempts >= _LOGIN_MAX_ATTEMPTS:
            cache.set(lockout_key, 1, timeout=_LOGIN_LOCKOUT_WINDOW)
            cache.delete(attempt_key)
            log_security_event(
                "auth.login_lockout",
                ip=get_client_ip(request),
                username_hash=username_key,
                attempts=attempts,
            )
        else:
            cache.set(attempt_key, attempts, timeout=_LOGIN_LOCKOUT_WINDOW)
        log_security_event(
            "auth.login_failed",
            ip=get_client_ip(request),
            username_hash=username_key,
            attempts=attempts,
        )
        raise HttpError(401, "Invalid credentials")

    # Password accepted — clear any previous failure counters.
    cache.delete(lockout_key)
    cache.delete(attempt_key)

    enrolled = active_credential(user) is not None
    must_enrol = not enrolled and two_factor_required(user)
    if enrolled or must_enrol:
        request.session[_PENDING_2FA_SESSION_KEY] = {
            "user_id": user.pk,
            "started": int(time.time()),
            # Recorded rather than re-derived: the second half logs the user in
            # through the same backend that verified the password here.
            "backend": user.backend,
        }
        log_activity(verb="user.login.challenge", target=user)
        if must_enrol:
            # The password was correct; the account simply has no factor yet and
            # the deployment requires one. The caller is sent to the enrolment
            # step rather than being refused, because refusing would leave the
            # account with no route in: every other enrolment endpoint needs the
            # session this response is withholding.
            log_security_event(
                "auth.2fa_enrolment_required",
                ip=get_client_ip(request),
                actor_id=user.pk,
            )
        return {
            "authenticated": False,
            "two_factor_required": True,
            "two_factor_enrolment_required": must_enrol,
            "user": None,
        }

    login(request, user)
    log_activity(verb="user.login", target=user)
    return {"authenticated": True, "two_factor_required": False, "user": _serialize_user(user)}


class LoginEnrolOut(Schema):
    """The secret an authenticator scans, handed out before a session exists."""

    secret: str
    provisioning_uri: str


@api.post("/login/2fa/setup", response=LoginEnrolOut)
def login_two_factor_setup_endpoint(request):
    """Mint a secret for an account that must enrol before it may sign in.

    This exists to break a circularity. Every other enrolment endpoint sits
    behind ``_require_auth``, so enrolling needs a session; enforcement means
    withholding the session from an account that has no factor yet. With both in
    place and neither of these, such an account is told to enrol before it may
    sign in while the only way to enrol is to be signed in — locked out by the
    control meant to protect it, and under ``TWO_FACTOR_REQUIRED_FOR_ALL`` that
    is every account on the deployment at once.

    The credential it accepts is the pending-login marker: a password-verified
    identity with a five-minute life, which is exactly the right strength for
    this and nothing more. No session is opened here; that happens in
    ``POST /login/2fa`` once a code proves the authenticator holds the secret.

    Deliberately refused when enforcement does not require it. Otherwise this
    would be a way to enrol without the password re-confirmation that
    ``POST /me/2fa`` demands, which is a weaker path to the same credential.

    **This one enforces session CSRF, unlike its two neighbours**, and the
    difference is where the authority comes from rather than whether a session
    user exists. ``POST /login`` and ``POST /login/2fa`` both carry an
    unguessable value in the body — a password, a TOTP code — so a forged
    cross-site request cannot produce a meaningful one. This endpoint takes no
    body at all: the account it acts on is decided entirely by session-cookie
    state, which is ambient authority and exactly what the chokepoint exists to
    protect. Reasoning it into the same class as ``/login`` was a
    reclassification, and it was wrong.

    The harm it would otherwise allow is worse than losing an enrolment. This
    endpoint shares its lockout keys with the code step, so a forged call timed
    against a real enrolment replaces the secret underneath the user, turns the
    code they are about to type into a failure, and repeated, drives the account
    into a lockout that also blocks ordinary login — all without knowing the
    password.
    """
    enforce_session_csrf(request)
    user = _pending_two_factor(request, require_credential=False)
    if user is None:
        raise HttpError(401, "No login in progress. Please sign in again.")
    if active_credential(user) is not None:
        raise HttpError(409, "Two-factor authentication is already enabled.")
    if not two_factor_required(user):
        raise HttpError(403, "Enrolment is not required for this account.")

    # Honour the code step's lockout. Without this, an account locked out of the
    # code prompt could mint a fresh secret and start over, which would make the
    # lockout a delay rather than a limit. Failed attempts are not counted here:
    # there is nothing to fail, and counting a restarted enrolment as an attempt
    # would lock out the very user this endpoint exists to let in.
    if cache.get(f"login_2fa_lockout:{user.pk}"):
        log_security_event("auth.2fa_blocked", ip=get_client_ip(request), actor_id=user.pk, phase="enrolment")
        raise HttpError(429, "Too many failed codes. Please wait before trying again.")

    # Idempotent within a login: an unconfirmed credential is returned as it is
    # rather than replaced. Minting afresh on every call would mean any repeat —
    # a double-submitted form, a retried request, or a forged one that got past
    # the check above — invalidates a secret the user may already have scanned,
    # and they discover it as a code that will not verify. Nothing here needs a
    # new secret: an unconfirmed credential has never authenticated anything.
    credential = getattr(user, "two_factor", None)
    if credential is None:
        secret = generate_secret()
        TwoFactorCredential.objects.create(user=user, secret=secret, confirmed_at=None, last_counter=0, backup_codes=[])
        log_activity(verb="user.2fa.enroll", target=user)
    else:
        secret = credential.secret
    return {
        "secret": secret,
        "provisioning_uri": build_provisioning_uri(
            secret,
            account_name=user.get_username(),
            issuer=request.get_host(),
        ),
    }


@api.post("/login/2fa", response=LoginResultOut)
def login_two_factor_endpoint(request, payload: TwoFactorCodeIn):
    """Complete a login by verifying the second factor, and open the session.

    Accepts either a six-digit TOTP code or one of the account's backup codes,
    tried in that order; a backup code is spent on use. Attempts are counted
    against the same policy as the password step, keyed on the pending account
    rather than a submitted username, since the username is not in this request.

    **Enforces session CSRF**, unlike ``POST /login``. The difference is where
    identity comes from, not whether a session user exists: ``/login`` is keyed
    by a username in the body and needs no cookie at all, while this endpoint's
    account comes entirely from the session marker, which is ambient authority.

    The earlier justification — that a forged call cannot guess a code, so
    nothing is mutated — omitted the mutation that matters. A wrong code still
    increments ``login_2fa_attempts``, and enough forged requests reach the
    lockout that then blocks the victim's own login. ``SESSION_COOKIE_SAMESITE``
    of ``Lax`` does prevent the forgery in practice today, but that is a
    different defence from the one claimed here, it does not cover a
    same-registrable-domain origin, and the platform does not lean on client
    behaviour anywhere it can avoid doing so.
    """
    enforce_session_csrf(request)
    # require_credential=False so a first-time enrolment can be confirmed here:
    # the credential exists but is unconfirmed, which active_credential does not
    # return. An account with no credential row at all still fails below.
    user = _pending_two_factor(request, require_credential=False)
    if user is None:
        raise HttpError(401, "No login in progress. Please sign in again.")

    lockout_key = f"login_2fa_lockout:{user.pk}"
    attempt_key = f"login_2fa_attempts:{user.pk}"
    if cache.get(lockout_key):
        log_security_event(
            "auth.2fa_blocked",
            ip=get_client_ip(request),
            actor_id=user.pk,
        )
        raise HttpError(429, "Too many failed codes. Please wait before trying again.")

    credential = active_credential(user)
    # An unconfirmed credential is a first-time enrolment being completed. It is
    # accepted only while enforcement requires one, so this cannot become a
    # general route to activating a factor without re-confirming the password.
    enrolling = credential is None
    if enrolling:
        credential = getattr(user, "two_factor", None)
        if credential is None or not two_factor_required(user):
            raise HttpError(401, "No login in progress. Please sign in again.")

    used_backup = False
    if consume_totp(credential, payload.code):
        verified = True
    elif enrolling:
        # No backup codes exist yet, and there is nothing to fall back to.
        verified = False
    else:
        verified = consume_backup_code(credential, payload.code)
        used_backup = verified

    if not verified:
        attempts = (cache.get(attempt_key) or 0) + 1
        if attempts >= _LOGIN_MAX_ATTEMPTS:
            cache.set(lockout_key, 1, timeout=_LOGIN_LOCKOUT_WINDOW)
            cache.delete(attempt_key)
        else:
            cache.set(attempt_key, attempts, timeout=_LOGIN_LOCKOUT_WINDOW)
        log_security_event(
            "auth.2fa_failed",
            ip=get_client_ip(request),
            actor_id=user.pk,
            attempts=attempts,
        )
        raise HttpError(401, "Invalid code")

    cache.delete(lockout_key)
    cache.delete(attempt_key)

    backup_codes = None
    if enrolling:
        # Activate the enrolment in the same step that opens the session. The
        # codes are returned here and nowhere else; only their hashes are kept.
        presented, stored = generate_backup_codes()
        credential.confirmed_at = timezone.now()
        credential.backup_codes = stored
        credential.save(update_fields=["confirmed_at", "last_counter", "backup_codes"])
        backup_codes = presented
        log_activity(verb="user.2fa.confirm", target=user)
        log_security_event("auth.2fa_enrolled", ip=get_client_ip(request), actor_id=user.pk, phase="login")

    # login() cycles the session key but carries the data across, so the marker
    # has to go explicitly or it would ride into the authenticated session.
    request.session.pop(_PENDING_2FA_SESSION_KEY, None)
    login(request, user)
    if used_backup:
        remaining = len(credential.backup_codes or [])
        log_security_event(
            "auth.2fa_backup_used",
            ip=get_client_ip(request),
            actor_id=user.pk,
            remaining=remaining,
        )
    log_activity(
        verb="user.login",
        target=user,
        metadata={"method": "backup_code" if used_backup else "totp", "two_factor": True},
    )
    return {
        "authenticated": True,
        "two_factor_required": False,
        "backup_codes": backup_codes,
        "user": _serialize_user(user),
    }


@api.post("/logout")
def logout_endpoint(request):
    """Destroy the current session."""

    user = getattr(request, "user", None)
    target = user if (user and user.is_authenticated) else None
    logout(request)
    log_activity(verb="user.logout", target=target)
    return {"status": "ok"}


@api.get("/me", response=AuthStateOut)
def me_endpoint(request):
    """Report the current auth state, always with HTTP 200.

    The SPA probes this on every boot to decide between the app and the login
    screen. Answering "logged out" with a 401 turns a normal state check into a
    console error on each unauthenticated load, so the state is carried in the
    body instead. A safe method with no writes, it never routes through the
    session-CSRF chokepoint.
    """

    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        log_activity(verb="user.profile.read", target=user)
        return {"authenticated": True, "user": _serialize_user(user)}
    return {"authenticated": False, "user": None}


@api.patch("/me", response=UserOut)
def update_profile_endpoint(request, payload: ProfileIn):
    """Update the current user's email, first name, and/or last name."""

    user = _require_auth(request)
    fields_updated: list[str] = []
    if payload.email is not None:
        try:
            validate_email(payload.email)
        except ValidationError:
            raise HttpError(400, "Enter a valid email address.")
        user.email = payload.email
        fields_updated.append("email")
    if payload.first_name is not None:
        user.first_name = payload.first_name
        fields_updated.append("first_name")
    if payload.last_name is not None:
        user.last_name = payload.last_name
        fields_updated.append("last_name")
    user.save()
    log_activity(
        verb="user.profile.update",
        target=user,
        metadata={"fields_updated": fields_updated},
    )
    return _serialize_user(user)


@api.post("/me/change-password")
def change_password_endpoint(request, payload: ChangePasswordIn):
    """Change the current user's password. Keeps the session alive."""

    user = _require_auth(request)
    if not user.check_password(payload.current_password):
        raise HttpError(400, "Current password is incorrect")
    try:
        validate_password(payload.new_password, user=user)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages))
    user.set_password(payload.new_password)
    user.save()
    update_session_auth_hash(request, user)
    log_activity(verb="user.password.change", target=user)
    return {"status": "ok"}


@api.post("/reset-password")
def request_password_reset(request, payload: PasswordResetRequestIn):
    """Send a password reset link to the given email address if it exists.

    Rate-limited to one request per email address per 5 minutes. The limit is
    checked before the user lookup so the 429 response cannot be used to infer
    whether an address is registered.
    """

    address = payload.email.strip()

    # Reject anything that is not an address before it reaches the query. Without
    # this an empty string matches every account whose email is blank — the
    # default for admin-created and OIDC-provisioned accounts — so an
    # unauthenticated caller could mail a reset link to whoever holds the first
    # such account. The response is the same "ok" as every other outcome, so this
    # discloses nothing about which addresses are registered.
    try:
        validate_email(address)
    except ValidationError:
        return {"status": "ok"}

    # Rate limit by a hash of the normalised email address. Stored in Redis
    # (production) or LocMemCache (development). The hash avoids keeping raw
    # email addresses in the cache backend.
    email_key = hashlib.sha256(address.lower().encode()).hexdigest()
    cache_key = f"pwd_reset_rate:{email_key}"
    if cache.get(cache_key):
        log_security_event(
            "auth.password_reset_rate_limited",
            ip=get_client_ip(request),
            email_hash=email_key,
        )
        raise HttpError(429, "Please wait before requesting another password reset link.")
    cache.set(cache_key, 1, timeout=_RESET_RATE_WINDOW)

    User = get_user_model()
    # filter, not get: the user model does not constrain email to be unique, so
    # two active accounts can share one address. `get` raised
    # MultipleObjectsReturned there, which surfaced as a 500 — password reset
    # permanently broken for that address, and the 500-against-200 difference is
    # itself the enumeration signal this endpoint is built to avoid. Every
    # matching account belongs to whoever controls the address, so each gets its
    # own link rather than one of them becoming unreachable.
    users = list(User.objects.filter(email__iexact=address, is_active=True).order_by("pk"))
    if not users:
        # Return success regardless to avoid user enumeration. Deliberately no
        # email_hash: with no target, erase_subject cannot reach this row (it
        # walks Activity by target_content_type / target_object_id), so a hash
        # here would outlive an erasure request for the person who owns the
        # address. The verb alone records that an attempt happened.
        log_activity(verb="user.password.reset.request", metadata={"found": False})
        return {"status": "ok"}

    from user.tasks import send_password_reset_email

    # Only the primary key crosses the broker. The URL embeds a token valid for
    # three days and the recipient address is personal data, and the broker now
    # persists to an append-only file, so both would outlive their use on disk.
    # The task mints the token and reads the address at send time.
    for user in users:
        send_password_reset_email.delay(user.pk)

    # One Activity row exists per request — log_activity annotates the row the
    # middleware already created rather than appending — so calling it in the
    # loop above would overwrite the target N times and record only the last
    # account, silently losing the rest.
    if len(users) == 1:
        log_activity(
            verb="user.password.reset.request",
            target=users[0],
            metadata={"email_hash": email_key, "found": True},
        )
    else:
        # Several accounts share the address. The row cannot target all of them,
        # and erase_subject reaches rows only through target, so any identifier
        # here — the hash or the account ids — would be unerasable for every
        # account but one. Record the shape of what happened, not who it was.
        log_activity(
            verb="user.password.reset.request",
            metadata={"found": True, "account_count": len(users)},
        )
    return {"status": "ok"}


@api.get("/search", response=list[UserSearchOut])
def search_users(request, q: str):
    """Search active users by username, first name, or last name.

    Requires authentication. Returns up to 20 matches.  The ``q`` parameter
    must be at least 2 characters to avoid listing every user on an empty or
    single-character query.

    Only ``id``, ``username``, ``first_name``, and ``last_name`` are returned —
    no email or other sensitive fields.  Inactive users are excluded.
    """
    _require_auth(request)
    if len(q.strip()) < 2:
        raise HttpError(400, "Search query must be at least 2 characters.")
    User = get_user_model()
    results = list(
        User.objects.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q),
            is_active=True,
        ).order_by("username")[:20]
    )
    # The raw query string is somebody's name or email more often than not,
    # and Activity rows are permanent — store a hash so repeated searches
    # remain correlatable without retaining third-party personal data that
    # no erasure request could ever locate.
    query_hash = hashlib.sha256(q.strip().lower().encode()).hexdigest()[:16]
    log_activity(
        verb="user.search",
        metadata={
            "query_hash": query_hash,
            "query_length": len(q.strip()),
            "returned_count": len(results),
        },
    )
    return results


class GroupOut(Schema):
    """Group representation for access-control flows."""

    id: int
    name: str


@api.get("/groups", response=list[GroupOut])
def list_groups(request):
    """List all groups available for access-right grants.

    Requires authentication.  Returns groups ordered by name.
    Only the ``id`` and ``name`` fields are exposed.
    """
    from django.contrib.auth.models import Group

    _require_auth(request)
    groups = list(Group.objects.order_by("name"))
    log_activity(
        verb="user.group.list",
        metadata={"returned_count": len(groups)},
    )
    return groups


@api.post("/reset-password/confirm")
def confirm_password_reset(request, payload: PasswordResetConfirmIn):
    """Validate reset token and set a new password."""

    User = get_user_model()
    try:
        uid = force_str(urlsafe_base64_decode(payload.uid))
        user = User.objects.get(pk=uid)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        raise HttpError(400, "Invalid reset link")

    if not default_token_generator.check_token(user, payload.token):
        raise HttpError(400, "Reset link is invalid or has expired")

    try:
        validate_password(payload.new_password, user=user)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages))
    user.set_password(payload.new_password)
    user.save()
    log_activity(verb="user.password.reset.confirm", target=user)
    return {"status": "ok"}


# ── External login (OpenID Connect) ──────────────────────────────────────────
# Backend-driven Authorization Code + PKCE flow. The browser hits /start (a
# top-level redirect, not XHR), authenticates at the provider, and is returned
# to /callback, which exchanges the code, validates the ID token, resolves the
# local user, and opens a normal Django session. No token ever reaches the SPA.
# Disabled (404) unless OIDC_ENABLED is set and a provider is configured.


class OIDCProviderOut(Schema):
    """A configured external-login provider the SPA can offer."""

    name: str
    label: str
    login_url: str


class AuthConfigOut(Schema):
    """Public, unauthenticated description of available login methods."""

    oidc_providers: list[OIDCProviderOut]


def _oidc_error_redirect(reason: str) -> HttpResponseRedirect:
    """Send the browser back to the SPA login with a coarse, PII-free reason."""
    base = settings.FRONTEND_URL.rstrip("/")
    return HttpResponseRedirect(f"{base}/login?error=oidc&reason={urllib.parse.quote(reason)}")


def _safe_redirect(target: str, request) -> str:
    """Clamp an open-redirect target to a same-host relative path."""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return "/"


@api.get("/auth-config", response=AuthConfigOut)
def auth_config(request):
    """List the external login providers the SPA should render.

    Unauthenticated and side-effect-free; consulted on the login screen to
    decide whether to show a "Sign in with <provider>" button. Returns an
    empty list when OIDC is disabled or unconfigured.
    """
    providers: list[dict] = []
    if oidc_enabled():
        for name, cfg in available_providers().items():
            providers.append(
                {
                    "name": name,
                    "label": cfg.get("label", name),
                    "login_url": f"/api/v1/user/oidc/{name}/start",
                }
            )
    return {"oidc_providers": providers}


@api.get("/oidc/{provider}/start")
def oidc_start(request, provider: str, redirect: str = "/"):
    """Begin the OIDC flow: stash flow state in the session and redirect out.

    The ``state`` / ``nonce`` / PKCE verifier are kept server-side in the
    session and checked on return, so the callback needs no CSRF token — the
    ``state`` round-trip is the flow's anti-forgery control.
    """
    if not oidc_enabled():
        raise HttpError(404, "Not found")
    try:
        cfg = get_provider(provider)
    except OIDCConfigError:
        raise HttpError(404, "Not found")

    state = generate_state()
    nonce = generate_nonce()
    verifier, challenge = generate_pkce()
    request.session["oidc_flow"] = {
        "provider": provider,
        "state": state,
        "nonce": nonce,
        "code_verifier": verifier,
        "redirect": _safe_redirect(redirect, request),
    }
    try:
        url = build_authorization_url(cfg, cfg["redirect_uri"], state, nonce, challenge)
    except (OIDCError, OSError, ValueError):
        return _oidc_error_redirect("provider_unavailable")
    log_activity(verb="user.login.initiate", metadata={"provider": provider})
    return HttpResponseRedirect(url)


@api.get("/oidc/{provider}/callback")
def oidc_callback(request, provider: str, code: str = "", state: str = "", error: str = ""):
    """Complete the OIDC flow and open a session, or redirect back with an error.

    Validates the flow state, exchanges the code, validates the ID token
    (signature + issuer + audience + tenant + nonce), enforces the email-domain
    allowlist, resolves / provisions the local user, and logs them in.
    """
    if not oidc_enabled():
        raise HttpError(404, "Not found")

    flow = request.session.pop("oidc_flow", None)
    if error:
        log_security_event(
            "auth.oidc_denied",
            ip=get_client_ip(request),
            provider=provider,
            reason="provider_error",
        )
        return _oidc_error_redirect("provider_error")
    if not flow or flow.get("provider") != provider:
        return _oidc_error_redirect("invalid_flow")
    if not state or state != flow.get("state"):
        log_security_event(
            "auth.oidc_denied",
            ip=get_client_ip(request),
            provider=provider,
            reason="state_mismatch",
        )
        return _oidc_error_redirect("state_mismatch")

    try:
        cfg = get_provider(provider)
        id_token = exchange_code(cfg, cfg["redirect_uri"], code, flow["code_verifier"])
        claims = validate_id_token(cfg, id_token, flow["nonce"])
        identity, created = resolve_identity(provider, cfg, claims)
    except OIDCAuthError as exc:
        log_security_event(
            "auth.oidc_denied",
            ip=get_client_ip(request),
            provider=provider,
            reason=exc.reason,
        )
        return _oidc_error_redirect(exc.reason)
    except (OIDCError, OSError, ValueError):
        log_security_event(
            "auth.oidc_denied",
            ip=get_client_ip(request),
            provider=provider,
            reason="provider_unavailable",
        )
        return _oidc_error_redirect("provider_unavailable")

    user = authenticate(request, oidc_identity=identity)
    if user is None:
        log_security_event(
            "auth.oidc_denied",
            ip=get_client_ip(request),
            provider=provider,
            reason="inactive_user",
        )
        return _oidc_error_redirect("inactive_user")

    login(request, user)
    log_activity(
        verb="user.login",
        target=user,
        metadata={"provider": provider, "method": "oidc", "created": created},
    )
    return HttpResponseRedirect(_safe_redirect(flow.get("redirect", "/"), request))


# ---------------------------------------------------------------------------
# Client preferences
# ---------------------------------------------------------------------------


class PreferencesOut(Schema):
    """A user's stored settings for one client scope."""

    scope: str
    settings: dict[str, bool | float | int | str | list | dict | None]


class PreferencesIn(Schema):
    """Replacement settings map for one client scope."""

    settings: dict[str, bool | float | int | str | list | dict | None]


def _validate_scope(scope: str) -> str:
    """Return *scope* normalised, or raise 400 when it is not a valid scope name."""

    scope = (scope or "").strip().lower()
    if not _PREFERENCE_SCOPE_RE.match(scope):
        raise HttpError(400, "Invalid preference scope.")
    return scope


def _validate_preference_value(key: str, value) -> None:
    """Raise 400 unless *value* is a primitive, or a short flat list of primitives.

    Nested objects are rejected outright. The blob is a settings map, and every
    client setting the viewer exposes is a scalar, a colour tuple, or a short
    list; allowing arbitrary nesting would turn an audited user-owned field into
    general-purpose storage with no bound on its depth.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > _PREFERENCE_MAX_VALUE_LENGTH:
            raise HttpError(400, f"Preference '{key}' is too long.")
        return
    if isinstance(value, list):
        if len(value) > _PREFERENCE_MAX_ITEMS_PER_VALUE:
            raise HttpError(400, f"Preference '{key}' has too many items.")
        for item in value:
            if item is not None and not isinstance(item, (bool, int, float, str)):
                raise HttpError(400, f"Preference '{key}' may only contain primitive values.")
            if isinstance(item, str) and len(item) > _PREFERENCE_MAX_VALUE_LENGTH:
                raise HttpError(400, f"Preference '{key}' contains a value that is too long.")
        return
    raise HttpError(400, f"Preference '{key}' must be a primitive value or a list of them.")


def _validate_preferences(values: dict) -> dict:
    """Return *values* unchanged after checking it is a well-formed settings map.

    Keys must look like `<module>.<field>` setting paths and values must be
    primitives or short flat lists of them. The check is what keeps this an
    opaque *settings* store rather than opaque storage: the blob is written to
    the permanent audit trail on every change, so free-form content here would
    be free-form content there.
    """

    if len(values) > _PREFERENCE_MAX_KEYS:
        raise HttpError(400, "Too many preferences.")
    for key, value in values.items():
        if len(key) > _PREFERENCE_MAX_KEY_LENGTH or not _PREFERENCE_KEY_RE.match(key):
            raise HttpError(400, f"Invalid preference name '{key[:_PREFERENCE_MAX_KEY_LENGTH]}'.")
        _validate_preference_value(key, value)
    if len(json.dumps(values, separators=(",", ":"))) > _PREFERENCE_MAX_TOTAL_BYTES:
        raise HttpError(400, "Preferences are too large.")
    return values


@api.get("/preferences", response=PreferencesOut)
def get_preferences(request, scope: str = "viewer"):
    """Return the caller's stored settings for *scope*.

    An unknown scope is not an error — it yields an empty map, which is what a
    client that has never saved anything should see.
    """

    user = _require_auth(request)
    scope = _validate_scope(scope)
    row = UserPreference.objects.filter(user=user, scope=scope).first()
    log_activity(
        verb="user.preferences.read",
        target=row or user,
        metadata={"scope": scope, "setting_count": len(row.values) if row else 0},
    )
    return PreferencesOut(scope=scope, settings=row.values if row else {})


@api.put("/preferences", response=PreferencesOut)
def put_preferences(request, payload: PreferencesIn, scope: str = "viewer"):
    """Replace the caller's stored settings for *scope*.

    The whole map is replaced rather than merged: the client owns the settings
    and sends a complete snapshot, so a merge would resurrect settings the user
    has since cleared.
    """

    user = _require_auth(request)
    scope = _validate_scope(scope)
    values = _validate_preferences(payload.settings)
    with transaction.atomic():
        row, created = UserPreference.objects.update_or_create(
            user=user,
            scope=scope,
            defaults={"values": values},
        )
        log_activity(
            verb="user.preferences.update",
            target=row,
            metadata={"scope": scope, "created": created, "setting_count": len(values)},
        )
    return PreferencesOut(scope=scope, settings=row.values)
