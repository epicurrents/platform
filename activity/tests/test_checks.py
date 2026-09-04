"""Contract tests for the erasure-registry system checks.

The checks exist because ``register_subject_pii`` accepts any strings and every
wrong one fails silently, ending in personal data that an Art. 17 request cannot
reach. So the tests that matter are the ones proving each wrong registration is
*caught* — a check that only ever passes is worth nothing.

The wiring is asserted too, through ``django.core.checks.run_checks``. A check
function that is correct but never imported is the same as no check at all, and
the import lives in ``ActivityConfig.ready`` where nothing else would notice its
removal.
"""

import pytest
from django.core.checks import run_checks

from activity import erasure
from activity.checks import check_subject_pii_registrations


@pytest.fixture
def registry():
    """Snapshot and restore the process-wide registry around a test."""
    saved = dict(erasure._SUBJECT_PII)
    yield erasure
    erasure._SUBJECT_PII.clear()
    erasure._SUBJECT_PII.update(saved)


def _ids(errors):
    return sorted(error.id for error in errors)


class TestRealRegistrationsPass:
    def test_the_shipped_registrations_are_valid(self):
        """Guards the check itself. If this went red the registrations would be
        wrong, or the check would be — and the next test says which."""
        assert check_subject_pii_registrations(None) == []

    def test_the_user_model_registration_with_no_owner_field_is_accepted(self):
        """`user.user` registers owner_field=None because the link is the row's
        object_id, not a payload key. That must not read as a missing field."""
        spec = erasure.registered_subject_pii()["user.user"]
        assert spec.owner_field is None
        assert check_subject_pii_registrations(None) == []


class TestBadRegistrationsAreCaught:
    def test_a_model_that_does_not_exist(self, registry):
        registry.register_subject_pii("nosuchapp.nosuchmodel", owner_field="user_id", pii_fields={"x"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E001"]

    def test_a_malformed_label(self, registry):
        registry.register_subject_pii("not-a-label", owner_field=None, pii_fields=set())
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E001"]

    def test_an_owner_field_that_is_not_a_field(self, registry):
        registry.register_subject_pii("user.userpreference", owner_field="usr_id", pii_fields={"values"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E002"]

    def test_a_foreign_key_named_without_its_id_suffix(self, registry):
        """The realistic mistake. Registrations name the serialized attname, and
        serialize_instance emits `user_id` for the FK — `user` matches nothing,
        so the scrub finds no rows and reports zero exactly like a clean run."""
        registry.register_subject_pii("user.userpreference", owner_field="user", pii_fields={"values"})
        errors = check_subject_pii_registrations(None)
        assert _ids(errors) == ["activity.E002"]
        assert "user_id" in errors[0].hint

    def test_a_label_that_resolves_but_is_the_wrong_case(self, registry):
        """The nastiest of the family, because the model plainly exists.
        ``apps.get_model`` is case-insensitive, so the label looks fine; the
        ContentType columns ``erase_subject`` queries are lowercase, so it
        matches nothing there. Verified against a real ContentType row."""
        registry.register_subject_pii("user.UserPreference", owner_field="user_id", pii_fields={"values"})
        errors = check_subject_pii_registrations(None)
        assert _ids(errors) == ["activity.E004"]
        assert "user.userpreference" in errors[0].hint

    def test_a_registration_with_no_fields_to_scrub(self, registry):
        registry.register_subject_pii("user.userpreference", owner_field="user_id", pii_fields=set())
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E005"]

    def test_a_pii_field_that_is_not_a_field(self, registry):
        registry.register_subject_pii("user.userpreference", owner_field="user_id", pii_fields={"valeus"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E003"]

    def test_each_bad_pii_field_is_reported_separately(self, registry):
        registry.register_subject_pii("user.userpreference", owner_field="user_id", pii_fields={"a", "b"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E003", "activity.E003"]

    def test_a_bad_owner_field_and_a_bad_pii_field_both_surface(self, registry):
        """One run reports everything wrong with a registration, so fixing them
        is not a sequence of re-runs."""
        registry.register_subject_pii("user.userpreference", owner_field="nope", pii_fields={"alsonope"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E002", "activity.E003"]

    def test_a_bad_model_reports_once_and_stops(self, registry):
        """Field checks against a model that could not be resolved would be
        noise on top of the one error that matters."""
        registry.register_subject_pii("nosuchapp.nosuchmodel", owner_field="nope", pii_fields={"a", "b"})
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E001"]


class TestHistoricalRegistrations:
    """historical=True keeps a dropped model's audit rows scrubable without
    tripping the model-existence check — and flips the posture: while the model
    still exists, the flag is the error."""

    def test_a_historical_registration_for_a_gone_model_passes(self, registry):
        registry.register_subject_pii(
            "nosuchapp.retiredmodel", owner_field="user_id", pii_fields={"role"}, historical=True
        )
        assert check_subject_pii_registrations(None) == []

    def test_a_historical_registration_for_a_live_model_is_caught(self, registry):
        registry.register_subject_pii(
            "user.userpreference", owner_field="user_id", pii_fields={"values"}, historical=True
        )
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E006"]

    def test_field_names_are_not_validated_for_a_gone_model(self, registry):
        """There is nothing to validate against; a typo here is caught only by
        the non-empty rule, which still applies."""
        registry.register_subject_pii(
            "nosuchapp.retiredmodel", owner_field="whatever", pii_fields={"anything"}, historical=True
        )
        assert check_subject_pii_registrations(None) == []

    def test_the_empty_fields_rule_still_applies(self, registry):
        registry.register_subject_pii(
            "nosuchapp.retiredmodel", owner_field="user_id", pii_fields=set(), historical=True
        )
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E005"]

    def test_the_lowercase_rule_still_applies(self, registry):
        registry.register_subject_pii(
            "nosuchapp.RetiredModel", owner_field="user_id", pii_fields={"role"}, historical=True
        )
        assert _ids(check_subject_pii_registrations(None)) == ["activity.E004"]


class TestTheCheckIsWiredIn:
    def test_django_runs_it(self, registry):
        """Reached through run_checks rather than by calling the function, so the
        @register decorator and the return shape are covered too."""
        registry.register_subject_pii("nosuchapp.nosuchmodel", owner_field=None, pii_fields=set())
        assert "activity.E001" in {getattr(m, "id", None) for m in run_checks()}

    def test_a_clean_registry_produces_no_findings_from_this_check(self):
        assert "activity.E001" not in {getattr(m, "id", None) for m in run_checks()}

    def test_the_app_config_imports_the_checks_module(self):
        """The one assertion that cannot be made at runtime from in here.

        ``@register`` fires on import, and this test module imports
        ``activity.checks`` to reach the function under test — so the check is
        registered whether or not ``ActivityConfig.ready`` imports it, and
        ``run_checks`` above passes either way. Deleting that one line would
        disable the check in every real process while leaving this file green;
        confirmed by mutation. Hence a source assertion on the line itself.
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "apps.py"
        tree = ast.parse(source.read_text())
        ready = next(
            node
            for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == "ActivityConfig"
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "ready"
        )
        imported = {alias.name for node in ast.walk(ready) if isinstance(node, ast.ImportFrom) for alias in node.names}
        assert "checks" in imported, (
            "ActivityConfig.ready no longer imports activity.checks, so the erasure-registry "
            "system checks never register and a bad registration passes manage.py check silently"
        )
