"""Django system checks for the audit-trail registries.

``register_subject_pii`` and ``register_masked_fields`` take plain strings — a
model label, field names — and store them without looking at anything. Every way
of getting one wrong fails silently, and every failure ends in personal data
sitting in the permanent audit trail that an Art. 17 request cannot reach:

- a wrong ``model_label`` resolves to no ContentType, and ``erase_subject``
  skips the spec (``activity/erasure.py``, the ``content_type is None`` branch);
- a label that is merely the wrong *case* does the same, and is nastier because
  it looks right: ``django.apps.get_model`` resolves it, so the model plainly
  exists, while the ContentType columns it is looked up against are lowercase;
- a wrong ``owner_field`` makes the ``before_state__<field>`` lookup match no
  rows, so nothing is scrubbed;
- a wrong entry in ``pii_fields`` is never found in a payload, so that one field
  is never scrubbed;
- an empty ``pii_fields`` scrubs nothing at all while looking like a completed
  registration.

None of the three raises, and all three leave ``erase_subject``'s summary
reporting zero rows for the model — which is exactly what a legitimately clean
run reports. The misconfiguration is invisible from the operator's dry run as
well as from the code.

A system check rather than a test, for two reasons. It runs after every app's
``ready()``, so it sees whatever project or plugins the deployment has active,
where a test under the platform test settings sees only the core registrations
and could never cover a project's. And it runs on ``manage.py check``, which
Django performs before ``runserver`` and most management commands, so a typo
stops a deployment rather than waiting for the erasure request that discovers it.

A spec registered with ``historical=True`` covers audit rows of a model that
was dropped from the schema — the scrub needs only the surviving ContentType
row, so the field checks have nothing to validate against and are skipped;
what is checked instead is that the flag is truthful (the model really is
gone) plus the casing and non-empty-fields rules, which apply either way.

Deliberately *not* checked: a field registered for both masking and subject
erasure. That reads like dead configuration — a masked field reaches the payload
as a digest sentinel, never the real value — but ``user.user`` registers
``password`` in both on purpose, and the scrub does still replace the mask
sentinel with the erasure sentinel. Warning about it would fire on a deliberate
core registration, which is how a check earns being ignored.
"""

from django.apps import apps as django_apps
from django.core.checks import Error, Tags, register

from activity.erasure import registered_subject_pii


def _serialized_field_names(model) -> set[str]:
    """The payload keys ``serialize_instance`` will produce for *model*.

    Attnames, not field names: ``serialize_instance`` walks
    ``_meta.concrete_fields`` and reads ``field.attname``, so a foreign key
    appears as ``user_id`` rather than ``user``. Registrations name the payload
    key, so that is what has to match.
    """
    return {field.attname for field in model._meta.concrete_fields}


@register(Tags.models)
def check_subject_pii_registrations(app_configs, **kwargs):
    """Verify every ``register_subject_pii`` spec against its real model."""
    errors = []
    for label, spec in sorted(registered_subject_pii().items()):
        app_label, _, model_name = label.partition(".")
        try:
            model = django_apps.get_model(app_label, model_name)
        except (LookupError, ValueError):
            model = None

        if spec.historical:
            # A historical spec covers audit rows of a model that has been
            # dropped from the schema; the scrub needs only the surviving
            # ContentType row, so there is no model to validate fields
            # against. What CAN still be validated: the label casing and a
            # non-empty field set (below), and that the flag is truthful —
            # a historical registration whose model still exists is either a
            # stale flag or a label typo shadowing a live model, and both
            # deserve the same hard stop a wrong label gets.
            if model is not None:
                errors.append(
                    Error(
                        f"register_subject_pii({label!r}) is marked historical, but the model exists.",
                        hint=(
                            "historical=True skips field validation because the model is expected to be "
                            "gone from the schema. While it exists, drop the flag so the registration is "
                            "checked against the real fields."
                        ),
                        id="activity.E006",
                    )
                )
        elif model is None:
            errors.append(
                Error(
                    f"register_subject_pii({label!r}) names a model that does not exist.",
                    hint=(
                        "Use the lowercase 'app_label.model_name' pair. erase_subject skips a spec whose "
                        "label resolves to no ContentType, so this registration currently scrubs nothing. "
                        "If the model was deliberately dropped from the schema and old audit rows still "
                        "need scrubbing, register with historical=True."
                    ),
                    id="activity.E001",
                )
            )
            continue

        if label != label.lower():
            errors.append(
                Error(
                    f"register_subject_pii({label!r}) is not lowercase.",
                    hint=(
                        "django.apps.get_model resolves a label case-insensitively, but erase_subject looks "
                        "the model up with ContentType.objects.filter(app_label=..., model=...), and "
                        "ContentType stores both lowercase. A capitalised label therefore resolves here and "
                        f"matches nothing there. Use {label.lower()!r}."
                    ),
                    id="activity.E004",
                )
            )

        if not spec.pii_fields:
            errors.append(
                Error(
                    f"register_subject_pii({label!r}) lists no pii_fields.",
                    hint=(
                        "The registration scrubs nothing, and erase_subject reports zero rows for the model "
                        "exactly as it would for a clean run. Name the payload keys carrying the subject's "
                        "personal data, or drop the registration."
                    ),
                    id="activity.E005",
                )
            )

        if spec.historical:
            continue

        available = _serialized_field_names(model)

        if spec.owner_field is not None and spec.owner_field not in available:
            errors.append(
                Error(
                    f"register_subject_pii({label!r}) has owner_field={spec.owner_field!r}, "
                    f"which is not a field on {model.__name__}.",
                    hint=(
                        "owner_field is the serialized attname linking a row to the subject, so a foreign "
                        f"key is named with its _id suffix. Available: {sorted(available)}."
                    ),
                    id="activity.E002",
                )
            )

        for field in sorted(spec.pii_fields - available):
            errors.append(
                Error(
                    f"register_subject_pii({label!r}) lists pii_field {field!r}, "
                    f"which is not a field on {model.__name__}.",
                    hint=(
                        "A payload key that never appears is never scrubbed, and the erasure summary "
                        f"reports zero either way. Available: {sorted(available)}."
                    ),
                    id="activity.E003",
                )
            )

    return errors
