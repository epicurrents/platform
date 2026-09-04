"""OpenID Connect external-login flow (Authorization Code + PKCE).

⚠️ LOAD-BEARING — external-login authorization boundary.
When the feature is enabled, three checks in this module decide whether a
browser holding a provider-issued token becomes an authenticated platform
user, and which user:

1. :func:`_check_claims` — issuer / audience / **tenant (``tid``)** / nonce
   verification on the ID token. The tenant check is what locks logins to a
   single Entra directory; without it any Microsoft account in the world
   would pass.
2. :func:`identity_domain` + :func:`email_domain_allowed` — the app-side
   email-domain allowlist. This is PHI-containment control #1 (the only one
   implemented in code; controls #2/#3 are identity-provider-side and are
   documented in user/README.md). It fails **closed**: an absent / unparseable
   domain is rejected when an allowlist is configured.
3. :func:`resolve_identity` — find-or-create keyed on the pairwise ``sub``
   claim, applying the auto-create and verified-email linking policy.

Silently weakening any of these (dropping the ``tid`` check, defaulting the
domain gate open, linking on an unverified email) opens cross-tenant or
cross-domain account access. The signature / JWKS validation in
:func:`validate_id_token` is the cryptographic root of trust.

Contract test: [user/tests/test_oidc.py](tests/test_oidc.py). See AGENTS.md →
*Load-bearing files* before modifying.

HTTP to the provider (discovery document, JWKS, token endpoint) uses the
standard library so the module imports cleanly even when ``Authlib`` is not
installed; ``Authlib`` is imported lazily inside :func:`validate_id_token` and
is the only place ID-token signatures are verified.
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

# Clock skew tolerance (seconds) when validating token time claims.
_LEEWAY = 60
# Timeout (seconds) for outbound calls to the provider's well-known endpoints.
_HTTP_TIMEOUT = 10

# Discovery documents are immutable for the lifetime of a provider config;
# cache per authority so the login path does not refetch on every request.
_DISCOVERY_CACHE: dict[str, dict] = {}


class OIDCError(Exception):
    """Base class for OIDC errors."""


class OIDCConfigError(OIDCError):
    """The provider is unknown, not configured, or a dependency is missing."""


class OIDCAuthError(OIDCError):
    """An authentication attempt was rejected.

    ``reason`` is a stable token suitable for the ``auth.oidc_denied`` security
    log event and for a coarse ``?error=`` hint on the login redirect. It must
    never carry PII.
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


# ── Configuration ───────────────────────────────────────────────────────────


def oidc_enabled() -> bool:
    """True when the feature flag is on and at least one provider is configured."""
    return bool(getattr(settings, "OIDC_ENABLED", False)) and bool(available_providers())


def _is_configured(cfg: dict) -> bool:
    """A provider is usable only when the operator has supplied all credentials."""
    return bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("authority"))


def available_providers() -> dict[str, dict]:
    """Return the configured-and-usable providers keyed by name."""
    providers = getattr(settings, "OIDC_PROVIDERS", {}) or {}
    return {name: cfg for name, cfg in providers.items() if _is_configured(cfg)}


def get_provider(name: str) -> dict:
    """Return a usable provider config or raise :class:`OIDCConfigError`."""
    cfg = available_providers().get(name)
    if cfg is None:
        raise OIDCConfigError(f"OIDC provider {name!r} is not configured")
    return cfg


# ── Domain allowlist (PHI-containment control #1) ────────────────────────────


def identity_domain(claims: dict) -> str:
    """Best-effort email domain for the signed-in identity.

    Prefers the verified ``email`` claim, falling back to ``preferred_username``
    / ``upn`` (UPN-shaped). Returns ``""`` when no domain can be determined —
    callers treat that as "not allowed" when an allowlist is configured.
    Guest / B2B accounts whose UPN is mangled
    (``user_home.com#EXT#@tenant.onmicrosoft.com``) do not yield a usable home
    domain and are therefore rejected by a strict allowlist.
    """
    candidate = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    candidate = candidate.strip().lower()
    if "#ext#" in candidate or candidate.count("@") != 1:
        return ""
    return candidate.rsplit("@", 1)[1]


def email_domain_allowed(claims: dict, provider_cfg: dict) -> bool:
    """Apply the configured email-domain allowlist. Empty allowlist = allow all.

    Fails closed: when an allowlist is configured but no domain can be derived
    from the claims, access is denied.
    """
    allowed = [d.strip().lower() for d in provider_cfg.get("allowed_domains", []) if d.strip()]
    if not allowed:
        return True
    domain = identity_domain(claims)
    return bool(domain) and domain in allowed


# ── PKCE + authorization request ─────────────────────────────────────────────


def generate_state() -> str:
    """Return an opaque anti-CSRF state value for the authorization request."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """Return an opaque nonce bound into the ID token to defeat replay."""
    return secrets.token_urlsafe(32)


def generate_pkce() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` pair (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(provider_cfg: dict, redirect_uri: str, state: str, nonce: str, code_challenge: str) -> str:
    """Build the provider authorization URL the browser is redirected to."""
    discovery = _discovery(provider_cfg)
    params = {
        "client_id": provider_cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(provider_cfg.get("scopes", ["openid", "profile", "email"])),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{discovery['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


# ── Token exchange + validation ──────────────────────────────────────────────


def exchange_code(provider_cfg: dict, redirect_uri: str, code: str, code_verifier: str) -> str:
    """Exchange an authorization code for tokens; return the raw ID token.

    The client secret is sent server-to-server to the provider token endpoint
    over TLS — it never reaches the browser.
    """
    discovery = _discovery(provider_cfg)
    data = {
        "client_id": provider_cfg["client_id"],
        "client_secret": provider_cfg["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        payload = _http_post_form(discovery["token_endpoint"], data)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OIDCAuthError("token_exchange_failed", str(exc)) from exc
    id_token = payload.get("id_token")
    if not id_token:
        raise OIDCAuthError("token_exchange_failed", "no id_token in token response")
    return id_token


def validate_id_token(provider_cfg: dict, id_token: str, nonce: str) -> dict:
    """Verify the ID token signature and claims; return the claims as a dict.

    Signature verification uses the provider JWKS via ``Authlib``; the
    issuer / audience / tenant / nonce checks are delegated to
    :func:`_check_claims`.
    """
    jwt, JsonWebKey = _load_jose()
    discovery = _discovery(provider_cfg)
    jwks = _http_get_json(discovery["jwks_uri"])
    key_set = JsonWebKey.import_key_set(jwks)
    try:
        claims = jwt.decode(id_token, key_set)
        claims.validate(leeway=_LEEWAY)
    except Exception as exc:  # authlib raises a variety of JoseError subclasses
        raise OIDCAuthError("invalid_token", str(exc)) from exc
    claims = dict(claims)
    _check_claims(provider_cfg, claims, nonce)
    return claims


def _check_claims(provider_cfg: dict, claims: dict, expected_nonce: str) -> None:
    """Validate issuer, audience, tenant, and nonce. Raise on any mismatch.

    Pure (no network / ORM) so the security-critical gate is unit-testable.
    The tenant (``tid``) check is the single-directory lock for Microsoft
    Entra ID; it is skipped only when the provider declares no ``tenant_id``.
    """
    if claims.get("iss") != provider_cfg.get("authority"):
        raise OIDCAuthError("issuer_mismatch")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if provider_cfg["client_id"] not in audiences:
        raise OIDCAuthError("audience_mismatch")
    tenant_id = provider_cfg.get("tenant_id")
    if tenant_id and claims.get("tid") != tenant_id:
        raise OIDCAuthError("tenant_mismatch")
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise OIDCAuthError("nonce_mismatch")


# ── User resolution (find-or-create) ─────────────────────────────────────────


def resolve_identity(provider_name: str, provider_cfg: dict, claims: dict):
    """Map validated claims to an :class:`~user.models.ExternalIdentity`.

    Enforces the domain allowlist for every login (existing links included),
    then finds the identity by ``(provider, issuer, subject)`` or applies the
    create / link policy. Returns ``(identity, created)``.
    """
    from user.models import ExternalIdentity

    if not email_domain_allowed(claims, provider_cfg):
        raise OIDCAuthError("domain_not_allowed")

    issuer = claims["iss"]
    subject = claims["sub"]
    email = (claims.get("email") or "").strip()
    email_verified = bool(claims.get("email_verified", False))

    existing = ExternalIdentity.objects.filter(provider=provider_name, issuer=issuer, subject=subject).first()
    if existing is not None:
        existing.email = email or existing.email
        existing.email_verified = email_verified
        existing.last_login_at = timezone.now()
        existing.save(update_fields=["email", "email_verified", "last_login_at"])
        return existing, False

    with transaction.atomic():
        user = _link_or_create_user(claims, email, email_verified)
        identity = ExternalIdentity.objects.create(
            user=user,
            provider=provider_name,
            issuer=issuer,
            subject=subject,
            email=email,
            email_verified=email_verified,
            last_login_at=timezone.now(),
        )
    return identity, True


def _link_or_create_user(claims: dict, email: str, email_verified: bool):
    """Resolve the local ``User`` for a first-time identity per the link/create policy."""
    User = get_user_model()
    link_by_email = getattr(settings, "OIDC_LINK_BY_VERIFIED_EMAIL", False)
    if link_by_email and email and email_verified:
        match = User.objects.filter(email__iexact=email, is_active=True).first()
        if match is not None:
            return match
    if not getattr(settings, "OIDC_AUTO_CREATE_USERS", False):
        raise OIDCAuthError("auto_create_disabled")
    return _create_user(claims, email)


def _create_user(claims: dict, email: str):
    """Create a password-less local user from the token claims."""
    User = get_user_model()
    username = _unique_username(claims, email)
    user = User(
        username=username,
        email=email,
        first_name=(claims.get("given_name") or "")[:150],
        last_name=(claims.get("family_name") or "")[:150],
        is_active=True,
    )
    # Password login is disabled for provisioned accounts — they authenticate
    # only through the provider.
    user.set_unusable_password()
    user.save()
    return user


def _unique_username(claims: dict, email: str) -> str:
    """Derive a unique username from the email local part or the subject."""
    User = get_user_model()
    base = (email.split("@", 1)[0] if email else "") or f"oidc_{claims['sub'][:12]}"
    base = base[:140] or "oidc_user"
    candidate = base
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ── Standard-library HTTP + lazy Authlib ─────────────────────────────────────


def _discovery(provider_cfg: dict) -> dict:
    """Fetch (and cache) the provider's OpenID Connect discovery document."""
    authority = provider_cfg["authority"].rstrip("/")
    cached = _DISCOVERY_CACHE.get(authority)
    if cached is not None:
        return cached
    document = _http_get_json(f"{authority}/.well-known/openid-configuration")
    _DISCOVERY_CACHE[authority] = document
    return document


def _http_get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_jose():
    """Import ``authlib.jose`` lazily so the module loads without the dependency."""
    try:
        from authlib.jose import JsonWebKey, jwt
    except ImportError as exc:
        raise OIDCConfigError("Authlib is required for OIDC login but is not installed") from exc
    return jwt, JsonWebKey
