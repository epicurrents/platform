"""Whether an account signs in through an external identity provider, and under which name.

Four surfaces need the same answer and would otherwise each invent one: the
password-reset request, the change-password endpoint, the operator's
set-password action, and the invite mail. All four offer a local password to an
account, and an account provisioned through
OIDC is not supposed to have one — a local password bypasses the tenant (``tid``)
and email-domain gates in :mod:`user.oidc`, which are the whole of the access
control on who may sign in at all.

``has_usable_password()`` is not the test. An invited account has an unusable
password until the person follows the link, so that check refuses exactly the
accounts the invite exists to serve. The identity row is the test, and it keeps
working when a second provider is added.

The second half of the test is what separates the two ways an account can hold
an identity. A local account that was linked to a provider by verified email
(``OIDC_LINK_BY_VERIFIED_EMAIL``) keeps its password, and that password login
already exists — refusing it a reset takes away a recovery path without closing
anything, since nothing new is bypassed by restoring a credential the account
already had. So an account counts as externally authenticated when it holds an
identity *and* has no usable password: the case where a local password would be
a new way in rather than the one it already uses.
"""

from django.conf import settings


def external_identity(user):
    """Return one of the account's external identities, or ``None``.

    Which one, when there is more than one, is unspecified and does not matter
    to any caller: the answer they need is whether the account is reachable
    through a provider at all, and the label is only used to name a provider in
    a message.

    Iterates the unmodified related manager rather than calling ``first()`` on an
    ordered queryset, because any modifier issues its own query and so ignores a
    ``prefetch_related`` the caller set up — which is a query per row on a
    listing that serializes a page of accounts.
    """
    if user is None or not getattr(user, "pk", None):
        return None
    for identity in user.external_identities.all():
        return identity
    return None


def is_externally_authenticated(user) -> bool:
    """Whether ``user`` signs in through a provider and has no local password of its own.

    The one question the reset, change-password, operator set-password and
    invite paths ask before offering to give the account a password.
    """
    if user is None or user.has_usable_password():
        return False
    return external_identity(user) is not None


def provider_label(user) -> str:
    """A human name for the provider ``user`` signs in through, for a message that must say which.

    Falls back to the provider key, and to a generic phrase when the account has
    no identity at all, so a caller can interpolate the result unconditionally.
    """
    identity = external_identity(user)
    if identity is None:
        return "an external identity provider"
    config = (getattr(settings, "OIDC_PROVIDERS", {}) or {}).get(identity.provider) or {}
    return config.get("label") or identity.provider
