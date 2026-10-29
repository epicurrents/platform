"""The deployment's user agreement: which version binds, who has accepted it, and the gate that waits for it.

A deployment publishes its agreement with ``manage.py publish_agreement``; until it has published one, nothing here
does anything. Once it has, an account that has not accepted the binding version reaches no API endpoint but the
exempt routes, and the SPA sends it to the agreement page. The core routes are its auth state, the agreement itself,
signing in, out and back in as someone else, and removing its push subscription on the way out; a project adds its own
with :func:`register_exempt_route`.

The binding version is the latest one published with ``reaccept`` set. A correction published without it is the text
shown from then on, but an acceptance of the binding version, or of anything published after it, still counts. That is
how a typo is fixed without asking everyone again, while the record still says which words each person saw.

Only a session account is gated. A request without a session, whether a federated peer's, a share-token holder's or an
anonymous caller's, has no account to accept anything with, and its grants decide what it reaches. A project exempts an
account that is not one person's, such as a classroom's shared account, with :func:`register_exemption`: an acceptance
by an account many people use records nothing about any of them.

The binding version is cached for :data:`BINDING_CACHE_SECONDS`, and an account's acceptance of it in its session, so
the gate costs no query on most requests. Publishing rewrites the cache entry, which reaches every process through a
shared cache such as production's Redis; with a per-process cache, other processes pick it up when the entry expires.
A cache that cannot be reached is skipped for the database, so the gate stays closed rather than failing the request.
"""

import hashlib
import logging
import re
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse

# The API mount shapes the audit middleware recognises; a path outside them reaches no endpoint the gate holds back.
from epicurrents.middleware import _API_PATH_RE
from user.models import UserAgreement, UserAgreementAcceptance

#: The response ``code`` of a refusal, which the SPA keys on.
AGREEMENT_REQUIRED = "agreement_required"

BINDING_CACHE_KEY = "user.agreement.binding"
BINDING_CACHE_SECONDS = 60

#: The session key holding the pk of the binding version the account was last found to have accepted.
SESSION_KEY = "user_agreement_accepted"

#: The longest version label a ``UserAgreement`` row holds.
LABEL_MAX_LENGTH = 64

_NOT_PUBLISHED = 0

logger = logging.getLogger(__name__)

#: Routes an account that has not accepted may still call, as (anchored path pattern, methods or ``None`` for every
#: method). The operational paths in ``ACTIVITY_PATH_SKIP_LIST`` are exempt as well.
_EXEMPT_ROUTES: list[tuple[re.Pattern, frozenset[str] | None]] = []


def register_exempt_route(pattern: str, methods=None) -> None:
    """Let an account that has not accepted call the API paths matching ``pattern`` with ``methods``.

    ``pattern`` is a regular expression matched against the whole path; ``methods`` is an iterable of HTTP methods, or
    ``None`` for every method. A project calls this from ``AppConfig.ready`` for an endpoint an account must reach
    before it accepts, such as one that signs the browser in as another account. Keep it to such endpoints: every
    route exempted is one the agreement does not guard.
    """
    entry = (re.compile(rf"(?:{pattern})\Z"), frozenset(m.upper() for m in methods) if methods is not None else None)
    if entry not in _EXEMPT_ROUTES:
        _EXEMPT_ROUTES.append(entry)


register_exempt_route(r"/api/v1/user/me", methods=["GET"])
register_exempt_route(r"/api/v1/user/agreement", methods=["GET"])
register_exempt_route(r"/api/v1/user/agreement/accept", methods=["POST"])
register_exempt_route(r"/api/v1/user/logout", methods=["POST"])
# Signing in as someone else, or resetting a password, replaces the session rather than using it.
register_exempt_route(
    r"/api/v1/user/(?:login|login/2fa|login/2fa/setup|auth-config|reset-password|reset-password/confirm)"
)
register_exempt_route(r"/api/v1/user/oidc/[^/]+/(?:start|callback)", methods=["GET"])
# Signing out removes the browser's push subscription first; refusing that would leave it receiving the account's
# notifications after the sign-out.
register_exempt_route(r"/api/v1/notifications/subscribe", methods=["DELETE"])

_EXEMPTIONS: list[Callable] = []


def register_exemption(predicate: Callable) -> None:
    """Exempt the accounts ``predicate(user)`` answers true for from the agreement. Called from ``AppConfig.ready``."""
    if predicate not in _EXEMPTIONS:
        _EXEMPTIONS.append(predicate)


def is_exempt(user) -> bool:
    """Whether a registered exemption covers ``user``."""
    return any(predicate(user) for predicate in _EXEMPTIONS)


def text_digest(text: str) -> str:
    """The SHA-256 of an agreement's text, as published and as recorded on each acceptance."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_agreement() -> UserAgreement | None:
    """The latest published version, which is the one shown, or ``None`` when none has been published."""
    return UserAgreement.objects.order_by("-pk").first()


def binding_agreement_pk() -> int | None:
    """The pk of the version an account must have accepted, or of a later one, or ``None`` when none binds."""
    try:
        cached = cache.get(BINDING_CACHE_KEY)
    except Exception:
        logger.warning("The cache could not be read; the user agreement gate reads the database.", exc_info=True)
        cached = None
    if cached is None:
        cached = _binding_pk_from_database()
        try:
            # add, not set: a reader that read the database just before a publish must not put the old version back
            # over the one the publish wrote.
            cache.add(BINDING_CACHE_KEY, cached, BINDING_CACHE_SECONDS)
        except Exception:
            logger.warning("The cache could not be written; the user agreement gate reads the database.", exc_info=True)
    return cached or None


def _binding_pk_from_database() -> int:
    pk = UserAgreement.objects.filter(reaccept=True).order_by("-pk").values_list("pk", flat=True).first()
    return pk if pk is not None else _NOT_PUBLISHED


def _refresh_binding_cache() -> None:
    """Write the binding version into the cache once a publish has committed.

    Written rather than deleted, so a reader that missed the entry before the commit and writes what it read after
    it finds the entry taken; the two together are what makes a shared cache gate on the new version at once.
    """
    try:
        cache.set(BINDING_CACHE_KEY, _binding_pk_from_database(), BINDING_CACHE_SECONDS)
    except Exception:
        logger.warning("The cache could not be written; a new agreement binds once the entry expires.", exc_info=True)


def has_accepted(user, binding_pk: int | None = None) -> bool:
    """Whether ``user`` has accepted the binding version or one published after it. True when none binds."""
    if binding_pk is None:
        binding_pk = binding_agreement_pk()
    if binding_pk is None:
        return True
    return UserAgreementAcceptance.objects.filter(user=user, agreement_id__gte=binding_pk).exists()


def acceptance_required(user) -> bool:
    """Whether ``user`` must accept the agreement before reaching the API."""
    binding_pk = binding_agreement_pk()
    if binding_pk is None or is_exempt(user):
        return False
    return not has_accepted(user, binding_pk)


def accept(user, agreement: UserAgreement) -> UserAgreementAcceptance:
    """Record that ``user`` accepted ``agreement``; accepting a version twice returns the first record."""
    acceptance, _created = UserAgreementAcceptance.objects.get_or_create(
        user=user,
        agreement=agreement,
        defaults={"version": agreement.version, "digest": agreement.digest},
    )
    return acceptance


def check_publication(version: str, text: str, *, reaccept: bool = True) -> str:
    """Return the label to publish ``text`` under, or raise ``ValueError`` saying why it cannot be published.

    Refuses an empty or over-long label, a label already published, an empty text, and a correction while nothing is
    published.
    """
    version = version.strip()
    if not version:
        raise ValueError("The version label is empty.")
    if len(version) > LABEL_MAX_LENGTH:
        raise ValueError(f"The version label is longer than {LABEL_MAX_LENGTH} characters.")
    if not text.strip():
        raise ValueError("The agreement text is empty.")
    if UserAgreement.objects.filter(version=version).exists():
        raise ValueError(f"Version {version!r} is already published; publish the change under a new label.")
    if not reaccept and current_agreement() is None:
        raise ValueError("A correction corrects a published version, and none is published yet.")
    return version


def publish(version: str, text: str, *, reaccept: bool = True) -> UserAgreement:
    """Publish a new version of the agreement, after :func:`check_publication`.

    The binding version's cache entry is rewritten after the commit, so a shared cache gates on the new version at once.
    """
    version = check_publication(version, text, reaccept=reaccept)
    agreement = UserAgreement.objects.create(
        version=version,
        text=text,
        digest=text_digest(text),
        reaccept=reaccept,
    )
    transaction.on_commit(_refresh_binding_cache)
    return agreement


def refusal() -> JsonResponse:
    """The response to an API request from an account that has not accepted."""
    response = JsonResponse(
        {"detail": "Accept the user agreement before using your account.", "code": AGREEMENT_REQUIRED},
        status=403,
    )
    # The refusal returns before the inner middleware that would set it.
    response["Cache-Control"] = "no-store"
    return response


def _gated(path: str, method: str) -> bool:
    """Whether a request for ``path`` with ``method`` waits for the agreement."""
    if not _API_PATH_RE.match(path) or path in getattr(settings, "ACTIVITY_PATH_SKIP_LIST", ()):
        return False
    return not any(
        pattern.match(path) and (methods is None or method in methods) for pattern, methods in _EXEMPT_ROUTES
    )


class AgreementGateMiddleware:
    """Refuse API requests from a session account that has not accepted the binding agreement.

    Runs after ``AuthenticationMiddleware``, which it reads, and before ``ApiActivityLoggingMiddleware``, so a refusal
    writes no ``Activity`` row, as a maintenance refusal writes none. The refusal is not a security event either: the
    account is authenticated and authorised, and what it lacks is an acceptance it can give on the next page.

    The session remembers an acceptance, never an exemption, so an account that stops being exempt is gated on its next
    request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not _gated(request.path, request.method):
            return self.get_response(request)
        binding_pk = binding_agreement_pk()
        if binding_pk is None:
            return self.get_response(request)
        session = getattr(request, "session", None)
        if session is not None and session.get(SESSION_KEY) == binding_pk:
            return self.get_response(request)
        if is_exempt(user):
            return self.get_response(request)
        if not has_accepted(user, binding_pk):
            return refusal()
        if session is not None:
            session[SESSION_KEY] = binding_pk
        return self.get_response(request)
