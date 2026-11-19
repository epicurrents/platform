"""Subject fields: the schema a dataset declares for its members, and the values each member carries.

A dataset's *subject schema* (``DatasetMeta.subject_schema``) is an ordered list of fields, each a key, a label and
either a fixed list of choices or the type ``number``. There is no free-text field: an age is declared as bands or
as a number, a diagnosis as categories, so a value names a category or a quantity rather than describing a person,
the line the annotation-text rule draws for a code's value. A member's *values* (``DatasetItemSubject.values``) map
a schema key to one of its choices, or to a finite number for a number field. They are set by the dataset's
managers, and in a submission pool they also arrive with each submission, in its sidecar's ``subject_fields`` key,
which the submission gate checks against the schema (``recordings.submissions.validate_subject_fields``).

The rules, each enforced here and called by the endpoints in ``library.api.v1.ninja``:

* A schema is validated as a whole at every write, and a change that removes a field or a choice still in use is
  refused, so a stored value always names a declared choice.
* A pool's schema is fixed once the pool fills, as its profile is: removing or rebanding a field changes the
  classes a past release was sized on, and the submissions were checked against it. Before it fills, a key the
  pool's profile forbids in a sidecar is refused, since no submission could carry it.
* A released member of a release-gated dataset keeps its values. The release was decided on them.
* On a release-gated dataset, a reader who does not manage it is shown only the fields the schema marks
  ``displayed``, and only their values. A displayed field is part of the equivalence class: the release gate and the
  anonymity report count a member's class as the project's class key together with its displayed values
  (:func:`subject_class_keys`), so a reader who combines what the table shows finds no class smaller than the one
  the release was sized on. Once a dataset has a release, which fields are displayed is fixed.
* A dataset that has had a release keeps hiding the fields it does not display after its gate is turned off: its
  released members were sized on the displayed fields alone, and their values are fixed, so the others could never
  be cleared first.
* Elsewhere ``displayed`` has no effect: a dataset's readers are shown every field it declares.
* A number field is declared only on a dataset that is not release-gated, not a pool and has had no release, and
  such a dataset cannot be gated or made a pool while it declares one (:func:`number_field_refusal`). A number is
  never displayed: on a gated dataset each value would be a class of its own. The field exists for a dataset
  re-released from a published one whose publishers already settled how fine its values are.

Nothing here judges whether a schema is too fine-grained. Outside a release-gated dataset no class is sized at all,
and inside one only the displayed fields are, so whether the fields a dataset declares could together identify a
subject is a judgement made before the dataset is put on the platform, not one the platform makes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

MAX_FIELDS = 24
MAX_CHOICES = 64
MAX_LABEL_LENGTH = 80
MAX_CHOICE_LENGTH = 64
#: The largest magnitude a number value may have, so a value stays exact in every JSON reader and in a float column.
MAX_NUMBER = 1e15
NUMBER = "number"
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FIELD_KEYS = {"key", "label", "choices"}
_OPTIONAL_FIELD_KEYS = {"displayed"}
_NUMBER_FIELD_KEYS = {"key", "label", "type"}


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


def is_number_field(field: dict) -> bool:
    """Whether *field* is a number field rather than a field of choices."""
    return field.get("type") == NUMBER


def is_number(value: Any) -> bool:
    """Whether *value* is a number a number field stores: finite, within :data:`MAX_NUMBER`, and not a boolean."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and abs(value) <= MAX_NUMBER
    )


def validate_schema(raw: Any) -> list[dict]:
    """The normalised form of a submitted schema, or :class:`SubjectError` (400) naming the first fault.

    Strings are stripped. Keys are lower-case identifiers, unique within the schema; choices are unique within
    their field. A field of choices carries ``key``, ``label`` and ``choices`` and may carry ``displayed``, a
    boolean; a number field carries exactly ``key``, ``label`` and ``type: "number"``. A misspelt attribute is
    refused rather than dropped. ``displayed`` is stored only when it is true, so a field without it and the same
    field with ``displayed: false`` compare equal.
    """
    if not isinstance(raw, list):
        raise SubjectError(400, "The subject schema must be a list of fields.")
    if len(raw) > MAX_FIELDS:
        raise SubjectError(400, f"A subject schema has at most {MAX_FIELDS} fields.")
    fields: list[dict] = []
    seen_keys: set[str] = set()
    for index, field in enumerate(raw):
        number = isinstance(field, dict) and "type" in field
        if number and (field["type"] != NUMBER or set(field) != _NUMBER_FIELD_KEYS):
            raise SubjectError(400, f"Field {index} with a type must be a number field with only key, label and type.")
        if not number and (
            not isinstance(field, dict) or not _FIELD_KEYS <= set(field) <= _FIELD_KEYS | _OPTIONAL_FIELD_KEYS
        ):
            raise SubjectError(
                400, f"Field {index} must have the attributes key, label and choices, and may have displayed."
            )
        key = field["key"]
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise SubjectError(
                400, f"Field {index} key must start with a lower-case letter and use only a-z, 0-9 and _."
            )
        if key in seen_keys:
            raise SubjectError(400, f"The key {key} is declared twice.")
        seen_keys.add(key)
        label = _text(field["label"], f"The label of {key}", MAX_LABEL_LENGTH)
        if number:
            fields.append({"key": key, "label": label, "type": NUMBER})
            continue
        choices = field["choices"]
        if not isinstance(choices, list) or not choices:
            raise SubjectError(400, f"The choices of {key} must be a non-empty list.")
        if len(choices) > MAX_CHOICES:
            raise SubjectError(400, f"{key} has more than {MAX_CHOICES} choices.")
        normalised = [_text(choice, f"A choice of {key}", MAX_CHOICE_LENGTH) for choice in choices]
        if len(set(normalised)) != len(normalised):
            raise SubjectError(400, f"The choices of {key} are not unique.")
        displayed = field.get("displayed", False)
        if not isinstance(displayed, bool):
            raise SubjectError(400, f"displayed of {key} must be true or false.")
        fields.append({"key": key, "label": label, "choices": normalised, **({"displayed": True} if displayed else {})})
    return fields


def displayed_keys(schema: list[dict]) -> list[str]:
    """The keys of the fields *schema* marks ``displayed``, in declared order."""
    return [field["key"] for field in schema if field.get("displayed")]


def schema_warnings(schema: list[dict]) -> list[dict[str, str]]:
    """Free-text warnings for a schema's labels and choices, which every reader of the dataset is shown as typed."""
    from epicurrents.text_hygiene import name_warnings

    rows: list[dict[str, str]] = []
    for field in schema:
        rows.extend(name_warnings(**{f"{field['key']}.label": field["label"]}))
        for choice in field.get("choices", ()):
            rows.extend(name_warnings(**{f"{field['key']}.choices": choice}))
    return rows


def number_field_refusal(dataset: Any, schema: list[dict]) -> str | None:
    """Why *schema*, declaring a number field, may not be *dataset*'s, or None when it may (always None without one).

    A number field is refused on a release-gated dataset, a pool and a dataset that has had a release, where the
    fields a reader is shown are part of the class a release is sized on.
    """
    from library.pools import is_pool

    if not any(is_number_field(field) for field in schema):
        return None
    if is_pool(dataset) or hides_undisplayed(dataset):
        return "A number field is declared only on a dataset that is not release-gated, not a pool and has no release."
    return None


def gating_refusal(dataset: Any) -> str | None:
    """Why *dataset*'s subject schema keeps it from being release-gated or made a pool, or None when it does not: a
    number field, which :func:`number_field_refusal` admits only on a dataset that is neither."""
    if any(is_number_field(field) for field in schema_of(dataset)):
        return "A dataset with a number subject field cannot be release-gated or a pool; declare it as bands first."
    return None


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
    """Refuse a schema change to a filling pool, one that drops a field or choice a member still carries, one that
    changes which fields a dataset with a release displays, or a number field where :func:`number_field_refusal`
    refuses one.

    The dataset row must already be locked by the caller, so a submission cannot fill the pool in between.
    """
    from library.pools import is_filling, is_pool

    current = schema_of(dataset)
    if schema == current:
        return
    refusal = number_field_refusal(dataset, schema)
    if refusal:
        raise SubjectError(409, refusal)
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
    if displayed_keys(schema) != displayed_keys(current):
        from library.models import DatasetRelease

        if DatasetRelease.objects.filter(dataset=dataset).exists():
            raise SubjectError(
                409, "The displayed fields of a dataset with a release are fixed: its classes were sized on them."
            )
    fields = {field["key"]: field for field in schema}
    dropped = sorted(
        ((key, value) for key, value in values_in_use(dataset) if not _accepts(fields.get(key), value)),
        key=lambda pair: (pair[0], str(pair[1])),
    )
    if dropped:
        key, value = dropped[0]
        if isinstance(value, str):
            raise SubjectError(409, f"The choice {value!r} of {key} is in use; clear it from the members first.")
        raise SubjectError(409, f"{key} holds numbers on some members; clear them first.")


def _accepts(field: dict | None, value: Any) -> bool:
    """Whether *field* admits *value*: a number for a number field, one of its choices otherwise."""
    if field is None:
        return False
    if is_number_field(field):
        return is_number(value)
    return isinstance(value, str) and value in field["choices"]


def apply_values(schema: list[dict], current: dict, changes: Any) -> dict:
    """*current* with *changes* merged in: a key set to a choice of its field or a number for a number field, or
    cleared with ``null``.

    :class:`SubjectError` (400) for a key the schema does not declare or a value its field does not admit.
    """
    if not isinstance(changes, dict):
        raise SubjectError(400, "Values must be an object of field keys.")
    fields = {field["key"]: field for field in schema}
    merged = dict(current or {})
    for key, value in changes.items():
        if key not in fields:
            raise SubjectError(400, f"The dataset declares no subject field {key}.")
        if value is None:
            merged.pop(key, None)
        elif not _accepts(fields[key], value):
            if is_number_field(fields[key]):
                raise SubjectError(400, f"The value for {key} must be a finite number.")
            raise SubjectError(400, f"The value for {key} is not one of its choices.")
        else:
            merged[key] = value
    return merged


def ensure_values_editable(dataset: Any, item: Any) -> None:
    """Refuse changing the values of a released member of a release-gated dataset: the release was decided on them."""
    if dataset.release_gated and item.release_id is not None:
        raise SubjectError(409, "A released member's subject values are fixed.")


def hides_undisplayed(dataset: Any) -> bool:
    """Whether the dataset shows only its displayed fields to readers who do not manage it: a release-gated dataset,
    and one that has had a release, whatever has become of its gate."""
    from library.models import DatasetRelease

    return bool(dataset.release_gated) or DatasetRelease.objects.filter(dataset=dataset).exists()


def shown_schema(dataset: Any, user: Any) -> list[dict]:
    """The schema *user* is shown: all of it, or its displayed fields to a reader who does not manage a dataset that
    hides the others (:func:`hides_undisplayed`). A caller without an account, *user* ``None``, manages nothing."""
    from library.release import is_dataset_manager

    schema = schema_of(dataset)
    if hides_undisplayed(dataset) and not is_dataset_manager(user, dataset):
        return [field for field in schema if field.get("displayed")]
    return schema


def values_by_object_id(dataset: Any, content_type: Any, object_ids: list, schema: list[dict]) -> dict[str, dict]:
    """The values of the dataset's members of *content_type* among *object_ids*, keyed by ``object_id``, restricted
    to the fields of *schema*, the schema the caller is shown."""
    from library.models import DatasetItemSubject

    keys = {field["key"] for field in schema}
    if not keys:
        return {}
    rows = DatasetItemSubject.objects.filter(
        item__dataset=dataset,
        item__content_type=content_type,
        item__object_id__in=[str(object_id) for object_id in object_ids],
    ).values_list("item__object_id", "values")
    return {object_id: {k: v for k, v in (values or {}).items() if k in keys} for object_id, values in rows}


def subject_class_keys(items: list) -> dict[int, tuple]:
    """The displayed values of each ``DatasetItem`` among *items*, as a tuple in the schema's order, keyed by item pk.

    A field without a value is ``None`` in the tuple, since a reader sees the blank as well. A member of a dataset
    that displays no field gets the empty tuple. This is the part of the equivalence class a reader of the dataset
    browser can see; the release gate and the anonymity report join it to the project's class key.
    """
    from library.models import DatasetItemSubject, DatasetMeta

    dataset_ids = {item.dataset_id for item in items}
    displayed = {
        dataset_id: displayed_keys(schema or [])
        for dataset_id, schema in DatasetMeta.objects.filter(dataset_id__in=dataset_ids).values_list(
            "dataset_id", "subject_schema"
        )
    }
    with_fields = [item.pk for item in items if displayed.get(item.dataset_id)]
    values = dict(DatasetItemSubject.objects.filter(item_id__in=with_fields).values_list("item_id", "values"))
    return {
        item.pk: tuple((values.get(item.pk) or {}).get(key) for key in displayed.get(item.dataset_id, []))
        for item in items
    }


def subject_class_key(item: Any) -> tuple:
    """The displayed values of one ``DatasetItem``, as :func:`subject_class_keys` gives them."""
    return subject_class_keys([item])[item.pk]


def joined_class_key(project_key: Any, subject_key: tuple) -> Any:
    """The class a member is counted in: the project's key alone, or with the displayed values when there are any.

    ``None`` stays ``None``: a member the project leaves unclassified is unclassified whatever it displays. Without
    displayed fields the key is the project's own, so a dataset that displays nothing is counted by that key alone.
    """
    if project_key is None or not subject_key:
        return project_key
    return (project_key, subject_key)
