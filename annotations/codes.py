"""Writing the classification codes of an annotation.

The annotations API takes an annotation's codes inline with the write that creates or changes it, and a project
that writes annotations through its own endpoint does the same through ``replace_codes``, so the rows and their
audit trail come out alike on every path. Which codes a path accepts is the path's decision and is made before this
is called; ``replace_codes`` validates nothing.
"""

from collections.abc import Mapping
from typing import Any

from django.contrib.contenttypes.models import ContentType

from annotations.models import Code


def replace_codes(parent, codes: Mapping[str, tuple[str, Any]]) -> None:
    """Make *parent*'s codes exactly *codes*, a ``(value, meta)`` pair per standard.

    A code whose standard stays is changed in place and one whose standard is gone is deleted. Rows are saved one at
    a time so each change reaches the audit trail through the model signals; call it inside the write's transaction.
    """
    content_type = ContentType.objects.get_for_model(type(parent))
    existing = {
        code.standard: code for code in Code.objects.filter(content_type=content_type, object_id=str(parent.pk))
    }
    for standard, code in existing.items():
        if standard not in codes:
            code.delete()
    for standard, (value, meta) in codes.items():
        code = existing.get(standard)
        if code is None:
            Code.objects.create(
                content_type=content_type,
                object_id=str(parent.pk),
                standard=standard,
                value=value,
                meta=meta,
            )
        elif code.value != value or code.meta != meta:
            code.value = value
            code.meta = meta
            code.save()
    # A prefetched set would serialise the codes as they were before this write.
    getattr(parent, "_prefetched_objects_cache", {}).pop("codes", None)
