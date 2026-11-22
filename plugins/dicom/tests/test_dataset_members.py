"""A DICOM study as a dataset member.

Added and listed by its hash under its description; never in a release-gated dataset, whose gate has nothing to hide a
study with; left out for a reader without an account, since the study endpoints serve none; and, stored without the
header pass, given to nobody who reads the dataset under de-identifying terms, in the listing or on the study
surfaces. The terms refusal and its queryset form in the item type must agree row for row. The dataset's author is
listed every study, their own row's default terms notwithstanding, so that one stored as uploaded can be seen and
removed; each row names the pass applied to the stored files, and its sequential keys go to the dataset's managers.
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

    def test_a_study_is_arranged_in_the_manual_order_like_any_member(self, client, user, make_user, make_study):
        dataset = _dataset(user)
        # Unsorted, studies list last and by hash; arranged, they stand where the manager put them.
        studies = [_study(make_study, user, description=name) for name in ("Ulnar nerve", "Median nerve")]
        items = [_member(dataset, study) for study in studies]
        reader = make_user()
        _share(dataset, user, access_target=reader, apply_middleware=False)
        unsorted = sorted(study.content_hash for study in studies)
        client.force_login(user)
        assert [r["object_hash"] for r in client.get(ITEMS.format(dataset.object_hash)).json()] == unsorted
        last = next(item for item, study in zip(items, studies, strict=True) if study.content_hash == unsorted[1])
        response = client.post(
            f"{ITEMS.format(dataset.object_hash)}{last.pk}/order",
            json.dumps({"action": "top"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        rows = client.get(ITEMS.format(dataset.object_hash)).json()
        assert [(r["object_hash"], r["position"]) for r in rows] == [(unsorted[1], 1), (unsorted[0], None)]
        client.force_login(reader)
        rows = client.get(ITEMS.format(dataset.object_hash)).json()
        assert [(r["object_hash"], r["position"]) for r in rows] == [(unsorted[1], None), (unsorted[0], None)]

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


@pytest.mark.django_db
class TestRawStudyAndTheDatasetAuthor:
    """The dataset author's own row carries ``apply_middleware`` at its default, and they are listed every study."""

    def _rows(self, client, dataset):
        response = client.get(ITEMS.format(dataset.object_hash))
        assert response.status_code == 200, response.content
        return response.json()

    def test_the_author_is_listed_and_can_remove_their_own_raw_study(self, client, user, make_study):
        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=None)
        client.force_login(user)
        added = client.post(
            ITEMS.format(dataset.object_hash),
            json.dumps({"content_type_id": _study_ct().pk, "object_id": study.content_hash}),
            content_type="application/json",
        )
        assert added.status_code == 201
        (row,) = self._rows(client, dataset)
        assert (row["object_hash"], row["platform_pass"]) == (study.content_hash, "none")
        assert can_read_object(user, study) is True
        removed = client.delete(f"{ITEMS.format(dataset.object_hash)}{row['id']}/")
        assert removed.status_code == 200
        assert self._rows(client, dataset) == []

    def test_the_author_is_listed_a_raw_study_they_read_by_a_raw_grant(self, client, user, make_user, make_study):
        other = make_user()
        dataset = _dataset(user)
        study = _study(make_study, other, deidentification_version=None)
        AccessRight.objects.create(
            content_type=_study_ct(),
            object_id=str(study.pk),
            access_giver=other,
            access_target=user,
            can_read=True,
            apply_middleware=False,
        )
        _member(dataset, study)
        client.force_login(user)
        (row,) = self._rows(client, dataset)
        assert (row["object_hash"], row["platform_pass"], row["id"] is not None) == (study.content_hash, "none", True)
        assert can_read_object(user, study) is True

    def test_a_de_identifying_reader_with_a_raw_grant_of_their_own_is_listed_it(
        self, client, user, make_user, make_study
    ):
        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=None)
        _member(dataset, study)
        reader, classmate = make_user(), make_user()
        for target in (reader, classmate):
            _share(dataset, user, access_target=target, apply_middleware=True)
        AccessRight.objects.create(
            content_type=_study_ct(),
            object_id=str(study.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
            apply_middleware=False,
        )
        client.force_login(reader)
        assert [row["object_hash"] for row in self._rows(client, dataset)] == [study.content_hash]
        assert can_read_object(reader, study) is True
        client.force_login(classmate)
        assert self._rows(client, dataset) == []
        assert can_read_object(classmate, study) is False

    def test_a_write_grantee_on_de_identifying_terms_is_not_listed_it(self, client, user, make_user, make_study):
        dataset = _dataset(user)
        _member(dataset, _study(make_study, user, deidentification_version=None))
        manager = make_user()
        _share(dataset, user, access_target=manager, can_write=True, apply_middleware=True)
        client.force_login(manager)
        assert self._rows(client, dataset) == []

    def test_a_superuser_is_listed_it(self, client, user, superuser, make_study):
        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=None)
        _member(dataset, study)
        client.force_login(superuser)
        assert [row["platform_pass"] for row in self._rows(client, dataset)] == ["none"]

    @pytest.mark.parametrize(("version", "applied"), [(None, "none"), (1, "dicom-header")])
    def test_a_row_names_the_pass_as_the_attachments_listing_does(self, client, user, make_study, version, applied):
        from epicurrents.attachments import registered_attachment_types

        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=version)
        _member(dataset, study)
        client.force_login(user)
        (row,) = self._rows(client, dataset)
        (attachment_type,) = [t for t in registered_attachment_types() if t.key == "dicomstudy"]
        assert row["platform_pass"] == applied == attachment_type.platform_pass(study)

    def test_a_reader_who_does_not_manage_the_dataset_gets_no_sequential_key(self, client, user, make_user, make_study):
        dataset = _dataset(user)
        study = _study(make_study, user, deidentification_version=1)
        item = _member(dataset, study)
        reader, manager = make_user(), make_user()
        _share(dataset, user, access_target=reader, apply_middleware=False)
        _share(dataset, user, access_target=manager, can_write=True, apply_middleware=False)
        expected = {reader: None, manager: item.pk, user: item.pk}
        for caller, item_id in expected.items():
            client.force_login(caller)
            (row,) = self._rows(client, dataset)
            assert row["id"] == item_id
            # The study's own primary key is served to nobody, a manager included.
            assert "object_id" not in row
            assert row["object_hash"] == study.content_hash
