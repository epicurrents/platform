"""Subject erasure (GDPR Art. 17) across the audit trail.

⚠️ LOAD-BEARING — GDPR Art. 17 subject-erasure pathway for audit data.
``erase_subject`` is the routine that removes a data subject's personal
data from ``ObjectChangeLog`` payloads and ``Activity`` metadata after
the account itself is deleted. Its contract has two halves that must not
regress independently:

- **Completeness** — every registered PII field of every registered
  subject model is scrubbed, actor-side and subject-side. Silent
  narrowing (a lost registration, a filter change) leaves personal data
  in the permanent audit trail with no visible signal, which is exactly
  the unfulfillable-erasure gap this module exists to close.
- **Chain integrity** — scrubbing must not invalidate the audit chain.
  Rows are tombstoned: payload fields are replaced by ``ERASED_SENTINEL``,
  ``after_hash`` is left untouched (downstream ``prev_hash`` links keep
  verifying), and the scrubbed payload is re-sealed under ``erased_hash``
  so post-erasure tampering stays detectable. The erasure itself is
  recorded as a chained ``ACTION_ERASE`` row on the subject's shard.

``scrub_rows`` is the second entry point, keyed by rows rather than by a
subject: the caller names a model, the object ids and the fields, and every
change-log row of those objects is scrubbed and sealed the same way, with a
chained ``ACTION_ERASE`` record of the run on each shard it touched. It is
for a value that is not personal data alone but would join two records the
platform keeps apart, such as a reviewed copy and the pooled file made from
it. The same two halves hold: a narrowed selection leaves the join in the
permanent trail, and a broken seal makes the scrubbed rows unverifiable.

Contract test: ``activity/tests/test_erasure.py``. See AGENTS.md →
*Load-bearing files* before modifying.

Owning apps register their models' PII surface from ``AppConfig.ready()``
via ``register_subject_pii`` — see user/apps.py and notifications/apps.py
for the core registrations. Project plugins with their own user-linked
PII models register the same way.
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .audit import (
    compute_erased_hash,
    create_chained_change_log,
    current_erased_hash_version,
)
from .models import Activity, ObjectChangeLog

ERASED_SENTINEL = "<erased>"

# Activity.metadata keys that carry (pseudonymous) personal data and are
# stripped from the subject's targeted Activity rows during erasure.
ACTIVITY_METADATA_PII_KEYS = frozenset({"email_hash"})


@dataclass(frozen=True)
class SubjectPiiSpec:
    """PII surface of one audited model, as registered by its owning app.

    ``owner_field`` is the serialized attname that links a row's payload to
    the subject user (e.g. ``"user_id"``), or ``None`` for the user model
    itself, where the link is the row's ``object_id``. ``pii_fields`` are
    the payload keys scrubbed on erasure. ``historical`` marks a model that
    has since been dropped from the schema: its audit rows outlive it and
    still need scrubbing, so the registration must outlive it too.
    """

    model_label: str
    owner_field: str | None
    pii_fields: frozenset[str]
    historical: bool = False


_SUBJECT_PII: dict[str, SubjectPiiSpec] = {}


def register_subject_pii(model_label: str, *, owner_field, pii_fields, historical: bool = False) -> None:
    """Register *model_label* (lowercase ``app_label.model_name``) for erasure.

    Re-registering a label replaces the previous spec, so an app can extend
    its own registration in tests without duplicate entries.

    Set ``historical=True`` when the registered model has been removed from
    the schema. The scrub itself needs only the ContentType row, which
    survives a model deletion, so erasure keeps reaching the old audit rows —
    but the system check can no longer validate field names against a model
    that is gone, and without the flag it would fail the deployment for a
    registration that is doing exactly its job. The flag flips the check's
    posture: a historical registration whose model still exists is the error.

    The flag also forfeits the check's typo protection — a misspelled
    historical label passes ``manage.py check`` and scrubs nothing, reporting
    the same zero rows as a clean run. Pin every historical registration with
    a test that scrubs synthesized rows end-to-end in the project that
    registers it.
    """
    _SUBJECT_PII[model_label] = SubjectPiiSpec(
        model_label=model_label,
        owner_field=owner_field,
        pii_fields=frozenset(pii_fields),
        historical=historical,
    )


def registered_subject_pii() -> dict[str, SubjectPiiSpec]:
    """Return a copy of the current registry, for introspection and tests."""
    return dict(_SUBJECT_PII)


def _scrub_row(row: ObjectChangeLog, pii_fields: frozenset[str]) -> bool:
    """Tombstone one change-log row: scrub PII fields and re-seal.

    Returns ``True`` when the row was mutated. Rows with nothing left to
    scrub are left untouched, so repeat erasure runs are idempotent and do
    not churn the seal.
    """
    mutated = False
    for field in pii_fields:
        if field in row.before_state and row.before_state[field] != ERASED_SENTINEL:
            row.before_state[field] = ERASED_SENTINEL
            mutated = True
        if (
            row.changes
            and field in row.changes
            and row.changes[field] != {"from": ERASED_SENTINEL, "to": ERASED_SENTINEL}
        ):
            row.changes[field] = {
                "from": ERASED_SENTINEL,
                "to": ERASED_SENTINEL,
            }
            mutated = True

    if not mutated:
        return False

    algorithm, key_version = current_erased_hash_version()
    row.erased_at = timezone.now()
    row.erased_hash_algorithm = algorithm
    row.erased_hash_key_version = key_version
    row.erased_hash = compute_erased_hash(
        before_state=row.before_state,
        changes=row.changes,
        after_hash=row.after_hash,
        content_type_id=row.content_type_id,
        object_id=row.object_id,
        action=row.action,
        erased_at=row.erased_at.isoformat(),
        algorithm=algorithm,
        hash_key_version=key_version,
    )
    row.save(
        update_fields=[
            "before_state",
            "changes",
            "erased_at",
            "erased_hash",
            "erased_hash_algorithm",
            "erased_hash_key_version",
        ]
    )
    return True


def erase_subject(user_pk, *, erased_by=None) -> dict:
    """Scrub a data subject's personal data from the audit trail.

    Walks every registered subject model, tombstoning matching
    ``ObjectChangeLog`` rows (see ``_scrub_row``), strips PII metadata keys
    from ``Activity`` rows targeting the subject, and appends a chained
    ``ACTION_ERASE`` record on the user shard summarising what was erased.
    Runs after the ``User`` row is deleted (or for a user deleted in the
    past), so it matches rows by primary key, not by instance.

    Rows are matched actor-side and subject-side: the user model's own rows
    by ``object_id``, dependent models (external identities, push
    subscriptions, …) by the serialized owner FK inside ``before_state``.
    Integer foreign keys referencing the user elsewhere in the trail are
    intentionally retained — once the account row and its payloads are
    erased, the bare pk no longer relates to an identifiable person.

    Returns a summary dict: per-model scrub counts plus the count of
    Activity rows whose metadata was stripped.

    The whole scrub is wrapped in ``transaction.atomic()`` so a failure
    partway leaves no half-erased state.
    """
    user_model = get_user_model()
    user_ct = ContentType.objects.get_for_model(user_model)
    object_id = str(user_pk)

    summary: dict[str, int] = {}
    with transaction.atomic():
        for spec in _SUBJECT_PII.values():
            app_label, _, model_name = spec.model_label.partition(".")
            content_type = ContentType.objects.filter(app_label=app_label, model=model_name).first()
            if content_type is None:
                continue

            if spec.owner_field is None:
                rows = ObjectChangeLog.objects.filter(content_type=content_type, object_id=object_id)
            else:
                rows = ObjectChangeLog.objects.filter(
                    content_type=content_type,
                    **{f"before_state__{spec.owner_field}": user_pk},
                )
            # Erasure records carry no subject PII (their payload is the
            # scrub summary) and must never be re-tombstoned.
            rows = rows.exclude(action=ObjectChangeLog.ACTION_ERASE)

            scrubbed = 0
            for row in rows.iterator():
                if _scrub_row(row, spec.pii_fields):
                    scrubbed += 1
            summary[spec.model_label] = scrubbed

        activities_stripped = 0
        targeted = Activity.including_archived.filter(target_content_type=user_ct, target_object_id=object_id)
        for activity in targeted.iterator():
            metadata = activity.metadata or {}
            pii_keys = {key for key in ACTIVITY_METADATA_PII_KEYS & metadata.keys() if metadata[key] != ERASED_SENTINEL}
            if not pii_keys:
                continue
            for key in pii_keys:
                metadata[key] = ERASED_SENTINEL
            activity.metadata = metadata
            activity.save(update_fields=["metadata"])
            activities_stripped += 1
        summary["activity.metadata"] = activities_stripped

        erase_summary = {
            "erased": {label: count for label, count in summary.items()},
            "reason": "gdpr_art17",
        }
        create_chained_change_log(
            content_type=user_ct,
            object_id=object_id,
            action=ObjectChangeLog.ACTION_ERASE,
            performed_by=erased_by,
            before_state=erase_summary,
            changes=None,
            hash_payload=erase_summary,
            timestamp=timezone.now(),
        )

    return summary


# A row scrub's reason is a code, never text, because the record of the run is permanent.
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")

# Object ids per query; keeps the IN list inside SQLite's bound-parameter limit.
_ID_CHUNK = 500


@dataclass(frozen=True)
class RowScrub:
    """The change-log rows of *object_ids* of *model_label*, and the payload fields to scrub from them.

    ``fields`` are serialized attnames, as ``serialize_instance`` writes them, so a foreign key is
    ``recording_id`` rather than ``recording``. A bare string for either is refused: iterated, it
    would name one-character ids or fields, and a wrong id scrubs another object without an error.
    """

    model_label: str
    object_ids: tuple[str, ...]
    fields: frozenset[str]

    def __init__(self, model_label: str, object_ids: Iterable, fields: Iterable[str]):
        if isinstance(object_ids, str | bytes) or isinstance(fields, str | bytes):
            raise TypeError("RowScrub takes object_ids and fields as collections, not a single string")
        object.__setattr__(self, "model_label", model_label)
        object.__setattr__(self, "object_ids", tuple(dict.fromkeys(str(object_id) for object_id in object_ids)))
        object.__setattr__(self, "fields", frozenset(fields))


def _validate_scrub(scrub: RowScrub):
    """Return the scrub's content type, or raise when it names a model or field that does not exist.

    A wrong name scrubs nothing and reports zero rows, which a clean run reports too, so it is
    refused before anything is written.
    """
    try:
        model = apps.get_model(scrub.model_label)
    except (LookupError, ValueError) as error:
        raise ValueError(f"scrub_rows: {scrub.model_label!r} is not an installed model") from error
    if not scrub.fields:
        raise ValueError(f"scrub_rows: the scrub of {scrub.model_label} names no fields")
    attnames = {field.attname for field in model._meta.concrete_fields}
    unknown = sorted(scrub.fields - attnames)
    if unknown:
        raise ValueError(f"scrub_rows: {scrub.model_label} has no field {', '.join(unknown)}")
    return ContentType.objects.get_for_model(model, for_concrete_model=False)


def scrub_rows(scrubs: Iterable[RowScrub], *, reason: str, performed_by=None) -> dict[str, int]:
    """Scrub the named fields from every change-log row of the named objects, and record the run.

    Each row is tombstoned and sealed as ``erase_subject`` does it, so the chain keeps verifying
    and later tampering is detected. Delete and rollback rows are selected with the rest, which
    is what lets a caller delete objects and scrub their trail in one transaction: a delete
    record's state is outside its seal until the scrub seals it.

    The run appends one chained ``ACTION_ERASE`` row on each shard it scrubbed, all under one
    random run id as their object id. The record carries the row count and the field names per
    model and *reason*, a lower-case code; it names no object, because the objects are what the
    scrub exists to stop joining.

    Every scrub is validated before any write, and the whole run is atomic. Returns the scrubbed
    row count per model label.
    """
    scrubs = list(scrubs)
    if not _REASON_RE.match(reason or ""):
        raise ValueError(f"scrub_rows: reason {reason!r} is not a lower-case code")
    content_types = [_validate_scrub(scrub) for scrub in scrubs]

    summary: dict[str, int] = {}
    fields: dict[str, set[str]] = {}
    shards: dict[str, ContentType] = {}
    with transaction.atomic():
        for scrub, content_type in zip(scrubs, content_types, strict=True):
            scrubbed = 0
            for start in range(0, len(scrub.object_ids), _ID_CHUNK):
                rows = ObjectChangeLog.objects.filter(
                    content_type=content_type,
                    object_id__in=scrub.object_ids[start : start + _ID_CHUNK],
                ).exclude(action=ObjectChangeLog.ACTION_ERASE)
                for row in rows.iterator():
                    if _scrub_row(row, scrub.fields):
                        scrubbed += 1
            summary[scrub.model_label] = summary.get(scrub.model_label, 0) + scrubbed
            fields.setdefault(scrub.model_label, set()).update(scrub.fields)
            shards[scrub.model_label] = content_type

        record = {
            "erased": dict(sorted(summary.items())),
            "fields": {label: sorted(names) for label, names in sorted(fields.items())},
            "reason": reason,
        }
        run_id = uuid.uuid4().hex
        timestamp = timezone.now()
        for label in sorted(shards):
            create_chained_change_log(
                content_type=shards[label],
                object_id=run_id,
                action=ObjectChangeLog.ACTION_ERASE,
                performed_by=performed_by,
                before_state=record,
                changes=None,
                hash_payload=record,
                timestamp=timestamp,
            )

    return summary
