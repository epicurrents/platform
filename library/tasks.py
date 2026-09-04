"""Celery tasks for the library app — trash retention purge.

``purge_deleted_library`` hard-deletes soft-deleted collections, datasets,
and collection memberships once their retention window expires, mirroring
the recordings / media purge contract. Collection and dataset names are
free text that can carry case or subject identifiers, so trashed rows must
not linger indefinitely (GDPR Art. 5(1)(e)).
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def purge_deleted_library():
    """Hard-delete library rows trashed beyond the retention window.

    Reads ``LIBRARY_TRASH_RETENTION_DAYS`` (default 30). Three querysets,
    each filtered on ``deleted_at__isnull=False, deleted_at__lt=cutoff``:
    ``CollectionItem`` first (so membership rows never outlive an
    explicitly purged trash entry), then ``Collection``, then ``Dataset``.
    Per-row ``delete()`` fires the ``pre_delete`` audit signal inside the
    ``with_system_activity`` scope, and the declared reverse
    ``GenericRelation`` fields cascade access rights, memberships, and
    tags with each purged row.

    Live children of a purged collection re-parent to root via the
    ``parent`` FK's ``SET_NULL`` — purging a trashed parent never removes
    a live descendant.
    """
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from library.models import Collection, CollectionItem, Dataset

    retention_days = getattr(settings, "LIBRARY_TRASH_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=retention_days)

    with with_system_activity(
        "library.purge",
        interface=Activity.Interface.CELERY,
        metadata={"retention_days": retention_days},
    ):
        purged = {}
        for model in (CollectionItem, Collection, Dataset):
            count = 0
            queryset = model.objects.filter(deleted_at__isnull=False, deleted_at__lt=cutoff)
            for row in queryset.iterator():
                row.delete()
                count += 1
            purged[model.__name__] = count

    logger.info(
        "purge_deleted_library: items=%d collections=%d datasets=%d cutoff=%s",
        purged["CollectionItem"],
        purged["Collection"],
        purged["Dataset"],
        cutoff.isoformat(),
    )
    return purged
