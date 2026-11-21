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
    members are exempt as they are from the refusal.
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

    @property
    def model(self):
        return apps.get_model(self.model_label)

    @property
    def content_type(self) -> ContentType:
        return ContentType.objects.get_for_model(self.model, for_concrete_model=False)


_TYPES: dict[str, ItemType] = {}


def register_item_type(item_type: ItemType) -> None:
    """Register a member type. A model registered twice under different terms is refused."""
    current = _TYPES.get(item_type.model_label.lower())
    if current is not None and current != item_type:
        raise ValueError(f"Library item type {item_type.model_label!r} is already registered.")
    _TYPES[item_type.model_label.lower()] = item_type


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

    A caller without an account loses the types that serve no share token. A caller who reads the dataset under
    ``apply_middleware`` loses the members their type withholds under it, except the caller's own, which the terms
    refusal exempts as well. Superusers lose neither. The dataset's author is no exception: their own row on the
    dataset carries ``apply_middleware`` at its default, and a member someone else wrote is served to them under it.
    """
    from django.db.models import CharField
    from django.db.models.functions import Cast

    from epicurrents.permissions import get_read_access_result

    types = registered_item_types()
    if not types:
        return qs
    superuser = user is not None and getattr(user, "is_superuser", False)
    de_identifying = not superuser and get_read_access_result(user, dataset, share_token=share_token).apply_middleware
    for item_type in types:
        ct = item_type.content_type
        if user is None and not item_type.serves_share_tokens:
            qs = qs.exclude(content_type=ct)
            continue
        if de_identifying and item_type.withheld_under_middleware is not None:
            withheld = item_type.withheld_under_middleware(item_type.model.objects.all())
            if user is not None:
                withheld = withheld.exclude(author_id=user.pk)
            keys = withheld.annotate(key=Cast("pk", CharField())).values("key")
            qs = qs.exclude(content_type=ct, object_id__in=keys)
    return qs
