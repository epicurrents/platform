"""Django system checks for the subject-access export registry.

``user.export.RELATION_HANDLING`` decides what an Art. 15 request returns, and
every way of getting it wrong is quiet:

- a relation nobody classified is data the export never looks at, so the subject
  receives less than they are entitled to and the payload says only that
  something was unclassified;
- a field name that does not exist on the model raises ``FieldError`` from
  ``.values()`` — at export time, which is to say while a legal deadline runs,
  rather than at deploy time;
- a registration naming a model that is not installed is inert, and inert looks
  exactly like "this deployment holds none of that".

None of the three is visible from reading the registry, because the registry is
strings and the truth is the schema. So they are checked against the real models
at ``manage.py check``, which Django runs before ``runserver`` and ``migrate``.

The same reasoning, and the same shape, as ``activity/checks.py`` — which exists
because the erasure registry has the mirror-image failure. A check rather than a
test for the reason given there too: it runs after every app's ``ready()``, so it
sees whatever project and plugins the deployment actually has, and the set of
relations pointing at a user depends entirely on that.
"""

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.checks import Error, Tags, Warning, register

from activity.audit import registered_masked_fields
from user.export import RELATION_HANDLING


@register(Tags.models)
def check_export_relation_coverage(app_configs, **kwargs):
    """Every relation to the user model is classified, and classified correctly."""
    issues: list = []
    user_model = get_user_model()

    known = {
        f"{relation.related_model._meta.label_lower}:{relation.field.name}": relation
        for relation in user_model._meta.related_objects
    }
    # Many-to-many fields declared on the user model point outwards, so nothing
    # in related_objects represents them and the walk above cannot see them.
    # `groups` is personal data; it was missed exactly this way once already.
    m2m = {f"user:{field.name}": field for field in user_model._meta.many_to_many}

    # 1. Anything pointing at a user that no one has classified.
    for key in sorted((known.keys() | m2m.keys()) - RELATION_HANDLING.keys()):
        issues.append(
            Error(
                f"{key} points at the user model but is not classified for subject export.",
                hint=(
                    "Call user.export.register_export_relation(...) from the owning app's "
                    "AppConfig.ready(), giving either fields=(...) to export them or "
                    "omit_reason='...' to leave them out. An unclassified relation is "
                    "personal data an Art. 15 request would not return."
                ),
                obj=key,
                id="user.E001",
            )
        )

    # 2. Registrations that name something the schema does not have. Inert rather
    #    than loud, which is why they are worth a check at all.
    for key, handling in sorted(RELATION_HANDLING.items()):
        relation = known.get(key)
        if relation is None and key in m2m:
            model = m2m[key].related_model
            real = {field.name for field in model._meta.fields}
            if handling.mode == "export":
                for name in handling.fields:
                    if name not in real:
                        issues.append(
                            Error(
                                f"{key} exports field {name!r}, which {model._meta.label} does not have.",
                                hint="The export would raise FieldError while a request is running.",
                                obj=key,
                                id="user.E002",
                            )
                        )
            continue
        if relation is None:
            model_label = key.rsplit(":", 1)[0]
            if not django_apps.is_installed(model_label.split(".")[0]):
                # A project that is not the active one; its registration cannot
                # fire and its absence is normal.
                continue
            issues.append(
                Warning(
                    f"{key} is registered for subject export but nothing by that name points at the user model.",
                    hint="A stale registration exports nothing and reads as though it does.",
                    obj=key,
                    id="user.W001",
                )
            )
            continue
        if handling.mode != "export":
            continue
        model = relation.related_model
        real = {field.name for field in model._meta.fields}
        for name in handling.fields:
            if name not in real:
                issues.append(
                    Error(
                        f"{key} exports field {name!r}, which {model._meta.label} does not have.",
                        hint="The export would raise FieldError while a subject access request is running.",
                        obj=key,
                        id="user.E002",
                    )
                )
        # A field declared for export that the credential filter removes anyway.
        # The document simply lacks the column, indistinguishable from an
        # oversight — which is how original_name went missing from two of the
        # largest sections while being explicitly listed.
        masked = registered_masked_fields(model._meta.label_lower) - set(handling.include_masked)
        for name in sorted(set(handling.fields) & masked):
            issues.append(
                Error(
                    f"{key} declares field {name!r} for export, but it is registered for audit "
                    "masking and would be dropped without appearing anywhere in the document.",
                    hint=(
                        "If the subject is entitled to see it — an author-private value rather "
                        "than a credential — add it to include_masked=(...). If not, remove it "
                        "from the exported fields so the intent is visible."
                    ),
                    obj=key,
                    id="user.E004",
                )
            )
        # include_masked naming something that is not masked is dead config that
        # reads as a considered decision.
        for name in sorted(set(handling.include_masked) - registered_masked_fields(model._meta.label_lower)):
            issues.append(
                Warning(
                    f"{key} lists {name!r} in include_masked, but nothing registers it for audit masking.",
                    hint="Remove it; the exemption does nothing and implies a decision that was not needed.",
                    obj=key,
                    id="user.W002",
                )
            )
        if not handling.fields:
            issues.append(
                Error(
                    f"{key} is registered for export with no fields.",
                    hint="Give fields=(...), or omit_reason='...' if the rows should be left out.",
                    obj=key,
                    id="user.E003",
                )
            )

    return issues
