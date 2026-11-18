"""Subject fields: the schema a dataset declares for its members, and the values each member carries.

A dataset's *subject schema* (``DatasetMeta.subject_schema``) is an ordered list of fields, each a key, a label and
a fixed list of choices. There is no free-text field and no exact number: an age is declared as bands, a diagnosis
as categories, so a value names a category rather than describing a person, the line the annotation-text rule
draws for a code's value. A member's *values* (``DatasetItemSubject.values``) map a schema key to one of its
choices. They are set by the dataset's managers, and in a submission pool they also arrive with each submission,
in its sidecar's ``subject_fields`` key, which the submission gate checks against the schema
(``recordings.submissions.validate_subject_fields``).

The rules, each enforced here and called by the endpoints in ``library.api.v1.ninja``:

* A schema is validated as a whole at every write, and a change that removes a field or a choice still in use is
  refused, so a stored value always names a declared choice.
* A pool's schema is fixed once the pool fills, as its profile is: removing or rebanding a field changes the
  classes a past release was sized on, and the submissions were checked against it. Before it fills, a key the
  pool's profile forbids in a sidecar is refused, since no submission could carry it.
* A released member of a release-gated dataset keeps its values. The release was decided on them.
* On a release-gated dataset, a reader who does not manage it is shown no fields and no values. Which fields may
  be shown to readers there is a separate declaration, made against the equivalence class.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

MAX_FIELDS = 24
MAX_CHOICES = 64
MAX_LABEL_LENGTH = 80
MAX_CHOICE_LENGTH = 64
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FIELD_KEYS = {"key", "label", "choices"}


@dataclass
class SubjectError(Exception):
    """A subject rule refused a write. ``status`` is the HTTP status the endpoint answers with."""

    status: int
    message: str

    def __str__(self) -> str:
        return self.message


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubjectError(400, f"{what} must be a non-empty string.")
    value = value.strip()
    if len(value) > limit:
        raise SubjectError(400, f"{what} is longer than {limit} characters.")
    return value


def validate_schema(raw: Any) -> list[dict]:
    """The normalised form of a submitted schema, or :class:`SubjectError` (400) naming the first fault.

    Strings are stripped. Keys are lower-case identifiers, unique within the schema; choices are unique within
    their field. A field carries exactly ``key``, ``label`` and ``choices``, so a misspelt attribute is refused
    rather than dropped.
    """
    if not isinstance(raw, list):
        raise SubjectError(400, "The subject schema must be a list of fields.")
    if len(raw) > MAX_FIELDS:
        raise SubjectError(400, f"A subject schema has at most {MAX_FIELDS} fields.")
    fields: list[dict] = []
    seen_keys: set[str] = set()
    for index, field in enumerate(raw):
        if not isinstance(field, dict) or set(field) != _FIELD_KEYS:
            raise SubjectError(400, f"Field {index} must have exactly the attributes key, label and choices.")
        key = field["key"]
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise SubjectError(
                400, f"Field {index} key must start with a lower-case letter and use only a-z, 0-9 and _."
            )
        if key in seen_keys:
            raise SubjectError(400, f"The key {key} is declared twice.")
        seen_keys.add(key)
        label = _text(field["label"], f"The label of {key}", MAX_LABEL_LENGTH)
        choices = field["choices"]
        if not isinstance(choices, list) or not choices:
            raise SubjectError(400, f"The choices of {key} must be a non-empty list.")
        if len(choices) > MAX_CHOICES:
            raise SubjectError(400, f"{key} has more than {MAX_CHOICES} choices.")
        normalised = [_text(choice, f"A choice of {key}", MAX_CHOICE_LENGTH) for choice in choices]
        if len(set(normalised)) != len(normalised):
            raise SubjectError(400, f"The choices of {key} are not unique.")
        fields.append({"key": key, "label": label, "choices": normalised})
    return fields


def schema_warnings(schema: list[dict]) -> list[dict[str, str]]:
    """Free-text warnings for a schema's labels and choices, which every reader of the dataset is shown as typed."""
    from epicurrents.text_hygiene import name_warnings

    rows: list[dict[str, str]] = []
    for field in schema:
        rows.extend(name_warnings(**{f"{field['key']}.label": field["label"]}))
        for choice in field["choices"]:
            rows.extend(name_warnings(**{f"{field['key']}.choices": choice}))
    return rows


def schema_of(dataset: Any) -> list[dict]:
    """The dataset's subject schema; empty when it declares none."""
    from library.models import DatasetMeta

    meta = DatasetMeta.objects.filter(dataset=dataset).only("subject_schema").first()
    return list(meta.subject_schema) if meta is not None else []


def values_in_use(dataset: Any) -> Counter:
    """How many of the dataset's members carry each ``(key, choice)`` pair."""
    from library.models import DatasetItemSubject

    used: Counter = Counter()
    for values in DatasetItemSubject.objects.filter(item__dataset=dataset).values_list("values", flat=True):
        used.update((values or {}).items())
    return used


def ensure_schema_change_allowed(dataset: Any, schema: list[dict]) -> None:
    """Refuse a schema change to a filling pool, or one that drops a field or choice a member still carries.

    The dataset row must already be locked by the caller, so a submission cannot fill the pool in between.
    """
    from library.pools import is_filling, is_pool

    if schema == schema_of(dataset):
        return
    if is_pool(dataset) and is_filling(dataset):
        raise SubjectError(
            409, "The subject schema of a filling pool cannot change: its members were checked against it."
        )
    if is_pool(dataset):
        from recordings.submissions import get_ingest_profile

        profile = get_ingest_profile(dataset.submission_profile)
        forbidden = profile.all_forbidden_sidecar_keys if profile is not None else frozenset()
        clashing = [field["key"] for field in schema if field["key"] in forbidden]
        if clashing:
            raise SubjectError(
                400, f"The key {clashing[0]} is forbidden in the pool's sidecars, so no submission could carry it."
            )
    declared = {(field["key"], choice) for field in schema for choice in field["choices"]}
    dropped = sorted(pair for pair in values_in_use(dataset) if pair not in declared)
    if dropped:
        key, choice = dropped[0]
        raise SubjectError(409, f"The choice {choice!r} of {key} is in use; clear it from the members first.")


def apply_values(schema: list[dict], current: dict, changes: Any) -> dict:
    """*current* with *changes* merged in: a key set to a choice of its field, or cleared with ``null``.

    :class:`SubjectError` (400) for a key the schema does not declare or a value that is not one of its choices.
    """
    if not isinstance(changes, dict):
        raise SubjectError(400, "Values must be an object of field keys.")
    choices = {field["key"]: field["choices"] for field in schema}
    merged = dict(current or {})
    for key, value in changes.items():
        if key not in choices:
            raise SubjectError(400, f"The dataset declares no subject field {key}.")
        if value is None:
            merged.pop(key, None)
        elif not isinstance(value, str) or value not in choices[key]:
            raise SubjectError(400, f"The value for {key} is not one of its choices.")
        else:
            merged[key] = value
    return merged


def ensure_values_editable(dataset: Any, item: Any) -> None:
    """Refuse changing the values of a released member of a release-gated dataset: the release was decided on them."""
    if dataset.release_gated and item.release_id is not None:
        raise SubjectError(409, "A released member's subject values are fixed.")


def shown_schema(dataset: Any, manager: bool) -> list[dict]:
    """The schema a caller is shown: all of it, or nothing to a reader who does not manage a release-gated dataset."""
    if dataset.release_gated and not manager:
        return []
    return schema_of(dataset)


def values_by_object_id(dataset: Any, content_type: Any, object_ids: list) -> dict[str, dict]:
    """The values of the dataset's members of *content_type* among *object_ids*, keyed by ``object_id``."""
    from library.models import DatasetItemSubject

    rows = DatasetItemSubject.objects.filter(
        item__dataset=dataset,
        item__content_type=content_type,
        item__object_id__in=[str(object_id) for object_id in object_ids],
    ).values_list("item__object_id", "values")
    return {object_id: values or {} for object_id, values in rows}
