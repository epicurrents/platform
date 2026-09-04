"""Tests for the template's ``AppConfig.ready()`` registrations — the hook half of the extension contract.

``RecordingNote.notes`` and ``site_id`` carry data about the recording's subject, not a platform
account, so ``apps.py`` registers them as masked fields: they must never reach the permanent audit
trail in the clear. The export-relation registration is the mirror obligation — the reviewer's
rows appear in their Art. 15 export, with the masked fields dropped.
"""

import pytest

from activity.audit import registered_masked_fields, serialize_instance
from projects.example.models import RecordingNote


@pytest.mark.django_db
class TestMaskedFieldRegistration:
    def test_ready_registered_the_masked_fields(self):
        assert registered_masked_fields("example.recordingnote") == frozenset({"notes", "site_id"})

    def test_audit_payload_masks_the_clinical_fields(self, recording):
        note = RecordingNote.objects.create(
            recording=recording,
            site_id="SITE-SECRET",
            notes="Pt. reports aura, ref. Dr Hansen",
        )
        payload = serialize_instance(note)
        blob = str(payload)
        assert "Hansen" not in blob
        assert "SITE-SECRET" not in blob
        # The masked marker replaces the value, so a reviewer can still see
        # that the field changed without seeing what it holds.
        assert str(payload["notes"]).startswith("<masked:")
        assert str(payload["site_id"]).startswith("<masked:")

    def test_audit_trail_rows_carry_no_clinical_text(self, auth_client, recording, note_url):
        import json

        from activity.models import ObjectChangeLog

        c, _user = auth_client
        secret = "Pt. reports aura, ref. Dr Hansen"
        resp = c.put(note_url, json.dumps({"notes": secret}), content_type="application/json")
        assert resp.status_code == 200, resp.content
        rows = ObjectChangeLog.objects.all()
        assert rows.exists()
        combined = "".join(str(row.before_state) + str(row.changes) + str(row.extra_payload) for row in rows)
        assert "Hansen" not in combined


@pytest.mark.django_db
class TestExportRegistration:
    def test_ready_classified_the_reviewer_relation(self):
        from user.export import RELATION_HANDLING

        assert "example.recordingnote:reviewed_by" in RELATION_HANDLING

    def test_no_example_relation_is_left_unclassified(self):
        # The check this mirrors runs at manage.py check; asserting here keeps
        # the template honest when it grows a new user FK.
        from user.export import unclassified_relations

        assert [key for key in unclassified_relations() if key.startswith("example.")] == []
