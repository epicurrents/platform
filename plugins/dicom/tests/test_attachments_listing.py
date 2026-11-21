"""An attached study in the recording attachments listing.

Listed by its description with the pass the platform applied, `dicom-header` or `none`, as a record rather than an
assurance; withheld from a reader of the recording under de-identifying terms when it was stored as uploaded, as its
attachment rule withholds it everywhere; and never listed to a reader holding only a share token, since the study
endpoints would not serve them.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from epicurrents.models import AccessRight
from recordings.models import Recording

URL = "/recordings/api/v1/attachments/"
HASH = "R" * 32


@pytest.fixture
def recording(make_user):
    author = make_user()
    rec = Recording.objects.create(
        author=author,
        stored_name=f"{HASH}.edf",
        original_name="case.edf",
        file_size=1024,
        status=Recording.Status.READY,
    )
    _grant(rec, author, access_target=author)
    return rec


def _grant(obj, giver, **target):
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(obj, for_concrete_model=False),
        object_id=str(obj.pk),
        access_giver=giver,
        can_read=True,
        **target,
    )


def _attach(study, parent, description="Median nerve US"):
    study.attachment_content_type = ContentType.objects.get_for_model(parent, for_concrete_model=False)
    study.attachment_object_id = str(parent.pk)
    study.study_description = description
    study.save(update_fields=["attachment_content_type", "attachment_object_id", "study_description"])


@pytest.mark.django_db
class TestAttachedStudyListing:
    def test_listed_by_description_with_the_header_pass(self, client, recording, make_study):
        study = make_study(recording.author)
        _attach(study, recording)
        client.force_login(recording.author)
        assert client.get(URL, {"recordings": HASH}).json() == [
            {
                "parent": HASH,
                "type": "dicomstudy",
                "hash": study.content_hash,
                "name": "Median nerve US",
                "platform_pass": "dicom-header",
                "details": {"modalities": "CT"},
            }
        ]

    def test_patient_fields_never_listed(self, client, recording, make_study):
        study = make_study(recording.author, deidentification_version=None)
        _attach(study, recording)
        # Ingest writes the study's author a right of their own, which reads it whatever the recording grant says.
        _grant(study, recording.author, access_target=recording.author)
        client.force_login(recording.author)
        response = client.get(URL, {"recordings": HASH})
        assert "Doe" not in response.content.decode()
        assert response.json()[0]["platform_pass"] == "none"

    @pytest.mark.parametrize(
        ("version", "apply_middleware", "listed"),
        [(None, True, False), (None, False, True), (1, True, True)],
    )
    def test_follows_the_attachment_rule(
        self, client, recording, make_study, make_user, version, apply_middleware, listed
    ):
        study = make_study(recording.author, deidentification_version=version)
        _attach(study, recording)
        reader = make_user()
        _grant(recording, recording.author, access_target=reader, apply_middleware=apply_middleware)
        client.force_login(reader)
        assert bool(client.get(URL, {"recordings": HASH}).json()) is listed

    def test_not_listed_to_a_share_token_reader(self, client, recording, make_study):
        study = make_study(recording.author)
        _attach(study, recording)
        _grant(recording, recording.author, public_share_token="tok-study")
        assert client.get(URL, {"recordings": HASH, "share_token": "tok-study"}).json() == []

    def test_trashed_study_not_listed(self, client, recording, make_study):
        from django.utils import timezone

        study = make_study(recording.author, deleted_at=timezone.now())
        _attach(study, recording)
        client.force_login(recording.author)
        assert client.get(URL, {"recordings": HASH}).json() == []
