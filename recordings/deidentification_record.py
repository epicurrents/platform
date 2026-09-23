"""The per-recording record of the de-identification applied at ingest.

EDPB Guidelines 02/2026 paragraph 41 ask the controller to document the anonymisation process
and keep that documentation. The platform's record has three parts, and this module names them:

- ``RecordingMeta.deidentification_version`` — which version of the header and channel-block
  pass (``DEIDENTIFICATION_VERSION`` in :mod:`recordings.processors.edf`) wrote the stored
  file. ``0`` is a file processed before the record existed.
- ``RecordingMeta.annotation_text_preserved`` — whether the stored file keeps the annotation
  text it arrived with. Stamped together with the version, so it means nothing on a ``0`` row.
- ``deidentification_record`` in the READY transition's audit ``extra_payload`` — the two values
  above, the channel-order version, and the ingest privacy overrides in force at the time. The
  audit row is tamper-evident, so the record cannot be edited after the fact, and the overrides
  are kept nowhere else once the deployment's settings change.

``manage.py deidentification_report`` presents the record per recording and flags every
recording written by an older pass than the current one.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from recordings.processors.channel_labels import CHANNEL_ORDER_VERSION
from recordings.processors.edf import DEIDENTIFICATION_VERSION

DEIDENTIFICATION_RECORD_KEY = "deidentification_record"

# The ingest privacy overrides, with their declared defaults. The record captures
# every one of them whether or not it is on, because "the default was in force"
# is itself the fact an assessment needs.
INGEST_OVERRIDE_SETTINGS: dict[str, bool] = {
    "RECORDINGS_DISCARD_ORIGINAL_NAME": False,
    "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS": False,
    "RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA": False,
    "RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS": True,
}


def current_ingest_overrides() -> dict[str, bool]:
    """Return the ingest privacy overrides as the running deployment resolves them."""
    return {name: bool(getattr(settings, name, default)) for name, default in INGEST_OVERRIDE_SETTINGS.items()}


def current_pass_versions() -> dict[str, int]:
    """Return the version each stamped pass has in the running code."""
    return {
        "deidentification_version": DEIDENTIFICATION_VERSION,
        "channel_order_version": CHANNEL_ORDER_VERSION,
    }


def _meta_for(recording):
    from recordings.models import RecordingMeta

    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return RecordingMeta.objects.filter(content_type=ct, object_id=str(recording.pk)).first()


def build_deidentification_record(recording) -> dict | None:
    """Return the record to seal into the recording's READY audit row, or ``None`` without a meta row.

    A recording with no ``RecordingMeta`` was not written by the pass — a FAILED ingest, or a
    format the platform stores without processing — so there is nothing to record.
    """
    meta = _meta_for(recording)
    if meta is None:
        return None
    return {
        "deidentification_version": meta.deidentification_version,
        "annotation_text_preserved": meta.annotation_text_preserved,
        "channel_order_version": meta.channel_order_version,
        "ingest_overrides": current_ingest_overrides(),
    }


def _record_rows(recording_ct, **filters):
    from activity.models import ObjectChangeLog

    return ObjectChangeLog.objects.filter(
        content_type=recording_ct,
        action=ObjectChangeLog.ACTION_MODIFY,
        extra_payload__has_key=DEIDENTIFICATION_RECORD_KEY,
        **filters,
    )


def stored_deidentification_record(recording) -> dict | None:
    """Return the record from the newest audit row carrying one, or ``None``.

    ``None`` is the answer for every recording processed before the record existed, and for one
    whose trail was written outside the ingest paths that seal it.
    """
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    row = _record_rows(ct, object_id=str(recording.pk)).order_by("-created_at", "-pk").first()
    if row is None:
        return None
    return row.extra_payload[DEIDENTIFICATION_RECORD_KEY]


def stored_deidentification_records() -> dict[str, dict]:
    """Return the newest record per recording, keyed by the recording's pk as a string.

    The batch form of :func:`stored_deidentification_record` for a sweep: one query over the
    rows that carry the key, walked oldest first so the last write per recording wins.
    """
    from recordings.models import Recording

    ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    records: dict[str, dict] = {}
    rows = _record_rows(ct).order_by("created_at", "pk").values_list("object_id", "extra_payload")
    for object_id, payload in rows.iterator(chunk_size=500):
        records[object_id] = payload[DEIDENTIFICATION_RECORD_KEY]
    return records
