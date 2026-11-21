"""Tests for the dicom plugin's DICOMweb surface: QIDO-RS search and WADO-RS metadata for OHIF.

Covers the listing scope and the one-row-per-UID collapse, which copy a UID resolves to, the QIDO matching rules,
the DICOM JSON encoding, 404s for what the caller cannot reach, and the audit verbs.
"""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from model_bakery import baker

from activity.models import Activity
from epicurrents.models import AccessRight
from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy
from recordings.models import Recording

ROOT = "/plugin/dicom/api/v1/dicomweb"
UID = "1.2.826.0.1.7777001"

# DICOM tags as DICOM JSON keys them.
STUDY_UID, STUDY_DESCRIPTION, PATIENT_NAME = "0020000D", "00081030", "00100010"
SERIES_UID, SOP_UID, RESCALE_INTERCEPT, WINDOW_WIDTH = "0020000E", "00080018", "00281052", "00281051"


def _grant_read(obj, giver, target):
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(obj, for_concrete_model=False),
        object_id=str(obj.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
    )


# Each copy's series gets a UID of its own, numbered by label so it stays a valid UI value.
_LABELS = {"mine": 11, "theirs": 12, "inherited": 13, "granted": 14}


def _series_uid(label):
    return f"{UID}.{_LABELS[label]}"


def _copy(make_study, author, label, **fields):
    """A study with UID ``UID`` for *author*, told apart from other copies by *label*."""
    study = make_study(author, study_uid=UID)
    DicomStudy.objects.filter(pk=study.pk).update(study_description=label, **fields)
    DicomSeries.objects.filter(study=study).update(series_instance_uid=_series_uid(label))
    study.refresh_from_db()
    return study


def _get(client, path, **params):
    response = client.get(f"{ROOT}{path}", params)
    body = json.loads(response.content, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))
    return response, body


def _value(obj, tag):
    return obj[tag]["Value"][0]


@pytest.mark.django_db
class TestStudySearch:
    def test_lists_own_and_granted_studies_only(self, auth_client, make_study, make_user):
        client, user = auth_client
        other = make_user()
        own = make_study(user)
        granted = make_study(other)
        _grant_read(granted, other, user)
        make_study(other)
        make_study(user, deleted_at=timezone.now())
        response, body = _get(client, "/studies")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/dicom+json"
        assert {_value(s, STUDY_UID) for s in body} == {own.study_instance_uid, granted.study_instance_uid}

    def test_copies_of_one_study_collapse_to_the_callers_own(self, auth_client, make_study, make_user):
        client, user = auth_client
        other = make_user()
        theirs = _copy(make_study, other, "theirs")
        _grant_read(theirs, other, user)
        _copy(make_study, user, "mine")
        _, body = _get(client, "/studies")
        assert [_value(s, STUDY_DESCRIPTION) for s in body] == ["mine"]

    def test_study_level_encoding(self, auth_client, make_study):
        client, user = auth_client
        make_study(user)
        _, body = _get(client, "/studies")
        assert body[0][PATIENT_NAME] == {"vr": "PN", "Value": [{"Alphabetic": "Doe^John"}]}
        assert body[0]["00080061"] == {"vr": "CS", "Value": ["CT"]}
        assert body[0]["00201208"] == {"vr": "IS", "Value": [1]}
        # The fixture study has no date: an empty attribute is left out, not sent empty.
        assert "00080020" not in body[0]

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"PatientName": "*doe*"}, {"a"}),
            ({"PatientName": "ROE"}, {"b"}),
            ({"PatientName": "*"}, {"a", "b"}),
            ({"StudyDate": "20200101-20201231"}, {"a"}),
            ({"StudyDate": "20210101-"}, {"b"}),
            ({"StudyDate": "20220202"}, {"b"}),
            ({"ModalitiesInStudy": "MR"}, {"b"}),
            ({"ModalitiesInStudy": "CT\\MR"}, {"a", "b"}),
            ({"AccessionNumber": "*ACC1*"}, {"a"}),
        ],
    )
    def test_filters(self, auth_client, make_study, params, expected):
        client, user = auth_client
        for label, name, date, modalities, accession in [
            ("a", "Doe^John", "20200615", "CT", "ACC1"),
            ("b", "Roe^Jane", "20220202", "CT,MR", "ACC2"),
        ]:
            study = make_study(user)
            DicomStudy.objects.filter(pk=study.pk).update(
                study_description=label,
                patient_name=name,
                study_date=date,
                modalities=modalities,
                accession_number=accession,
            )
        _, body = _get(client, "/studies", **params)
        assert {_value(s, STUDY_DESCRIPTION) for s in body} == expected

    def test_study_uid_filter_takes_a_list(self, auth_client, make_study):
        client, user = auth_client
        a, b, _ = make_study(user), make_study(user), make_study(user)
        _, body = _get(client, "/studies", StudyInstanceUID=f"{a.study_instance_uid},{b.study_instance_uid}")
        assert {_value(s, STUDY_UID) for s in body} == {a.study_instance_uid, b.study_instance_uid}

    def test_paging(self, auth_client, make_study):
        client, user = auth_client
        for _ in range(5):
            make_study(user)
        assert len(_get(client, "/studies", limit=2)[1]) == 2
        assert len(_get(client, "/studies", limit=2, offset=4)[1]) == 1
        assert len(_get(client, "/studies", limit="many")[1]) == 5

    def test_unauthenticated_is_refused(self, client):
        assert client.get(f"{ROOT}/studies").status_code == 401

    def test_audit_names_the_filters_but_not_their_values(self, auth_client, make_study):
        client, user = auth_client
        make_study(user)
        _get(client, "/studies", PatientName="*doe*", limit=10)
        activity = Activity.objects.order_by("-pk").first()
        assert activity.verb == "dicom.study.list.qido"
        assert activity.metadata == {"count": 1, "filters": ["PatientName"]}
        assert "doe" not in json.dumps(activity.metadata).lower()


@pytest.mark.django_db
class TestResolution:
    def test_own_copy_wins_over_an_earlier_granted_one(self, auth_client, make_study, make_user):
        client, user = auth_client
        other = make_user()
        _grant_read(_copy(make_study, other, "theirs"), other, user)
        _copy(make_study, user, "mine")
        _, body = _get(client, f"/studies/{UID}/series")
        assert [_value(s, SERIES_UID) for s in body] == [_series_uid("mine")]

    def test_listed_copy_wins_over_an_attachment_inherited_one(self, auth_client, make_study, make_user):
        client, user = auth_client
        other = make_user()
        inherited = _copy(make_study, other, "inherited")
        recording = baker.make(Recording, author=other, status=Recording.Status.READY, content_hash="R" * 64)
        _grant_read(recording, other, user)
        DicomStudy.objects.filter(pk=inherited.pk).update(
            attachment_content_type=ContentType.objects.get_for_model(Recording),
            attachment_object_id=str(recording.pk),
        )
        third = make_user()
        _grant_read(_copy(make_study, third, "granted"), third, user)
        _, body = _get(client, f"/studies/{UID}/series")
        assert [_value(s, SERIES_UID) for s in body] == [_series_uid("granted")]

    def test_attachment_inherited_copy_is_reachable_but_not_listed(self, auth_client, make_study, make_user):
        client, user = auth_client
        other = make_user()
        study = _copy(make_study, other, "inherited")
        recording = baker.make(Recording, author=other, status=Recording.Status.READY, content_hash="R" * 64)
        _grant_read(recording, other, user)
        DicomStudy.objects.filter(pk=study.pk).update(
            attachment_content_type=ContentType.objects.get_for_model(Recording),
            attachment_object_id=str(recording.pk),
        )
        assert _get(client, "/studies")[1] == []
        assert client.get(f"{ROOT}/studies/{UID}/series").status_code == 200

    @pytest.mark.parametrize("trashed", [False, True])
    def test_unreadable_or_trashed_study_is_a_404(self, auth_client, make_study, make_user, trashed):
        client, user = auth_client
        owner = user if trashed else make_user()
        make_study(owner, study_uid=UID, deleted_at=timezone.now() if trashed else None)
        for path in ["/series", "/metadata", f"/series/{UID}.1/metadata"]:
            assert client.get(f"{ROOT}/studies/{UID}{path}").status_code == 404


@pytest.mark.django_db
class TestMetadata:
    @pytest.fixture
    def study(self, auth_client, make_study):
        _, user = auth_client
        study = make_study(user, study_uid=UID, instance_count=2)
        DicomInstance.objects.filter(series__study=study).update(
            rows=128, columns=128, rescale_intercept="-1024", rescale_slope="1", window_width=""
        )
        return study

    def test_series_metadata_lists_ready_instances(self, auth_client, study):
        client, _ = auth_client
        first = DicomInstance.objects.filter(series__study=study).order_by("pk").first()
        DicomInstance.objects.filter(pk=first.pk).update(status=DicomInstance.Status.FAILED)
        response, body = _get(client, f"/studies/{UID}/series/{UID}.1/metadata")
        assert response["Content-Type"] == "application/dicom+json"
        assert len(body) == 1
        instance = body[0]
        assert instance[RESCALE_INTERCEPT] == {"vr": "DS", "Value": [-1024.0]}
        assert WINDOW_WIDTH not in instance
        assert _value(instance, STUDY_UID) == UID
        assert instance["00280010"] == {"vr": "US", "Value": [128]}

    def test_non_finite_and_malformed_values_drop_only_themselves(self, auth_client, study):
        client, _ = auth_client
        DicomInstance.objects.filter(series__study=study).update(window_center="NaN", window_width="inf")
        DicomInstance.objects.filter(series__study=study).update(rescale_slope="not-a-number")
        _, body = _get(client, f"/studies/{UID}/series/{UID}.1/metadata")
        assert len(body) == 2
        for instance in body:
            assert "00281050" not in instance and WINDOW_WIDTH not in instance and "00281053" not in instance
            assert instance[RESCALE_INTERCEPT]["Value"] == [-1024.0]

    def test_study_metadata_covers_every_series(self, auth_client, study):
        client, _ = auth_client
        _, body = _get(client, f"/studies/{UID}/metadata")
        assert len(body) == 2
        assert {_value(i, SERIES_UID) for i in body} == {f"{UID}.1"}

    def test_unknown_series_is_a_404(self, auth_client, study):
        client, _ = auth_client
        assert client.get(f"{ROOT}/studies/{UID}/series/9.9.9/metadata").status_code == 404

    def test_series_of_another_copy_is_not_reachable(self, auth_client, study, make_study, make_user):
        client, _ = auth_client
        other = make_user()
        theirs = _copy(make_study, other, "theirs")
        _grant_read(theirs, other, auth_client[1])
        # The caller's own copy wins the UID, so the other copy's series does not resolve through it.
        assert client.get(f"{ROOT}/studies/{UID}/series/{_series_uid('theirs')}/metadata").status_code == 404

    @pytest.mark.parametrize(
        ("path", "verb"),
        [
            (f"/studies/{UID}/series", "dicom.series.list.qido"),
            (f"/studies/{UID}/metadata", "dicom.study.read.metadata"),
            (f"/studies/{UID}/series/{UID}.1/metadata", "dicom.series.read.metadata"),
        ],
    )
    def test_audit_verbs(self, auth_client, study, path, verb):
        client, _ = auth_client
        client.get(f"{ROOT}{path}")
        assert Activity.objects.order_by("-pk").first().verb == verb
