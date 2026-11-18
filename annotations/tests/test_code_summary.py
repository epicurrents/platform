"""The code summary of a recording, ``GET /recordings/{hash}/codes/``: counts per standard and value, the vocabulary's
names, no ``meta`` under any grant, rows on the recording only, and the hiding and access rules of the listings."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from model_bakery import baker

from activity.models import Activity
from annotations.models import Code, Event, Interruption, Label
from epicurrents.models import AccessRight
from recordings.models import Recording

pytestmark = pytest.mark.django_db

HASH = "C0DE" * 8


def _url(recording_hash: str) -> str:
    return f"/annotations/api/v1/recordings/{recording_hash}/codes/"


def _recording(author, *, recording_hash=HASH, **kwargs):
    """A recording with its author's own access row, which the upload endpoint writes and ``baker.make`` does not."""
    recording = baker.make(
        Recording,
        author=author,
        stored_name=f"{recording_hash}.edf",
        file_size=1,
        status=Recording.Status.READY,
        **kwargs,
    )
    _grant(recording, author)
    return recording


def _ct(obj):
    return ContentType.objects.get_for_model(obj, for_concrete_model=False)


def _grant(recording, target, **fields):
    AccessRight.objects.create(
        content_type=_ct(recording),
        object_id=str(recording.pk),
        access_giver=recording.author,
        access_target=target,
        can_read=True,
        **fields,
    )


def _code(row, standard, value, meta=None):
    return baker.make(Code, content_type=_ct(row), object_id=str(row.pk), standard=standard, value=value, meta=meta)


def _get(user, recording_hash=HASH):
    client = Client()
    if user is not None:
        client.force_login(user)
    return client.get(_url(recording_hash))


@pytest.fixture
def coded(make_user):
    """A recording with codes on two events, a label and an interruption, and a code on another recording."""
    owner = make_user()
    recording = _recording(owner)
    on = {"target_content_type": _ct(recording), "target_object_id": str(recording.pk)}
    first = baker.make(Event, author=owner, name="a", timestamp=1.0, **on)
    second = baker.make(Event, author=make_user(), name="b", timestamp=2.0, **on)
    label = baker.make(Label, author=owner, name="c", **on)
    interruption = baker.make(Interruption, author=owner, timestamp=3.0, duration=1.0, **on)
    _code(first, "epicurrents.eeg", "EEG_ACT_HV", meta={"note": "typed by a person"})
    _code(second, "epicurrents.eeg", "EEG_ACT_HV")
    _code(label, "epicurrents.eeg", "NOT_A_TERM")
    _code(first, "local.unregistered", "x")
    _code(interruption, "local.unregistered", "y", meta={"note": "electrode replaced"})
    other = _recording(owner, recording_hash="0" * 32)
    elsewhere = baker.make(
        Event, author=owner, name="d", timestamp=1.0, target_content_type=_ct(other), target_object_id=str(other.pk)
    )
    _code(elsewhere, "epicurrents.eeg", "EEG_ACT_HV")
    return {"owner": owner, "recording": recording}


class TestSummary:
    def test_counts_per_standard_and_value(self, coded):
        response = _get(coded["owner"])
        assert response.status_code == 200, response.content
        assert response.json() == [
            {
                "standard": "epicurrents.eeg",
                "label": response.json()[0]["label"],
                "count": 3,
                "codes": [
                    {"value": "EEG_ACT_HV", "name": "Hyperventilation", "count": 2},
                    {"value": "NOT_A_TERM", "name": None, "count": 1},
                ],
            },
            {
                "standard": "local.unregistered",
                "label": None,
                "count": 2,
                "codes": [{"value": "x", "name": None, "count": 1}, {"value": "y", "name": None, "count": 1}],
            },
        ]
        assert response.json()[0]["label"]

    def test_no_meta_under_a_de_identifying_grant(self, coded, make_user):
        reader = make_user()
        _grant(coded["recording"], reader, apply_middleware=True)
        response = _get(reader)
        assert response.status_code == 200
        assert "typed by a person" not in response.content.decode()
        assert "electrode replaced" not in response.content.decode()
        assert [group["count"] for group in response.json()] == [3, 2]

    def test_an_uncoded_recording_answers_an_empty_list(self, make_user):
        owner = make_user()
        _recording(owner)
        assert _get(owner).json() == []

    def test_audit_row(self, coded):
        _get(coded["owner"])
        row = Activity.objects.filter(verb="annotations.code.list").latest("pk")
        assert row.metadata == {"standard_count": 2, "code_count": 4}
        assert row.target_object_id == str(coded["recording"].pk)


class TestAccess:
    def test_requires_an_account(self, coded):
        assert _get(None).status_code == 401

    def test_no_grant_is_403(self, coded, make_user):
        assert _get(make_user()).status_code == 403

    def test_unknown_hash_and_primary_key_are_404(self, coded):
        assert _get(coded["owner"], "F" * 32).status_code == 404
        assert _get(coded["owner"], str(coded["recording"].pk)).status_code == 404

    def test_trashed_and_failed_are_404_to_a_grantee(self, coded, make_user):
        from django.utils import timezone

        reader = make_user()
        _grant(coded["recording"], reader)
        Recording.objects.filter(pk=coded["recording"].pk).update(status=Recording.Status.FAILED)
        assert _get(reader).status_code == 404
        assert _get(coded["owner"]).status_code == 200
        Recording.objects.filter(pk=coded["recording"].pk).update(
            status=Recording.Status.READY, deleted_at=timezone.now()
        )
        assert _get(reader).status_code == 404

    def test_an_unreleased_gated_member_is_404_to_a_reader(self, coded, make_user):
        from library.tests.test_release import _gated_dataset
        from library.tests.test_release import _grant as _object_grant

        reader = make_user()
        dataset, _ = _gated_dataset(coded["owner"], coded["recording"])
        _object_grant(dataset, coded["owner"], target=reader)
        assert _get(reader).status_code == 404
