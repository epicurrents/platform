"""Registry of attachment types: the objects attached to a recording, listed the same way whatever they are.

An attachment is content its submitter answers for in full, a video, a document or a DICOM study, attached to the
recording it belongs with through a generic ``attachment_content_type`` / ``attachment_object_id`` pair on its own
model. A biosignal recording is de-identified by the platform; an attachment is not, and where the platform cleans an
attachment's container (the dicom plugin's header pass) that is defence in depth, recorded on the item as
``platform_pass`` and never an assurance a reader can rely on. A de-identifying grant changes nothing in an
attachment's bytes; a type that cannot honour one withholds the object through its own read rule instead.

Each owning app registers its type from ``AppConfig.ready()`` with ``register_attachment_type``. ``attachments_of``
then lists what a caller may read, deciding every row through ``can_read_object``, so each type's own attachment
extension (the media app's, the dicom plugin's withholding rule) is what decides and this module knows none of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model, QuerySet

#: What the platform did to an attachment's content: nothing.
PASS_NONE = "none"


@dataclass(frozen=True)
class AttachmentType:
    """One registered type of attachment.

    ``model_label`` names a model carrying ``attachment_content_type`` and ``attachment_object_id``. ``live`` narrows
    its rows to those that may be listed at all (not trashed, for example). ``describe`` returns the item's public
    ``hash``, its grantee-visible ``name`` and a ``details`` mapping of what a client needs to open it; it never
    returns an author-private field. ``platform_pass`` names the pass the platform applied to the stored content, or
    ``PASS_NONE``. ``serves_share_tokens`` is False for a type whose serving endpoints authenticate by session only,
    which is then left out for a caller reading through a share token rather than listed unopenable.
    """

    key: str
    model_label: str
    describe: Callable[[Any], dict[str, Any]]
    platform_pass: Callable[[Any], str]
    live: Callable[[QuerySet], QuerySet] = lambda qs: qs
    serves_share_tokens: bool = True


_TYPES: dict[str, AttachmentType] = {}


def register_attachment_type(attachment_type: AttachmentType) -> None:
    """Register an attachment type. A key registered twice by different owners is refused.

    Registering a key again for the same model, with the same share-token setting, has no effect and the first
    registration stands, so a second ``ready()`` in one process is harmless. The callables are not compared: a lambda
    is a new object each time the module defining it runs.
    """
    current = _TYPES.get(attachment_type.key)
    if current is not None:
        if (current.model_label.lower(), current.serves_share_tokens) != (
            attachment_type.model_label.lower(),
            attachment_type.serves_share_tokens,
        ):
            raise ValueError(f"Attachment type {attachment_type.key!r} is already registered.")
        return
    _TYPES[attachment_type.key] = attachment_type


def registered_attachment_types() -> list[AttachmentType]:
    """The registered attachment types, in key order."""
    return [_TYPES[key] for key in sorted(_TYPES)]


def attachments_of(parents: Iterable[tuple[str, Model]], user, share_token: str | None = None) -> list[dict[str, Any]]:
    """List the attachments of *parents* that the caller may read, each naming its parent.

    *parents* pairs each parent with the public key it is named by, its hash; the parent's primary key is sequential
    and never leaves this function. The caller is expected to have checked that it may read every parent. Each row is
    decided by ``can_read_object`` for the caller and *share_token*. A type that serves no share token is decided
    without the token, since its endpoints take none: it is skipped for a token caller who has no session, and listed
    to a signed-in caller only where their own access reaches it. Items come in the order of *parents*, and by name within a parent. Each item is
    a dict with ``parent`` (the parent's public key), ``type``, ``hash``, ``name``, ``platform_pass`` and ``details``.
    """
    from epicurrents.permissions import can_read_object

    parents = list(parents)
    if not parents:
        return []
    token_only = bool((share_token or "").strip()) and not (user is not None and user.is_authenticated)
    order: dict[tuple[int, str], int] = {}
    public_key: dict[tuple[int, str], str] = {}
    for i, (key, parent) in enumerate(parents):
        ref = (ContentType.objects.get_for_model(parent, for_concrete_model=False).pk, str(parent.pk))
        order[ref] = i
        public_key[ref] = key
    by_type: dict[int, list[str]] = {}
    for ct_id, pk in order:
        by_type.setdefault(ct_id, []).append(pk)

    items: list[tuple[int, str, dict[str, Any]]] = []
    for attachment_type in registered_attachment_types():
        if token_only and not attachment_type.serves_share_tokens:
            continue
        type_token = share_token if attachment_type.serves_share_tokens else None
        model = apps.get_model(attachment_type.model_label)
        for ct_id, pks in by_type.items():
            rows = attachment_type.live(
                model.objects.filter(attachment_content_type_id=ct_id, attachment_object_id__in=pks)
            )
            for obj in rows:
                if not can_read_object(user=user, obj=obj, share_token=type_token):
                    continue
                described = attachment_type.describe(obj)
                ref = (ct_id, obj.attachment_object_id)
                item = {
                    "parent": public_key[ref],
                    "type": attachment_type.key,
                    "hash": described["hash"],
                    "name": described["name"],
                    "platform_pass": attachment_type.platform_pass(obj),
                    "details": described.get("details", {}),
                }
                items.append((order[ref], (item["name"] or "").lower(), item))
    items.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in items]
