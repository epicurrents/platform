"""Model tests for the dicom plugin.

These tests do not require pydicom — they exercise the Django models, the
author-scoped content-hash generation, and the per-author uniqueness rules.
"""

import pytest
from django.db import IntegrityError, transaction

from plugins.dicom.models import DicomInstance, DicomSeries, DicomStudy

STUDY_UID = "1.2.840.10008.5.1.4.1.1.2.0001"
SERIES_UID = "1.2.840.10008.5.1.4.1.1.2.0001.001"
SOP_UID = "1.2.840.10008.5.1.4.1.1.2.0001.001.001"


@pytest.fixture()
def dicom_study(user):
    return DicomStudy.objects.create(
        author=user,
        study_instance_uid=STUDY_UID,
        content_hash=DicomStudy.make_content_hash(user.pk, STUDY_UID),
        patient_name="Doe^John",
        modalities="CT",
        num_instances=1,
    )


@pytest.fixture()
def dicom_series(dicom_study):
    return DicomSeries.objects.create(
        study=dicom_study,
        series_instance_uid=SERIES_UID,
        modality="CT",
    )


@pytest.fixture()
def dicom_instance(dicom_series):
    return DicomInstance.objects.create(
        series=dicom_series,
        sop_instance_uid=SOP_UID,
        stored_name="test_instance.dcm",
        file_size=1024,
        status=DicomInstance.Status.READY,
    )


def test_app_config_is_plugin_config():
    """Regression: Django's config auto-detection must pick DicomConfig.

    With PluginConfig imported into apps.py and no explicit ``default``,
    Django silently instantiated the bare AppConfig — ready() never ran, so
    the file-unlink receiver and the attachment permission extension were
    silently absent.
    """
    from django.apps import apps

    from epicurrents.plugins import PluginConfig

    assert isinstance(apps.get_app_config("dicom"), PluginConfig)


@pytest.mark.django_db
class TestDicomStudy:
    def test_content_hash_is_deterministic(self, user):
        h1 = DicomStudy.make_content_hash(user.pk, STUDY_UID)
        h2 = DicomStudy.make_content_hash(user.pk, STUDY_UID)
        assert h1 == h2

    def test_content_hash_differs_for_different_uids(self, user):
        h1 = DicomStudy.make_content_hash(user.pk, STUDY_UID)
        h2 = DicomStudy.make_content_hash(user.pk, "1.2.3.999")
        assert h1 != h2

    def test_content_hash_differs_per_author(self, make_user):
        a, b = make_user(), make_user()
        assert DicomStudy.make_content_hash(a.pk, STUDY_UID) != (DicomStudy.make_content_hash(b.pk, STUDY_UID))

    def test_content_hash_is_64_hex_chars(self, user):
        h = DicomStudy.make_content_hash(user.pk, STUDY_UID)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_uid_allowed_for_different_authors(self, dicom_study, make_user):
        other = make_user()
        copy = DicomStudy.objects.create(
            author=other,
            study_instance_uid=STUDY_UID,
            content_hash=DicomStudy.make_content_hash(other.pk, STUDY_UID),
        )
        assert copy.pk != dicom_study.pk

    def test_same_uid_rejected_for_same_author(self, dicom_study, user):
        with pytest.raises(IntegrityError), transaction.atomic():
            DicomStudy.objects.create(
                author=user,
                study_instance_uid=STUDY_UID,
                content_hash="f" * 64,
            )

    def test_soft_delete_leaves_rows(self, dicom_instance):
        from django.utils import timezone

        study = dicom_instance.series.study
        study.deleted_at = timezone.now()
        study.save(update_fields=["deleted_at"])
        assert DicomStudy.objects.filter(pk=study.pk).exists()
        assert DicomInstance.objects.filter(pk=dicom_instance.pk).exists()

    def test_cascade_delete_removes_series_and_instances(self, dicom_instance):
        study = dicom_instance.series.study
        study_pk = study.pk
        series_pk = dicom_instance.series.pk
        instance_pk = dicom_instance.pk
        study.delete()
        assert not DicomStudy.objects.filter(pk=study_pk).exists()
        assert not DicomSeries.objects.filter(pk=series_pk).exists()
        assert not DicomInstance.objects.filter(pk=instance_pk).exists()

    def test_str(self, dicom_study):
        assert STUDY_UID in str(dicom_study)


@pytest.mark.django_db
class TestDicomSeries:
    def test_unique_per_study_enforced(self, dicom_study):
        DicomSeries.objects.create(
            study=dicom_study,
            series_instance_uid="1.2.3.UNIQUE",
            modality="MR",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            DicomSeries.objects.create(
                study=dicom_study,
                series_instance_uid="1.2.3.UNIQUE",
                modality="MR",
            )


@pytest.mark.django_db
class TestDicomInstance:
    def test_status_choices(self, dicom_instance):
        assert dicom_instance.status == DicomInstance.Status.READY

    def test_pending_is_default(self, dicom_series):
        inst = DicomInstance(
            series=dicom_series,
            sop_instance_uid="1.2.3.NEW",
            stored_name="new.dcm",
            file_size=512,
        )
        assert inst.status == DicomInstance.Status.PENDING

    def test_sop_uid_unique_per_series(self, dicom_instance, dicom_series):
        with pytest.raises(IntegrityError), transaction.atomic():
            DicomInstance.objects.create(
                series=dicom_series,
                sop_instance_uid=SOP_UID,
                stored_name="other.dcm",
                file_size=1,
            )

    def test_same_sop_uid_allowed_across_studies(self, dicom_instance, make_user):
        other = make_user()
        study = DicomStudy.objects.create(
            author=other,
            study_instance_uid=STUDY_UID,
            content_hash=DicomStudy.make_content_hash(other.pk, STUDY_UID),
        )
        series = DicomSeries.objects.create(study=study, series_instance_uid=SERIES_UID)
        copy = DicomInstance.objects.create(
            series=series,
            sop_instance_uid=SOP_UID,
            stored_name="copy.dcm",
            file_size=1,
        )
        assert copy.pk != dicom_instance.pk
