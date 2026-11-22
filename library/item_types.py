"""Registry of member types a plugin adds to collections, datasets and tags.

The library knows recordings and media files by name. Any other model can be a member by its primary key, and is
listed with no type and no hash unless registered, which leaves a client nothing to open and a sequential key to read.
An owning app registers its model here from ``AppConfig.ready()`` with ``register_item_type``, and the library then
resolves the type's public hash on an item write, lists the member by its type, hash and grantee-visible name, drops
the rows that are not live, and refuses the type where the release gate cannot cover it.

The release gate in ``library.release`` hides unreleased members through read-visibility gates the recordings and
media apps register; a registered type carries no such gate, so it is refused in a release-gated dataset unless it
declares one with ``release_gateable``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet


@dataclass(frozen=True)
class ItemType:
    """One registered member type.

    ``object_type`` is what listings name it by. ``hash_field`` holds its public identifier, which ``normalise_hash``
    brings to the stored spelling before lookup. ``display_name`` gives the label every reader of the member is shown
    and must never return an author-private field. ``live`` narrows the type's rows to those a listing may show.
    ``serves_share_tokens`` is False for a type whose serving endpoints authenticate by session only, which a listing
    then leaves out for a caller reading through a share token. ``withheld_under_middleware`` narrows the type's rows
    to those a caller reading under ``apply_middleware`` gets nothing of: the queryset form of the type's terms refusal
    (``epicurrents.permissions.register_terms_refusal``), which a dataset listing applies before it pages and which
    must agree with the refusal row for row; it needs the type's model to carry ``author_id``, since a caller's own
    members are exempt as they are from the refusal. ``platform_pass`` names the pass the platform applied to a
    member's stored content, or ``epicurrents.attachments.PASS_NONE``, as the type's attachment registration does; a
    row carries it as a record of what was done, and a type that declares none is listed with null.
    """

    object_type: str
    model_label: str
    hash_field: str
    display_name: Callable[[Any], str]
    live: Callable[[QuerySet], QuerySet] = lambda qs: qs
    normalise_hash: Callable[[str], str] = lambda value: value.strip()
    release_gateable: bool = False
    serves_share_tokens: bool = True
    withheld_under_middleware: Callable[[QuerySet], QuerySet] | None = None
    platform_pass: Callable[[Any], str] | None = None

    @property
    def model(self):
        return apps.get_model(self.model_label)

    @property
    def content_type(self) -> ContentType:
        return ContentType.objects.get_for_model(self.model, for_concrete_model=False)


_TYPES: dict[str, ItemType] = {}


def _declared_terms(item_type: ItemType) -> tuple:
    """The fields of *item_type* that compare by value.

    Its callables are left out: a lambda is a new object each time the module defining it runs, so two registrations
    of one type never compare equal on them.
    """
    return (
        item_type.object_type,
        item_type.hash_field,
        item_type.release_gateable,
        item_type.serves_share_tokens,
        item_type.withheld_under_middleware is None,
    )


def register_item_type(item_type: ItemType) -> None:
    """Register a member type. A model registered twice under different terms is refused.

    Registering a model again under the same terms has no effect and the first registration stands, so a second
    ``ready()`` in one process is harmless.
    """
    key = item_type.model_label.lower()
    current = _TYPES.get(key)
    if current is not None:
        if _declared_terms(current) != _declared_terms(item_type):
            raise ValueError(f"Library item type {item_type.model_label!r} is already registered.")
        return
    _TYPES[key] = item_type


def registered_item_types() -> list[ItemType]:
    """The registered member types whose model is installed, in model-label order."""
    return [_TYPES[label] for label in sorted(_TYPES) if _installed(label)]


def _installed(model_label: str) -> bool:
    """True when *model_label* names an installed model. ``apps.is_installed`` takes a module path, not a label."""
    try:
        apps.get_model(model_label)
    except LookupError:
        return False
    return True


def item_type_for(content_type: ContentType) -> ItemType | None:
    """The registered member type of *content_type*, or None for a type the library knows itself or nobody owns."""
    return _TYPES.get(f"{content_type.app_label}.{content_type.model}")


def resolve_hash(item_type: ItemType, value: str) -> str | None:
    """The primary key string of the live member *value* names by its hash, or None when there is none."""
    normalised = item_type.normalise_hash(value)
    if not normalised:
        return None
    obj = item_type.live(item_type.model.objects.filter(**{item_type.hash_field: normalised})).only("pk").first()
    return None if obj is None else str(obj.pk)


def without_unservable_members(qs: QuerySet, dataset: Any, user: Any, share_token: str | None) -> QuerySet:
    """Narrow a dataset's ``DatasetItem`` queryset to the registered-type members this caller can be served.

    A type that serves no share token is decided by the session alone, since its endpoints take no token: a caller
    without an account loses it, and a signed-in caller presenting a token keeps it only where their own access
    reaches the dataset, on the terms of that access.

    A member its type withholds under ``apply_middleware`` is listed on the terms the caller reads that member by, as
    ``get_read_access_result`` ranks them: the caller's direct row on the member where one exists, the dataset's
    terms otherwise. A de-identifying reader of the dataset therefore keeps a member a raw grant of their own reaches
    and loses the rest, and a raw reader of the dataset loses a member their own direct row de-identifies. The
    caller's own members are exempt, as they are from the terms refusal.

    Superusers lose nothing, and neither does the dataset's author, whose own row on the dataset carries
    ``apply_middleware`` at its default: they are listed every member, one they would be refused on opening included,
    because a member that is not listed cannot be removed.
    """
    from epicurrents.permissions import ReadAccessTerms, get_direct_read_terms, get_read_access_result

    types = registered_item_types()
    if not types:
        return qs
    signed_in = user is not None and getattr(user, "is_authenticated", False)
    superuser = signed_in and getattr(user, "is_superuser", False)
    token_value = (share_token or "").strip()
    if superuser:
        terms = ReadAccessTerms(granted=True)
    else:
        terms = get_read_access_result(user, dataset, share_token=share_token)
    # What the session reaches with the token set aside, for the types that serve none.
    own_terms = terms
    if signed_in and not superuser and token_value:
        own_terms = get_read_access_result(user, dataset, share_token=None)
    for item_type in types:
        ct = item_type.content_type
        type_terms = terms
        if not item_type.serves_share_tokens:
            if not signed_in or not (own_terms.granted or dataset.author_id == user.pk):
                qs = qs.exclude(content_type=ct)
                continue
            type_terms = own_terms
        if item_type.withheld_under_middleware is None or superuser:
            continue
        if signed_in and dataset.author_id == user.pk:
            continue
        member_pks = [
            int(key) for key in qs.filter(content_type=ct).values_list("object_id", flat=True) if key.isdigit()
        ]
        withheld = item_type.withheld_under_middleware(item_type.model.objects.filter(pk__in=member_pks))
        if signed_in:
            withheld = withheld.exclude(author_id=user.pk)
        withheld_keys = [str(pk) for pk in withheld.values_list("pk", flat=True)]
        if not withheld_keys:
            continue
        # One query for the caller's direct rows on the withheld members, ranked as the per-object check ranks them.
        token = share_token if item_type.serves_share_tokens else None
        direct = get_direct_read_terms(user, ct, withheld_keys, share_token=token)
        refused = [key for key in withheld_keys if direct.get(key, type_terms).apply_middleware]
        if refused:
            qs = qs.exclude(content_type=ct, object_id__in=refused)
    return qs
