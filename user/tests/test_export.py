"""Tests for the Art. 15 subject access export.

An export fails silently. It returns a plausible document that is missing
something, and the person who would notice is the subject, who cannot see what
was left out. So the tests here are mostly about absence — that nothing is
unclassified, that credentials are gone, that one subject's export holds no
other subject's rows — rather than about the happy path, which is the part that
would be noticed anyway.
"""

import json

import pytest
from django.contrib.auth import get_user_model

from activity.audit import register_masked_fields, registered_masked_fields
from user.export import (
    RELATION_HANDLING,
    export_user_data,
    export_user_json,
    export_user_text,
    unclassified_relations,
)

pytestmark = pytest.mark.django_db


class TestCoverage:
    def test_every_relation_to_the_user_model_is_classified(self):
        """The property the whole design exists for.

        A model added later that points at a user joins the schema without
        joining the export, and the only symptom is a subject access request
        that quietly returns less than it should.
        """
        assert unclassified_relations() == []

    def test_every_exported_field_exists_on_its_model(self):
        """A wrong field name raises FieldError from .values() — at export time,
        which is while a legal deadline is running rather than at deploy."""
        user_model = get_user_model()
        by_key = {
            f"{rel.related_model._meta.label_lower}:{rel.field.name}": rel for rel in user_model._meta.related_objects
        }
        wrong = []
        for key, handling in RELATION_HANDLING.items():
            relation = by_key.get(key)
            if relation is None or handling.mode != "export":
                continue
            real = {f.name for f in relation.related_model._meta.fields}
            wrong += [f"{key}.{name}" for name in handling.fields if name not in real]
        assert not wrong, f"exported fields that do not exist: {wrong}"

    def test_an_unclassified_relation_is_reported_in_the_payload(self, user, monkeypatch):
        """Belt and braces for the case the check somehow did not stop: the
        payload says so rather than omitting in silence."""
        monkeypatch.setitem(RELATION_HANDLING, "x", RELATION_HANDLING["user.userpreference:user"])
        monkeypatch.delitem(RELATION_HANDLING, "user.userpreference:user")
        payload = export_user_data(user)
        assert "user.userpreference:user" in payload["omitted"]
        assert "bug" in payload["omitted"]["user.userpreference:user"]


class TestCredentialsAreExcluded:
    def test_no_registered_masked_field_appears_anywhere_in_the_output(self, user):
        """Asserted over the rendered JSON, not the field lists, so a credential
        reaching the payload by any route is caught — including a field added to
        a model's export list by someone who did not know it was one."""
        blob = export_user_json(user)
        offenders = []
        for key in RELATION_HANDLING:
            model_label = key.rsplit(":", 1)[0]
            for field in registered_masked_fields(model_label):
                if f'"{field}"' in blob:
                    offenders.append(f"{model_label}.{field}")
        assert not offenders, f"credential fields present in the export: {offenders}"

    def test_the_password_hash_is_never_exported(self, user):
        payload = export_user_data(user)
        assert "password" not in payload["account"]
        assert user.password not in export_user_json(user)

    def test_a_newly_registered_credential_is_excluded_without_touching_this_module(self, user):
        """The reuse that makes the exclusion durable.

        A project registering a masked field gets it dropped from the export
        without knowing the export exists. Asserted through the payload rather
        than the helper, and in both directions, so it cannot pass because the
        field was absent for some other reason.
        """
        from django.apps import apps

        from user.models import UserPreference

        UserPreference.objects.create(user=user, scope="viewer", values={"montage": "longitudinal"})
        model = apps.get_model("user.UserPreference")
        original = registered_masked_fields(model._meta.label_lower)

        before = export_user_data(user)["data"]["user.userpreference:user"]
        assert before and "values" in before[0], "precondition: the field is exported when not masked"

        register_masked_fields(model._meta.label_lower, set(original) | {"values"})
        try:
            after = export_user_data(user)["data"]["user.userpreference:user"]
            assert after and "values" not in after[0], "a registered credential field still reached the export"
            assert "scope" in after[0], "masking one field must not drop the rest"
        finally:
            register_masked_fields(model._meta.label_lower, original)


class TestScoping:
    def test_one_subjects_export_contains_no_other_subjects_rows(self, make_user):
        """The obvious failure of a filter written against the wrong field."""
        from library.models import Collection

        mine, theirs = make_user(), make_user()
        Collection.objects.create(author=mine, name="mine-only")
        Collection.objects.create(author=theirs, name="theirs-only")

        blob = export_user_json(mine)
        assert "mine-only" in blob
        assert "theirs-only" not in blob

    def test_the_payload_is_json_serialisable(self, user):
        """Dates and UUIDs reach the encoder; a TypeError here surfaces only when
        someone actually asks for their data."""
        json.loads(export_user_json(user))

    def test_omitted_relations_carry_a_reason(self, user):
        payload = export_user_data(user)
        assert payload["omitted"], "nothing is omitted, which means the reasons went untested"
        for key, reason in payload["omitted"].items():
            assert reason and len(reason) > 20, f"{key} is omitted without a usable reason"


class TestTextDocument:
    """The text rendering is what a subject actually receives, so it is the form
    whose completeness matters. JSON stays for Art. 20 portability, and both
    render the same classified payload — the risk is a section silently missing
    from one of them, not the two disagreeing about what exists."""

    def test_every_classified_relation_appears_as_a_section(self, user):
        text = export_user_text(user)
        payload = export_user_data(user)
        missing = []
        for key in list(payload["data"]) + list(payload["omitted"]):
            handling = RELATION_HANDLING.get(key)
            title = handling.title if handling and handling.title else None
            if title and title.lower() not in text.lower():
                missing.append(key)
        assert not missing, f"sections absent from the document a subject receives: {missing}"

    def test_it_says_what_was_left_out(self, user):
        """A document that lists only what is held reads as complete whether or
        not it is; the omissions are the part a subject cannot otherwise see."""
        text = export_user_text(user)
        assert "HELD BUT NOT EXPORTED, AND WHY" in text
        assert "WHAT IS NOT INCLUDED" in text

    def test_no_credential_material_reaches_the_document(self, user):
        text = export_user_text(user)
        assert user.password not in text

    def test_an_empty_section_says_so_rather_than_vanishing(self, user):
        """An absent heading and an empty one mean different things to a reader:
        one looks like an oversight, the other is an answer."""
        assert "Nothing held." in export_user_text(user)


class TestManyToManyCoverage:
    """Many-to-many fields declared *on* the user model are invisible to
    ``_meta.related_objects`` — nothing points back, so a walk over reverse
    relations never sees them. ``groups`` is personal data and was missed
    exactly that way; AGENTS.md already records the same table as a blind spot
    for the audit signals, which is what makes it worth a test of its own rather
    than trust that the general walk covers everything."""

    def test_group_membership_is_exported(self, user):
        from django.contrib.auth.models import Group

        user.groups.add(Group.objects.create(name="clinical-reviewers"))
        assert "clinical-reviewers" in export_user_text(user)

    def test_every_forward_m2m_is_classified(self):
        from django.contrib.auth import get_user_model

        from user.export import RELATION_HANDLING as registry

        missing = [f.name for f in get_user_model()._meta.many_to_many if f"user:{f.name}" not in registry]
        assert not missing, f"many-to-many fields on the user model not classified: {missing}"


class TestDeclaredFieldsActuallyArrive:
    """A field can be declared for export and removed by the credential filter,
    leaving no column and no note — a document that reads as complete and is not.

    That happened: `original_name` was listed for both recordings and media and
    silently dropped, because `register_masked_fields` carries two different
    things. Credentials nobody may see, and author-private values hidden from
    grantees *because* they belong to the author — who is the subject here.
    """

    def test_no_declared_field_is_silently_dropped(self):
        from django.apps import apps
        from django.contrib.auth import get_user_model

        from user.export import _safe_fields

        user_model = get_user_model()
        by_key = {f"{r.related_model._meta.label_lower}:{r.field.name}": r for r in user_model._meta.related_objects}
        dropped = []
        for key, handling in RELATION_HANDLING.items():
            relation = by_key.get(key)
            if relation is None or handling.mode != "export":
                continue
            model = apps.get_model(key.rsplit(":", 1)[0])
            survived = set(_safe_fields(model, handling.fields, handling.include_masked))
            dropped += [f"{key}.{name}" for name in handling.fields if name not in survived]
        assert not dropped, (
            f"declared for export but filtered out without a trace: {dropped} — "
            "add to include_masked if the subject may see it, or stop declaring it"
        )

    def test_the_uploaded_filename_reaches_the_subject(self, user, tmp_path):
        """The concrete case, stated on its own: an author asking what is held
        about them is entitled to the name of the file they uploaded."""
        from recordings.models import Recording

        Recording.objects.create(
            author=user,
            original_name="patient-visit-3.edf",
            display_name="ABC123",
            stored_name="ABC123.edf",
            file_extension=".edf",
            file_size=1,
            file_hash="h",
            content_hash="c",
        )
        assert "patient-visit-3.edf" in export_user_text(user)
