"""Annotation signals — content-hash maintenance.

``update_parent_hash_on_code_change``: recomputes the parent annotation's
``content_hash`` after any ``Code`` is saved or deleted, keeping the hash in
sync with the current code list.

Cascade-deletion of annotation rows when a target object is deleted is
handled by reverse ``GenericRelation`` fields declared on the target models
(e.g. ``Recording``, ``Collection``, ``Dataset``). See the "GenericFK target
cascade pattern" entry in ``AGENTS.md`` for the rule that target models
must follow.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from annotations.models import Code, Event, Interruption, Label

# Models that support codes — their content_hash must be updated after code changes.
_CODEABLE_MODELS = (Event, Interruption, Label)


@receiver(post_save, sender=Code)
@receiver(post_delete, sender=Code)
def update_parent_hash_on_code_change(sender, instance, **kwargs):
    """Recompute the parent annotation's content_hash after any code mutation.

    The content_hash of Event, Interruption, and Label objects includes a sorted
    snapshot of their codes, so every add/edit/remove must refresh the hash.

    Bulk DML on Code (``queryset.update``, ``bulk_create``, ``bulk_update``,
    raw SQL) does not fire this signal and will leave the parent's
    content_hash stale. This is the same class of gap tracked by the
    ``Activity — close the bulk-operations audit-trail gap`` roadmap item;
    whatever lint / opt-in tracking lands there should naturally cover Code
    as well. If you need to bulk-mutate Codes today, call
    ``parent.recompute_content_hash()`` explicitly on each affected parent.
    """
    parent = instance.annotation
    if parent is not None and isinstance(parent, _CODEABLE_MODELS):
        parent.recompute_content_hash()
