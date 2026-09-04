"""Converter sidecar persistence — the general path that turns a converter-emitted sidecar into an ``Annotation``.

This module is **not** part of any single converter, despite living next to them. ``dispatch_post_convert``
fires for every successful conversion regardless of source format, and ``save_sidecar_events`` holds the
``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`` gate — one of that setting's two enforcement routes (the other is
the EDF+ TAL parse in ``recordings.tasks``). Omitting or bypassing this module silently disables the
discard guarantee for converter-sourced events.

## Sidecar event schema

A sidecar dict carries up to two parallel lists. Their per-item schema is pinned here — it is the contract a
converter must emit, and ``save_sidecar_events`` validates against it rather than silently absorbing unknown
shapes (a converter emitting different key names must fail loudly, not produce rows of null onsets):

* ``annotations`` — clinical text events: ``onset_seconds`` (required, numeric), ``duration_seconds``
  (optional, numeric or null), ``text`` (optional, string).
* ``events`` — system events (e.g. "Recording Paused"): ``onset_seconds`` (required, numeric),
  ``duration_seconds`` (optional, numeric or null), ``type`` and ``label`` (optional, strings).

Both lists are merged into a single ``{"events": [...]}`` annotation whose per-item format mirrors
``recordings.tasks._save_edf_results`` (``onset``, ``duration``, ``label``). The annotation is saved as
``"Source events"`` with the hash suffix ``"source-events"`` so it is distinct from the ``"Original
annotations"`` record written by the EDF processor.

``handle_post_convert`` is the built-in handler for the Nicolet ``.e`` converter's sidecar and doubles as
the worked example of the ``post_convert`` hook contract — a plugin author registering a converter for a
different format (e.g. ``.ncs`` Neuralynx) would put a similar shape-filtered handler under their plugin's
directory and register it the same way.

Two callers:

* ``RecordingsConfig.ready()`` registers ``handle_post_convert`` so the Celery worker's
  ``process_recording`` path fires this automatically via the hook dispatcher.
* ``recordings.management.commands.import_recordings`` calls ``save_sidecar_events`` directly because the
  bulk-import path does not currently use the hook dispatcher. Tracked as a follow-up to wire bulk import
  through dispatch_post_convert so every converter-emitted sidecar is handled the same way regardless of
  ingest path.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Pinned per-item schemas: key → (required, allowed types). ``bool`` is excluded from the numeric
# fields explicitly because ``isinstance(True, int)`` holds in Python.
_ANNOTATION_ITEM_SCHEMA: dict[str, tuple[bool, tuple[type, ...]]] = {
    "onset_seconds": (True, (int, float)),
    "duration_seconds": (False, (int, float, type(None))),
    "text": (False, (str,)),
}
_EVENT_ITEM_SCHEMA: dict[str, tuple[bool, tuple[type, ...]]] = {
    "onset_seconds": (True, (int, float)),
    "duration_seconds": (False, (int, float, type(None))),
    "type": (False, (str,)),
    "label": (False, (str,)),
}


def _annotation_hash(recording_pk: int, suffix: str) -> str:
    """Mirror of ``recordings.tasks._annotation_hash`` (kept local to avoid
    importing from tasks, which would create an import cycle when this
    module is loaded from ``RecordingsConfig.ready``)."""
    key = f"{recording_pk}:{suffix}"
    return hashlib.sha256(key.encode()).hexdigest()[:32].upper()


def _validate_items(items: list, list_name: str, schema: dict[str, tuple[bool, tuple[type, ...]]]) -> None:
    """Raise ``ValueError`` naming the first item that violates the pinned schema."""
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            # TRY004 suppressed: the schema contract raises a single exception type for every
            # violation — the input is untrusted converter data, not a caller programming error.
            raise ValueError(f"sidecar {list_name}[{index}] is not an object")  # noqa: TRY004
        for key, (required, allowed) in schema.items():
            if key not in item:
                if required:
                    raise ValueError(f"sidecar {list_name}[{index}] is missing required key {key!r}")
                continue
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, allowed):
                raise ValueError(  # noqa: TRY004 — same single-exception-type contract as above
                    f"sidecar {list_name}[{index}] key {key!r} has invalid type {type(value).__name__}"
                )


def validate_sidecar_events(sidecar_data: dict) -> None:
    """Validate a sidecar dict against the pinned event schema; raise ``ValueError`` on the first violation.

    The two lists are optional and may be empty; when present each must be a list of objects matching the
    schema in the module docstring. Unknown extra keys on an item are ignored — the pin constrains the keys
    this module reads, not the converter's whole output.
    """
    if not isinstance(sidecar_data, dict):
        # TRY004 suppressed here too — one exception type for every schema violation.
        raise ValueError("sidecar data is not an object")  # noqa: TRY004
    annotations = sidecar_data.get("annotations")
    if annotations is not None:
        if not isinstance(annotations, list):
            raise ValueError('sidecar "annotations" is not a list')
        _validate_items(annotations, "annotations", _ANNOTATION_ITEM_SCHEMA)
    events = sidecar_data.get("events")
    if events is not None:
        if not isinstance(events, list):
            raise ValueError('sidecar "events" is not a list')
        _validate_items(events, "events", _EVENT_ITEM_SCHEMA)


def _looks_like_nicolet_sidecar(sidecar_data: dict) -> bool:
    """Return True when the dict matches the Nicolet sidecar shape.

    Filters out other converters' sidecars that might be registered in
    the future — the post_convert dispatcher fires this handler for every
    successful conversion regardless of source format, so the handler
    must self-filter.
    """
    if not isinstance(sidecar_data, dict):
        return False
    return isinstance(sidecar_data.get("annotations"), list) or isinstance(sidecar_data.get("events"), list)


def save_sidecar_events(recording, sidecar_data: dict) -> None:
    """Persist sidecar events as a ``"Source events"`` annotation row.

    Validates ``sidecar_data`` against the pinned schema first and raises ``ValueError`` on a mismatch —
    both callers catch and log it, so a converter emitting the wrong shape fails loudly per recording
    without aborting the surrounding ingest. No-op when the two parallel lists are both empty. Idempotent
    against re-runs only insofar as the ``object_hash`` is keyed on ``recording.pk`` — calling twice would
    raise the per-target uniqueness constraint, but the production paths call this once per upload.
    """
    from django.conf import settings
    from django.contrib.contenttypes.models import ContentType

    from annotations.models import Annotation
    from epicurrents.system_user import get_system_user

    # Gated here rather than at the call sites so that every path honours it —
    # the Celery post_convert hook and import_recordings both reach this
    # function, and a deployment that discards file-borne annotations must not
    # depend on which one ran. Sidecar events carry the acquisition software's
    # own event vocabulary, which identifies the recording laboratory.
    if getattr(settings, "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", False):
        return

    validate_sidecar_events(sidecar_data)

    events: list[dict] = []

    for ann in sidecar_data.get("annotations") or []:
        events.append(
            {
                "onset": ann["onset_seconds"],
                "duration": ann.get("duration_seconds"),
                "label": ann.get("text", ""),
            }
        )

    for evt in sidecar_data.get("events") or []:
        label_parts = [p for p in [evt.get("type"), evt.get("label")] if p]
        events.append(
            {
                "onset": evt["onset_seconds"],
                "duration": evt.get("duration_seconds"),
                "label": ": ".join(label_parts),
            }
        )

    if not events:
        return

    events.sort(key=lambda e: e.get("onset") or 0)

    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    Annotation.objects.create(
        author=get_system_user(),
        name="Source events",
        target_content_type=recording_ct,
        target_object_id=str(recording.pk),
        object_hash=_annotation_hash(recording.pk, "source-events"),
        content={"events": events},
    )


def handle_post_convert(recording, source_path: Path, converted_path: Path, sidecar_data) -> None:
    """post_convert handler — parse a Nicolet-shaped sidecar when present.

    Skipped when ``sidecar_data`` is ``None`` (the converter produced no
    sidecar) or when the shape doesn't match the Nicolet format (a different
    converter ran).
    """
    if sidecar_data is None or not _looks_like_nicolet_sidecar(sidecar_data):
        return
    try:
        save_sidecar_events(recording, sidecar_data)
    except Exception as exc:
        # Hook is registered in soft mode (see RecordingsConfig.ready) so
        # this log line is the only externalised signal. Re-raising here
        # would defeat the soft-mode contract.
        logger.warning(
            "sidecar.handle_post_convert: failed to save sidecar events for recording %s: %s",
            recording.pk,
            exc,
            exc_info=True,
        )
