"""Derived-state digesters for the recordings app.

`process_recording` writes the final READY audit row with a digest of all
``SignalInfo`` rows attached to the recording, embedded in
``extra_payload``. Tampering with any header-derived ``SignalInfo`` field
after processing changes the recomputed digest, which the audit row's stored
value no longer matches — surfaced by
``activity.derived_state.verify_derived_state``.

``canonical_label`` is **deliberately excluded** from the digest (see
``_DIGEST_EXCLUDED_SIGNAL_FIELDS``): it is a deterministic, derived view of
``label`` (which *is* in the digest), so it adds no tamper-detection value, and
excluding it means introducing the field — and backfilling it on recordings that
were baselined before it existed — cannot raise a false tamper alarm.

The digest is also mixed into the row's ``after_hash``, so an attacker
who tampers with ``SignalInfo`` and then edits the row's stored digest
to match still breaks chain verification (the row's recomputed
``after_hash`` no longer matches its stored value).
"""

import hashlib
import json

from django.contrib.contenttypes.models import ContentType

from activity.audit import serialize_instance

SIGNAL_INFO_DIGEST_KEY = "signal_info_digest"

# Derived ``SignalInfo`` fields kept OUT of the digest. ``canonical_label`` is a
# deterministic function of ``label`` (already covered), so including it would add
# no tamper-detection value while invalidating every digest baselined before the
# field existed. Excluding it keeps the digest a function of the header-parsed
# descriptor only, stable across the field's introduction and its backfill.
_DIGEST_EXCLUDED_SIGNAL_FIELDS = ("canonical_label",)


def _serialize_signal_for_digest(row) -> dict:
    data = serialize_instance(row)
    for field_name in _DIGEST_EXCLUDED_SIGNAL_FIELDS:
        data.pop(field_name, None)
    return data


def compute_signal_info_digest(recording) -> str:
    """Return a deterministic sha256 hex digest over the recording's signals.

    Walks ``RecordingMeta`` → ``SignalInfo`` for the given recording,
    serialises each ``SignalInfo`` row through ``serialize_instance``,
    sorts by channel ``index`` to keep the order stable across DB
    backends, and hashes the canonical JSON.

    Returns ``sha256(b"[]").hexdigest()`` — the deterministic hash of an
    empty JSON array — when the recording has no ``SignalInfo`` rows
    yet (e.g. the row is still PENDING). Same hash for "no rows" vs
    "tampered to remove all rows" — the latter is caught by the parent
    recording's other state diff (signal_count).
    """
    from recordings.models import RecordingMeta, SignalInfo

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    meta = RecordingMeta.objects.filter(content_type=recording_ct, object_id=str(recording.pk)).first()
    if meta is None:
        return _digest([])

    rows = list(SignalInfo.objects.filter(meta=meta).order_by("index"))
    return _digest([_serialize_signal_for_digest(row) for row in rows])


def _digest(items: list[dict]) -> str:
    encoded = json.dumps(items, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
