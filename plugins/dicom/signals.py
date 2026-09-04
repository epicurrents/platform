"""Signal receivers for the *dicom* plugin.

``unlink_instance_file`` keeps the filesystem in lock-step with hard deletes:
every path that removes a ``DicomInstance`` row — the purge task's study
cascade, a direct study hard delete, and the account-erasure cascade from
``erase_user`` (``user.delete()`` → studies → series → instances) — unlinks
the stored ``.dcm`` through this one receiver. An unlink failure raises, which
aborts and rolls back the surrounding delete so the row survives for a retry
rather than dangling without its file (or vice versa). This mirrors the
unlink-before-delete ordering contract of ``purge_deleted_media``.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from plugins.dicom.models import DicomInstance

logger = logging.getLogger(__name__)


@receiver(
    pre_delete,
    sender=DicomInstance,
    dispatch_uid="dicom_unlink_instance_file",
)
def unlink_instance_file(sender, instance, **kwargs):
    """Unlink the stored file so every hard-delete path cleans the filesystem.

    A missing file is fine (already purged, or the row never reached READY);
    any other ``OSError`` propagates to abort the delete.
    """
    upload_path = getattr(settings, "DICOM_UPLOAD_PATH", "/data/dicom")
    path = Path(upload_path) / instance.stored_name
    try:
        path.unlink()
    except FileNotFoundError:
        pass
