"""Cascade tests for DicomStudy as a GenericFK target.

One test per (DicomStudy, reference-row type) pair per the GenericFK target
cascade pattern in AGENTS.md, plus the account-erasure cascade: hard-deleting
a user removes their studies and unlinks the stored files through the
``pre_delete`` receiver.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from epicurrents.models import AccessRight
from plugins.dicom.models import DicomInstance, DicomStudy


def _study_ct(study):
    return ContentType.objects.get_for_model(study, for_concrete_model=False)


@pytest.mark.django_db
class TestStudyCascade:
    def test_delete_removes_access_rights(self, user, make_user, make_study):
        study = make_study(user)
        right = AccessRight.objects.create(
            content_type=_study_ct(study),
            object_id=str(study.pk),
            access_giver=user,
            access_target=make_user(),
            can_read=True,
        )
        study.delete()
        assert not AccessRight.objects.filter(pk=right.pk).exists()

    def test_delete_removes_collection_membership(self, user, make_study):
        from library.models import Collection, CollectionItem

        study = make_study(user)
        collection = Collection.objects.create(author=user, name="c")
        item = CollectionItem.objects.create(
            collection=collection,
            content_type=_study_ct(study),
            object_id=str(study.pk),
        )
        study.delete()
        assert not CollectionItem.objects.filter(pk=item.pk).exists()

    def test_delete_removes_dataset_membership(self, user, make_study):
        from library.models import Dataset, DatasetItem

        study = make_study(user)
        dataset = Dataset.objects.create(author=user, name="d")
        item = DatasetItem.objects.create(
            dataset=dataset,
            content_type=_study_ct(study),
            object_id=str(study.pk),
        )
        study.delete()
        assert not DatasetItem.objects.filter(pk=item.pk).exists()

    def test_delete_removes_tagged_item(self, user, make_study):
        from library.models import Tag, TaggedItem

        study = make_study(user)
        tag = Tag.objects.create(author=user, name="t")
        item = TaggedItem.objects.create(
            tag=tag,
            content_type=_study_ct(study),
            object_id=str(study.pk),
        )
        study.delete()
        assert not TaggedItem.objects.filter(pk=item.pk).exists()

    def test_user_delete_cascades_and_unlinks_files(self, make_user, make_study, dicom_dirs):
        upload_dir, _ = dicom_dirs
        owner = make_user()
        study = make_study(owner, instance_count=2)
        stored = list(DicomInstance.objects.filter(series__study=study).values_list("stored_name", flat=True))
        assert all((upload_dir / name).exists() for name in stored)

        owner.delete()

        assert not DicomStudy.objects.filter(pk=study.pk).exists()
        assert not DicomInstance.objects.exists()
        for name in stored:
            assert not (upload_dir / name).exists()

    def test_soft_delete_leaves_reference_rows(self, user, make_user, make_study):
        from django.utils import timezone

        study = make_study(user)
        right = AccessRight.objects.create(
            content_type=_study_ct(study),
            object_id=str(study.pk),
            access_giver=user,
            access_target=make_user(),
            can_read=True,
        )
        study.deleted_at = timezone.now()
        study.save(update_fields=["deleted_at"])
        assert AccessRight.objects.filter(pk=right.pk).exists()
        assert DicomInstance.objects.filter(series__study=study).exists()
