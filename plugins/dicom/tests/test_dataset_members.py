"""A DICOM study as a dataset member.

Added and listed by its hash under its description; never in a release-gated dataset, whose gate has nothing to hide a
study with; left out for a reader without an account, since the study endpoints serve none; and, stored without the
header pass, given to nobody who reads the dataset under de-identifying terms, in the listing or on the study
surfaces. The terms refusal and its queryset form in the item type must agree row for row.
"""

import json

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object
from library.item_types import item_type_for
from library.models import Dataset, DatasetItem

ITEMS = "/api/v1/library/datasets/{}/items/"


def _study_ct():
    from plugins.dicom.models import DicomStudy

    return ContentType.objects.get_for_model(DicomStudy, for_concrete_model=False)


def _dataset(author, **fields):
    dataset = Dataset.objects.create(author=author, name="Ultrasound cases", **fields)
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
        object_id=str(dataset.pk),
        access_giver=author,
        access_target=author,
        can_read=True,
        can_write=True,
        can_share=True,
    )
    return dataset


def _member(dataset, study):
    return DatasetItem.objects.create(dataset=dataset, content_type=_study_ct(), object_id=str(study.pk))


def _share(dataset, giver, **target):
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(Dataset, for_concrete_model=False),
        object_id=str(dataset.pk),
        access_giver=giver,
        can_read=True,
        **target,
    )


def _study(make_study, author, description="Median nerve US", **kwargs):
    study = make_study(author, **kwargs)
    study.study_description = description
    study.save(update_fields=["study_description"])
    return study


@pytest.mark.django_db
class TestStudyAsDatasetMember:
    def test_added_by_hash_and_listed_by_type_hash_and_description(self, client, user, make_study):
        dataset = _dataset(user)
        study = _study(make_study, user)
        client.force_login(user)
        response = client.post(
            ITEMS.format(dataset.object_hash),
            json.dumps({"content_type_id": _study_ct().pk, "object_id": study.content_hash.upper()}),
            content_type="application/json",
        )
        assert response.status_code == 201
        row = response.json()
        assert (row["object_type"], row["object_hash"], row["object_name"]) == (
            "dicomstudy",
            study.content_hash,
            "Median nerve US",
        )
        listed = client.get(ITEMS.format(dataset.object_hash)).json()
        assert [r["object_hash"] for r in listed] == [study.content_hash]

    def test_an_unknown_hash_is_404(self, client, user):
        dataset = _dataset(user)
        client.force_login(user)
        response = client.post(
            ITEMS.format(dataset.object_hash),
            json.dumps({"content_type_id": _study_ct().pk, "object_id": "f" * 64}),
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_a_trashed_study_is_not_listed(self, client, user, make_study):
        dataset = _dataset(user)
        _member(dataset, _study(make_study, user, deleted_at=timezone.now()))
        client.force_login(user)
        assert client.get(ITEMS.format(dataset.object_hash)).json() == []

    def test_unnamed_study_is_listed_by_its_hash_prefix_never_the_patient(self, client, user, make_study):
        dataset = _dataset(user)
        study = _study(make_study, user, description="")
        _member(dataset, study)
        client.force_login(user)
        row = client.get(ITEMS.format(dataset.object_hash)).json()[0]
        assert row["object_name"] == study.content_hash[:8].upper()
        assert "Doe" not in json.dumps(row)

    def test_a_release_gated_dataset_takes_no_study(self, client, user, make_study):
        dataset = _dataset(user, release_gated=True)
        study = _study(make_study, user)
        client.force_login(user)
        response = client.post(
            ITEMS.format(dataset.object_hash),
            json.dumps({"content_type_id": _study_ct().pk, "object_id": study.content_hash}),
            content_type="application/json",
        )
        assert response.status_code == 409

    def test_a_dataset_holding_a_study_cannot_be_gated(self, client, user, make_study):
        dataset = _dataset(user)
        _member(dataset, _study(make_study, user))
        client.force_login(user)
        response = client.patch(
            f"/api/v1/library/datasets/{dataset.object_hash}/",
            json.dumps({"release_gated": True}),
            content_type="application/json",
        )
        assert response.status_code == 409
        dataset.refresh_from_db()
        assert dataset.release_gated is False

    def test_not_listed_to_a_share_token_reader(self, client, user, make_study):
        dataset = _dataset(user)
        _member(dataset, _study(make_study, user))
        _share(dataset, user, public_share_token="tok-ds")
        assert client.get(ITEMS.format(dataset.object_hash), {"share_token": "tok-ds"}).json() == []

    @pytest.mark.parametrize(
        ("version", "apply_middleware", "served"),
        [(None, True, False), (None, False, True), (1, True, True), (1, False, True)],
    )
    def test_a_raw_study_is_given_to_no_de_identifying_reader(
        self, client, user, make_user, make_study, version, apply_middleware, served
    ):
        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=version)
        _member(dataset, study)
        reader = make_user()
        _share(dataset, user, access_target=reader, apply_middleware=apply_middleware)
        assert can_read_object(reader, study) is served
        client.force_login(reader)
        assert bool(client.get(ITEMS.format(dataset.object_hash)).json()) is served
        detail = client.get(f"/plugin/dicom/api/v1/dicom/studies/{study.content_hash}/")
        assert detail.status_code == (200 if served else 404)

    @pytest.mark.parametrize("version", [None, 1])
    def test_the_listing_rule_agrees_with_the_terms_refusal(self, make_study, user, version):
        from epicurrents.permissions import _TERMS_REFUSALS
        from plugins.dicom.models import DicomStudy

        study = make_study(user, deidentification_version=version)
        item_type = item_type_for(_study_ct())
        withheld = item_type.withheld_under_middleware(DicomStudy.objects.filter(pk=study.pk)).exists()
        refused = any(rule(study) for rule in _TERMS_REFUSALS["dicom.dicomstudy"])
        assert withheld is refused
