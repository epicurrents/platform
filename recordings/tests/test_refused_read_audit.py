"""A refused read of a recording lands on the same verb and target as a served one.

The dataset access report (``library.reports.access_report``) counts a dataset's refused requests
as ``Activity`` rows with a read verb, the member as target and a 4xx status. Those rows exist only
if each read surface names its verb and target before the gate or the permission check refuses, so
this pins that for every read surface and both refusals. The target is the recording, a locator:
no name reaches the row.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from activity.models import Activity
from recordings.models import Recording
from recordings.tests.test_metadata import _make_recording

HASH = "0F" * 16

ROUTES = [
    ("recordings.read", f"/recordings/api/v1/{HASH}"),
    ("recordings.status", f"/recordings/api/v1/status/{HASH}"),
    ("recordings.read.slice", f"/recordings/api/v1/{HASH}/slice?t_start=0&t_end=1"),
    ("recordings.annotations.list", f"/recordings/api/v1/{HASH}/annotations"),
    ("recordings.download", f"/recordings/api/v1/{HASH}/file"),
    ("recordings.download.slice", f"/recordings/api/v1/{HASH}/file/slice?t_start=0&t_end=1"),
]


@pytest.fixture
def recording(db, user, tmp_path):
    recording, _meta = _make_recording(user, tmp_path, n_channels=2, stored_name=f"{HASH}.edf")
    return recording


def _row(recording):
    return Activity.objects.filter(path__startswith="/recordings/api/v1/").latest("pk")


@pytest.mark.django_db
class TestRefusedReadsAreAttributed:
    @pytest.mark.parametrize(("verb", "url"), ROUTES)
    def test_a_permission_refusal_carries_the_read_verb_and_the_recording(self, recording, make_user, verb, url):
        client = Client()
        client.force_login(make_user())
        response = client.get(url)
        assert response.status_code == 403, response.content
        row = _row(recording)
        assert (row.verb, row.status_code) == (verb, 403)
        assert row.target_content_type == ContentType.objects.get_for_model(Recording, for_concrete_model=False)
        assert row.target_object_id == str(recording.pk)
        assert row.metadata == {}

    @pytest.mark.parametrize(("verb", "url"), ROUTES)
    def test_a_gate_refusal_carries_the_read_verb_and_the_recording(self, recording, make_user, verb, url):
        Recording.objects.filter(pk=recording.pk).update(status=Recording.Status.FAILED)
        client = Client()
        client.force_login(make_user())
        response = client.get(url)
        assert response.status_code == 404, response.content
        row = _row(recording)
        assert (row.verb, row.status_code, row.target_object_id) == (verb, 404, str(recording.pk))
