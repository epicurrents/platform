"""Contract tests for the contextual-assessment record on grants (``epicurrents.assessment``).

The record is the sharer's: written and read by the row's giver, the object's author and superusers,
served as ``None`` to everyone else, exported under the grants a subject gave and scrubbed with their
account. ``grant_assessments`` is the reassessment side: it names the grants whose assessment is older
than the sweep's cut-off or older than the pass that last wrote a recording they cover.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from io import StringIO

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.utils import timezone
from model_bakery import baker

from activity.models import Activity, ObjectChangeLog
from conftest import patch_json, post_json
from epicurrents.assessment import ASSESSMENT_REFERENCE_MAX_LENGTH, assessment_visible, normalise_assessment
from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem
from recordings.models import Recording

pytestmark = pytest.mark.django_db

HASH = "ABCDEF1234567890ABCDEF1234567890"
TODAY = timezone.localdate()
YESTERDAY = TODAY - timedelta(days=1)


def _recording(author, **kwargs):
    defaults = {
        "author": author,
        "stored_name": f"{HASH}.edf",
        "file_extension": ".edf",
        "file_size": 1,
        "status": Recording.Status.READY,
    }
    defaults.update(kwargs)
    return baker.make(Recording, **defaults)


def _grant(obj, giver, *, target=None, group=None, token=None, **fields):
    ct = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        access_target_group=group,
        public_share_token=token,
        can_read=True,
        **fields,
    )


def _assessment_of(pk):
    right = AccessRight.objects.get(pk=pk)
    return right.assessment_reference, right.assessment_date


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_the_model_refuses_half_a_record(self, user, make_user):
        recording = _recording(user)
        with pytest.raises(IntegrityError):
            _grant(recording, user, target=make_user(), assessment_reference="DPIA-1")

    def test_the_model_refuses_a_date_without_a_reference(self, user, make_user):
        recording = _recording(user)
        with pytest.raises(IntegrityError):
            _grant(recording, user, target=make_user(), assessment_date=TODAY)

    def test_a_full_record_and_an_empty_one_are_both_stored(self, user, make_user):
        recording = _recording(user)
        assessed = _grant(recording, user, target=make_user(), assessment_reference="DPIA-1", assessment_date=TODAY)
        plain = _grant(recording, user, target=make_user())
        assert _assessment_of(assessed.pk) == ("DPIA-1", TODAY)
        assert _assessment_of(plain.pk) == ("", None)

    @pytest.mark.parametrize(
        "reference, when",
        [("DPIA-1", None), ("", TODAY), ("   ", TODAY), ("x" * (ASSESSMENT_REFERENCE_MAX_LENGTH + 1), TODAY)],
    )
    def test_normalisation_refuses_half_records_and_overlong_references(self, reference, when):
        with pytest.raises(ValueError):
            normalise_assessment(reference, when)

    def test_normalisation_refuses_a_future_date(self):
        with pytest.raises(ValueError, match="future"):
            normalise_assessment("DPIA-1", TODAY + timedelta(days=1))

    def test_normalisation_strips_and_accepts_an_empty_pair(self):
        assert normalise_assessment("  DPIA-1 ", TODAY) == ("DPIA-1", TODAY)
        assert normalise_assessment(None, None) == ("", None)

    def test_visibility_is_the_giver_the_author_and_superusers(self, user, make_user, make_superuser):
        recording = _recording(user)
        sharer = make_user()
        grantee = make_user()
        right = _grant(recording, sharer, target=grantee)
        assert assessment_visible(right, sharer)
        assert assessment_visible(right, user, recording)
        assert assessment_visible(right, make_superuser())
        assert not assessment_visible(right, grantee, recording)
        assert not assessment_visible(right, user)
        assert not assessment_visible(right, None, recording)


# ---------------------------------------------------------------------------
# Dataset grants
# ---------------------------------------------------------------------------


def _dataset_url(dataset, right_id=None):
    base = f"/api/v1/library/datasets/{dataset.pk}/access/"
    return base if right_id is None else f"{base}{right_id}/"


class TestDatasetGrants:
    def test_a_grant_may_carry_the_assessment(self, auth_client, make_user):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        resp = post_json(
            client,
            _dataset_url(dataset),
            {
                "access_target_id": make_user().pk,
                "assessment_reference": "https://dms.example/dpia/7",
                "assessment_date": YESTERDAY.isoformat(),
            },
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["assessment_reference"] == "https://dms.example/dpia/7"
        assert body["assessment_date"] == YESTERDAY.isoformat()
        assert _assessment_of(body["id"]) == ("https://dms.example/dpia/7", YESTERDAY)

    def test_a_half_record_on_creation_is_400_and_creates_nothing(self, auth_client, make_user):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        resp = post_json(
            client, _dataset_url(dataset), {"access_target_id": make_user().pk, "assessment_reference": "DPIA-1"}
        )
        assert resp.status_code == 400
        assert not AccessRight.objects.filter(object_id=str(dataset.pk)).exists()

    def test_the_author_records_updates_and_clears_it(self, auth_client, make_user):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        right = _grant(dataset, author, target=make_user())

        resp = patch_json(
            client, _dataset_url(dataset, right.pk), {"assessment_reference": "DPIA-1", "assessment_date": "2026-03-01"}
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["assessment_reference"] == "DPIA-1"
        assert _assessment_of(right.pk) == ("DPIA-1", date(2026, 3, 1))

        resp = patch_json(
            client, _dataset_url(dataset, right.pk), {"assessment_reference": "DPIA-1", "assessment_date": "2026-09-01"}
        )
        assert resp.status_code == 200
        assert _assessment_of(right.pk) == ("DPIA-1", date(2026, 9, 1))

        resp = patch_json(client, _dataset_url(dataset, right.pk), {})
        assert resp.status_code == 200
        assert resp.json()["assessment_reference"] is None
        assert _assessment_of(right.pk) == ("", None)

    @pytest.mark.parametrize(
        "payload",
        [
            {"assessment_reference": "DPIA-1"},
            {"assessment_date": "2026-03-01"},
            {"assessment_reference": "DPIA-1", "assessment_date": (TODAY + timedelta(days=1)).isoformat()},
        ],
    )
    def test_a_bad_record_is_400_and_the_row_is_untouched(self, auth_client, make_user, payload):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        right = _grant(dataset, author, target=make_user(), assessment_reference="KEEP", assessment_date=YESTERDAY)
        resp = patch_json(client, _dataset_url(dataset, right.pk), payload)
        assert resp.status_code == 400
        assert _assessment_of(right.pk) == ("KEEP", YESTERDAY)

    def test_the_write_is_audited_without_the_reference(self, auth_client, make_user):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        right = _grant(dataset, author, target=make_user())
        patch_json(
            client,
            _dataset_url(dataset, right.pk),
            {"assessment_reference": "SECRET-REF", "assessment_date": "2026-03-01"},
        )
        activity = Activity.objects.filter(verb="library.dataset.access.assess").latest("created_at")
        assert activity.target_object_id == str(right.pk)
        assert activity.metadata["assessed"] is True
        assert "SECRET-REF" not in json.dumps(activity.metadata)

    def test_a_write_grantee_who_lists_rights_sees_no_assessment(self, client, make_user):
        author = make_user()
        editor = make_user()
        dataset = baker.make(Dataset, author=author)
        _grant(dataset, author, target=editor, can_write=True)
        assessed = _grant(dataset, author, target=make_user(), assessment_reference="DPIA-1", assessment_date=TODAY)

        client.force_login(editor)
        resp = client.get(_dataset_url(dataset))
        assert resp.status_code == 200
        rows = {row["id"]: row for row in resp.json()}
        assert rows[assessed.pk]["assessment_reference"] is None
        assert rows[assessed.pk]["assessment_date"] is None

        client.force_login(author)
        rows = {row["id"]: row for row in client.get(_dataset_url(dataset)).json()}
        assert rows[assessed.pk]["assessment_reference"] == "DPIA-1"

    def test_a_delegated_sharer_sees_and_edits_only_their_own_grants(self, client, make_user):
        author = make_user()
        sharer = make_user()
        dataset = baker.make(Dataset, author=author)
        # Listing needs write on the dataset; managing access needs share.
        _grant(dataset, author, target=sharer, can_write=True, can_share=True)
        theirs = _grant(dataset, sharer, target=make_user(), assessment_reference="MINE", assessment_date=TODAY)
        authors = _grant(dataset, author, target=make_user(), assessment_reference="NOT-MINE", assessment_date=TODAY)

        client.force_login(sharer)
        rows = {row["id"]: row for row in client.get(_dataset_url(dataset)).json()}
        assert rows[theirs.pk]["assessment_reference"] == "MINE"
        assert rows[authors.pk]["assessment_reference"] is None

        resp = patch_json(client, _dataset_url(dataset, authors.pk), {})
        assert resp.status_code == 403
        assert _assessment_of(authors.pk) == ("NOT-MINE", TODAY)
        resp = patch_json(client, _dataset_url(dataset, theirs.pk), {})
        assert resp.status_code == 200
        assert _assessment_of(theirs.pk) == ("", None)

    def test_a_refusal_is_security_logged(self, client, make_user, caplog):
        import logging

        author = make_user()
        sharer = make_user()
        dataset = baker.make(Dataset, author=author)
        _grant(dataset, author, target=sharer, can_write=True, can_share=True)
        authors = _grant(dataset, author, target=make_user())
        client.force_login(sharer)
        with caplog.at_level(logging.WARNING, logger="epicurrents.security"):
            assert patch_json(client, _dataset_url(dataset, authors.pk), {}).status_code == 403
        events = [r for r in caplog.records if getattr(r, "security_event_type", "") == "permission.denied"]
        assert len(events) == 1
        assert events[0].permission == "assess"
        assert events[0].actor_id == sharer.pk

    def test_a_plain_grantee_cannot_write_one(self, client, make_user):
        author = make_user()
        reader = make_user()
        dataset = baker.make(Dataset, author=author)
        right = _grant(dataset, author, target=reader)
        client.force_login(reader)
        resp = patch_json(
            client, _dataset_url(dataset, right.pk), {"assessment_reference": "X", "assessment_date": "2026-03-01"}
        )
        assert resp.status_code == 403

    def test_an_unknown_right_is_404(self, auth_client):
        client, author = auth_client
        dataset = baker.make(Dataset, author=author)
        assert patch_json(client, _dataset_url(dataset, 999999), {}).status_code == 404


# ---------------------------------------------------------------------------
# Recording grants
# ---------------------------------------------------------------------------


def _recording_url(right_id=None):
    base = f"/recordings/api/v1/{HASH}/access/"
    return base if right_id is None else f"{base}{right_id}/"


class TestRecordingGrants:
    def test_the_author_records_and_the_listing_shows_it(self, auth_client, make_user):
        client, author = auth_client
        recording = _recording(author)
        right = _grant(recording, author, target=make_user())
        resp = patch_json(
            client, _recording_url(right.pk), {"assessment_reference": "DPIA-9", "assessment_date": "2026-03-01"}
        )
        assert resp.status_code == 200, resp.content
        assert _assessment_of(right.pk) == ("DPIA-9", date(2026, 3, 1))
        rows = {row["id"]: row for row in client.get(_recording_url()).json()}
        assert rows[right.pk]["assessment_reference"] == "DPIA-9"
        activity = Activity.objects.filter(verb="recordings.access.assess").latest("created_at")
        assert activity.target_object_id == str(right.pk)
        assert activity.metadata == {"assessed": True}

    def test_a_share_holder_sees_none_on_the_authors_grants_and_cannot_edit_them(self, client, make_user):
        author = make_user()
        sharer = make_user()
        recording = _recording(author)
        _grant(recording, author, target=author, can_write=True, can_share=True)
        _grant(recording, author, target=sharer, can_share=True)
        assessed = _grant(recording, author, target=make_user(), assessment_reference="DPIA-9", assessment_date=TODAY)
        client.force_login(sharer)
        rows = {row["id"]: row for row in client.get(_recording_url()).json()}
        assert rows[assessed.pk]["assessment_reference"] is None
        assert patch_json(client, _recording_url(assessed.pk), {}).status_code == 403

    def test_a_half_record_is_400(self, auth_client, make_user):
        client, author = auth_client
        recording = _recording(author)
        right = _grant(recording, author, target=make_user())
        assert patch_json(client, _recording_url(right.pk), {"assessment_reference": "DPIA-9"}).status_code == 400


# ---------------------------------------------------------------------------
# Federation grants
# ---------------------------------------------------------------------------

FED = "/api/v1/federation"


def _peer():
    from federation.auth import generate_keypair
    from federation.models import FederatedPeer

    pub, _ = generate_keypair()
    return baker.make(FederatedPeer, url="https://peer.example.com", public_key=pub, is_trusted=True)


class TestFederationGrants:
    def _create(self, client, recording, peer, **extra):
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        return post_json(
            client,
            f"{FED}/grants/",
            {"federated_peer_id": peer.pk, "content_type_id": ct.pk, "object_id": str(recording.pk), **extra},
        )

    def test_create_carries_the_assessment_and_the_listing_shows_it(self, auth_client):
        client, author = auth_client
        resp = self._create(
            client, _recording(author), _peer(), assessment_reference="PEER-DPIA", assessment_date="2026-03-01"
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["assessment_reference"] == "PEER-DPIA"
        listed = client.get(f"{FED}/grants/").json()
        assert listed[0]["assessment_reference"] == "PEER-DPIA"
        assert listed[0]["assessment_date"] == "2026-03-01"

    def test_create_without_one_serves_none(self, auth_client):
        client, author = auth_client
        resp = self._create(client, _recording(author), _peer())
        assert resp.status_code == 200
        assert resp.json()["assessment_reference"] is None
        assert resp.json()["assessment_date"] is None

    def test_create_with_half_a_record_is_400(self, auth_client):
        client, author = auth_client
        assert self._create(client, _recording(author), _peer(), assessment_reference="PEER-DPIA").status_code == 400

    def test_patching_the_assessment_leaves_the_expiry_alone(self, auth_client):
        client, author = auth_client
        expires = timezone.now() + timedelta(days=30)
        grant_id = self._create(client, _recording(author), _peer(), expires_at=expires.isoformat()).json()["id"]
        resp = patch_json(
            client, f"{FED}/grants/{grant_id}/", {"assessment_reference": "PEER-DPIA", "assessment_date": "2026-03-01"}
        )
        assert resp.status_code == 200, resp.content
        grant = AccessRight.objects.get(pk=grant_id)
        assert grant.assessment_reference == "PEER-DPIA"
        assert grant.expires_at == expires
        activity = Activity.objects.filter(verb="federation.grant.assess").latest("created_at")
        assert activity.target_object_id == str(grant_id)
        assert "PEER-DPIA" not in json.dumps(activity.metadata)

    def test_an_empty_patch_still_clears_the_expiry_and_not_the_assessment(self, auth_client):
        client, author = auth_client
        grant_id = self._create(
            client,
            _recording(author),
            _peer(),
            expires_at=(timezone.now() + timedelta(days=30)).isoformat(),
            assessment_reference="PEER-DPIA",
            assessment_date="2026-03-01",
        ).json()["id"]
        assert patch_json(client, f"{FED}/grants/{grant_id}/", {}).status_code == 200
        grant = AccessRight.objects.get(pk=grant_id)
        assert grant.expires_at is None
        assert grant.assessment_reference == "PEER-DPIA"

    def test_both_in_one_patch(self, auth_client):
        client, author = auth_client
        grant_id = self._create(client, _recording(author), _peer()).json()["id"]
        expires = timezone.now() + timedelta(days=10)
        resp = patch_json(
            client,
            f"{FED}/grants/{grant_id}/",
            {"expires_at": expires.isoformat(), "assessment_reference": "PEER-DPIA", "assessment_date": "2026-03-01"},
        )
        assert resp.status_code == 200
        grant = AccessRight.objects.get(pk=grant_id)
        assert grant.expires_at == expires
        assert grant.assessment_date == date(2026, 3, 1)

    def test_clearing_by_patch(self, auth_client):
        client, author = auth_client
        grant_id = self._create(
            client, _recording(author), _peer(), assessment_reference="PEER-DPIA", assessment_date="2026-03-01"
        ).json()["id"]
        resp = patch_json(client, f"{FED}/grants/{grant_id}/", {"assessment_reference": "", "assessment_date": None})
        assert resp.status_code == 200
        assert _assessment_of(grant_id) == ("", None)

    def test_only_the_giver_or_a_superuser(self, auth_client, make_user, client):
        _, author = auth_client
        peer = _peer()
        grant = _grant(_recording(author), author, federated_peer=peer)
        client.force_login(make_user())
        resp = patch_json(
            client, f"{FED}/grants/{grant.pk}/", {"assessment_reference": "X", "assessment_date": "2026-03-01"}
        )
        assert resp.status_code == 403

    def test_the_cli_records_and_clears(self, user):
        peer = _peer()
        grant = _grant(_recording(user), user, federated_peer=peer)
        out = StringIO()
        call_command(
            "federation_assess_grant",
            "--grant-id",
            str(grant.pk),
            "--reference",
            "PEER-DPIA",
            "--date",
            "2026-03-01",
            stdout=out,
        )
        assert _assessment_of(grant.pk) == ("PEER-DPIA", date(2026, 3, 1))
        assert "recorded" in out.getvalue()
        call_command("federation_assess_grant", "--grant-id", str(grant.pk), "--clear", stdout=StringIO())
        assert _assessment_of(grant.pk) == ("", None)

    def test_the_cli_refuses_half_a_record(self, user):
        grant = _grant(_recording(user), user, federated_peer=_peer())
        with pytest.raises(CommandError):
            call_command("federation_assess_grant", "--grant-id", str(grant.pk), "--reference", "X", stdout=StringIO())
        with pytest.raises(CommandError):
            call_command("federation_assess_grant", "--grant-id", str(grant.pk), "--clear", "--date", "2026-03-01")

    def test_federation_grant_takes_the_pair(self, user):
        peer = _peer()
        _recording(user)
        out = StringIO()
        call_command(
            "federation_grant",
            "--peer",
            str(peer.pk),
            "--giver",
            user.get_username(),
            "--recording",
            HASH,
            "--assessment-reference",
            "PEER-DPIA",
            "--assessment-date",
            "2026-03-01",
            stdout=out,
        )
        grant = AccessRight.objects.get(federated_peer=peer)
        assert _assessment_of(grant.pk) == ("PEER-DPIA", date(2026, 3, 1))


# ---------------------------------------------------------------------------
# explain_access
# ---------------------------------------------------------------------------


class TestExplainAccess:
    def test_names_the_assessment_on_the_direct_row(self, user, make_user):
        recording = _recording(user)
        reader = make_user()
        _grant(recording, user, target=reader, assessment_reference="DPIA-1", assessment_date=date(2026, 3, 1))
        out = StringIO()
        call_command("explain_access", "recordings.recording", str(recording.pk), reader.get_username(), stdout=out)
        assert "assessment: DPIA-1 (dated 2026-03-01)" in out.getvalue()

    def test_says_when_none_is_recorded(self, user, make_user):
        recording = _recording(user)
        reader = make_user()
        _grant(recording, user, target=reader)
        out = StringIO()
        call_command("explain_access", "recordings.recording", str(recording.pk), reader.get_username(), stdout=out)
        assert "assessment: none recorded" in out.getvalue()


# ---------------------------------------------------------------------------
# grant_assessments
# ---------------------------------------------------------------------------


def _seal_record(recording, *, on: date):
    """Write the READY audit row that carries the de-identification record, dated *on*."""
    from activity.audit import record_modify_change, serialize_instance
    from activity.system_activity import with_system_activity
    from recordings.deidentification_record import DEIDENTIFICATION_RECORD_KEY

    with with_system_activity("tests.seal", interface=Activity.Interface.COMMAND):
        row = record_modify_change(
            actor=None,
            obj=recording,
            before_state=serialize_instance(recording),
            extra_payload={DEIDENTIFICATION_RECORD_KEY: {"deidentification_version": 1}},
        )
    ObjectChangeLog.objects.filter(pk=row.pk).update(
        created_at=timezone.make_aware(timezone.datetime.combine(on, timezone.datetime.min.time()))
        + timedelta(hours=12)
    )
    return row


def _run(*args):
    out = StringIO()
    call_command("grant_assessments", *args, stdout=out)
    return out.getvalue()


def _run_json(*args):
    return json.loads(_run("--format", "json", *args))


class TestGrantAssessments:
    def test_classifies_every_grant(self, user, make_user):
        recording = _recording(user)
        _grant(recording, user, target=user, can_write=True, can_share=True)  # the author's row is not listed
        none = _grant(recording, user, target=make_user())
        current = _grant(recording, user, target=make_user(), assessment_reference="CUR", assessment_date=YESTERDAY)
        stale = _grant(
            recording,
            user,
            group=baker.make("auth.Group"),
            assessment_reference="OLD",
            assessment_date=TODAY - timedelta(days=200),
        )
        report = _run_json()
        by_id = {row["grant_id"]: row for row in report["grants"]}
        assert set(by_id) == {none.pk, current.pk, stale.pk}
        assert by_id[none.pk]["status"] == "none"
        assert by_id[current.pk]["status"] == "current"
        assert by_id[stale.pk]["status"] == "stale"
        assert by_id[stale.pk]["target"].startswith("group:")
        assert by_id[none.pk]["object"] == {"model": "recordings.recording", "handle": HASH}
        assert report["counts"] == {"none": 1, "current": 1, "stale": 1, "reprocessed": 0}

    def test_the_cut_off_is_an_argument(self, user, make_user):
        recording = _recording(user)
        right = _grant(
            recording, user, target=make_user(), assessment_reference="R", assessment_date=TODAY - timedelta(days=40)
        )
        assert _run_json()["grants"][0]["status"] == "current"
        assert _run_json("--older-than", "30")["grants"][0]["grant_id"] == right.pk
        assert _run_json("--older-than", "30")["grants"][0]["status"] == "stale"

    def test_a_recording_reprocessed_after_the_assessment_is_flagged(self, user, make_user):
        recording = _recording(user)
        right = _grant(
            recording, user, target=make_user(), assessment_reference="R", assessment_date=TODAY - timedelta(days=10)
        )
        _seal_record(recording, on=TODAY - timedelta(days=20))
        assert _run_json()["grants"][0]["status"] == "current"
        _seal_record(recording, on=TODAY - timedelta(days=5))
        row = _run_json()["grants"][0]
        assert row["grant_id"] == right.pk
        assert row["status"] == "reprocessed"
        assert row["reprocessed_count"] == 1

    def test_a_dataset_grant_covers_its_recording_members(self, user, make_user):
        recording = _recording(user)
        other = _recording(user, stored_name="0" * 32 + ".edf")
        dataset = baker.make(Dataset, author=user)
        recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        for member in (recording, other):
            DatasetItem.objects.create(dataset=dataset, content_type=recording_ct, object_id=str(member.pk))
        _grant(dataset, user, target=make_user(), assessment_reference="DS", assessment_date=TODAY - timedelta(days=10))
        _seal_record(recording, on=TODAY - timedelta(days=3))
        _seal_record(other, on=TODAY - timedelta(days=2))
        row = _run_json()["grants"][0]
        assert row["object"] == {"model": "library.dataset", "handle": dataset.object_hash}
        assert row["status"] == "reprocessed"
        assert row["reprocessed_count"] == 2

    def test_due_keeps_only_what_needs_a_re_run(self, user, make_user):
        recording = _recording(user)
        _grant(recording, user, target=make_user())
        _grant(recording, user, target=make_user(), assessment_reference="CUR", assessment_date=YESTERDAY)
        stale = _grant(
            recording, user, target=make_user(), assessment_reference="OLD", assessment_date=TODAY - timedelta(days=400)
        )
        report = _run_json("--due")
        assert [row["grant_id"] for row in report["grants"]] == [stale.pk]
        assert report["counts"]["none"] == 1

    def test_expired_grants_are_not_listed(self, user, make_user):
        recording = _recording(user)
        _grant(recording, user, target=make_user(), expires_at=timezone.now() - timedelta(days=1))
        assert _run_json()["grants"] == []

    def test_text_output_names_no_user_and_carries_the_reference(self, user, make_user):
        recording = _recording(user)
        reader = make_user(username="reader-with-a-name")
        _grant(recording, user, target=reader, assessment_reference="REF-42", assessment_date=YESTERDAY)
        out = _run()
        assert "reader-with-a-name" not in out
        assert f"user:{reader.pk}" in out
        assert "REF-42" in out
        assert "1 grant(s): 0 without an assessment, 1 current, 0 stale, 0 reprocessed" in out

    def test_the_run_is_audited_with_counts_only(self, user, make_user):
        recording = _recording(user)
        _grant(recording, user, target=make_user(), assessment_reference="REF-42", assessment_date=YESTERDAY)
        _run("--due")
        activity = Activity.objects.filter(verb="epicurrents.grant_assessments").latest("created_at")
        assert activity.interface == Activity.Interface.COMMAND
        assert activity.metadata == {"older_than_days": 183, "due_only": True, "format": "text"}


# ---------------------------------------------------------------------------
# The subject surfaces
# ---------------------------------------------------------------------------


class TestSubjectSurfaces:
    def test_the_export_carries_it_under_grants_given_and_not_received(self, make_user):
        from user.export import export_user_data

        giver = make_user()
        receiver = make_user()
        recording = _recording(giver)
        _grant(recording, giver, target=receiver, assessment_reference="GIVER-REF", assessment_date=YESTERDAY)

        given = export_user_data(giver)["data"]["epicurrents.accessright:access_giver"]
        assert given[0]["assessment_reference"] == "GIVER-REF"
        received = export_user_data(receiver)["data"]["epicurrents.accessright:access_target"]
        assert received
        assert "assessment_reference" not in received[0]
        from django.core.serializers.json import DjangoJSONEncoder

        assert "GIVER-REF" not in json.dumps(export_user_data(receiver), cls=DjangoJSONEncoder)

    def test_erasing_the_giver_scrubs_it_from_the_trail(self, make_user):
        from activity.erasure import ERASED_SENTINEL, erase_subject
        from activity.system_activity import with_system_activity

        giver = make_user()
        recording = _recording(giver)
        with with_system_activity("tests.history", interface=Activity.Interface.COMMAND):
            right = _grant(
                recording, giver, target=make_user(), assessment_reference="GIVER-REF", assessment_date=TODAY
            )
            right.assessment_reference = "GIVER-REF-2"
            right.save()
        giver_pk = giver.pk
        with with_system_activity("tests.delete", interface=Activity.Interface.COMMAND):
            giver.delete()

        erase_subject(giver_pk)

        right_ct = ContentType.objects.get_for_model(AccessRight, for_concrete_model=False)
        rows = list(ObjectChangeLog.objects.filter(content_type=right_ct, object_id=str(right.pk)))
        assert rows
        for row in rows:
            assert "GIVER-REF" not in json.dumps([row.before_state, row.changes])
            assert row.before_state.get("assessment_reference") in (ERASED_SENTINEL, None)
