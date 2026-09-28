"""Contract test for the grantee-visible recording response shape.

Backs the second ⚠️ LOAD-BEARING contract in recordings/api/v1/ninja.py. Every key a reader who is
not the author receives from the recording detail, listing and slice responses is listed here as a
literal, so a field added to an ``Out`` schema fails this suite until someone decides what the
field discloses and re-reads the given-data table in docs/anonymisation-compliance.md. Two
decisions already made are pinned as values rather than keys: ``file_hash`` is never serialised,
to anyone, and ``created_at`` is exact for the author and month-truncated for everyone else.

The reader classes are the ones the assessment names: a local grantee under a de-identifying
grant, a share-token holder, and a federated peer. The author is included where the contract says
the author is treated differently, so a change in the wrong direction is caught on that side too.
"""

from datetime import datetime

import pytest
from django.test import Client

from epicurrents.models import AccessRight
from recordings.tests.test_federation import _as_peer, _make_peer
from recordings.tests.test_federation import _grant as _peer_grant
from recordings.tests.test_metadata import _make_recording
from recordings.tests.test_serve_pipeline_parity import _grant_with_middleware

TOKEN = "shape-share-token"

_GIVEN_DATA_TABLE = (
    "the response shape changed; re-read the given-data table in docs/anonymisation-compliance.md "
    "and update this literal in the same commit"
)

# Keys of the recording detail and listing responses, ``RecordingOut``.
RECORDING_KEYS = {
    "hash",
    "original_name",
    "display_name",
    "has_custom_name",
    "processing_error",
    "file_extension",
    "file_size",
    "stored_hash",
    "content_hash",
    "status",
    "modality",
    "public_source",
    "created_at",
    "deleted_at",
    "meta",
    "events",
    "interruptions",
    "labels",
    "trashed_collection",
    "download_size",
}

# Keys of ``RecordingMetaOut``.
META_KEYS = {
    "format",
    "duration",
    "data_record_count",
    "data_record_duration",
    "signal_count",
    "discontinuous",
    "channel_layout",
    "unresolved_channel_count",
    "channel_order_version",
    "deidentification_version",
    "annotation_text_preserved",
    "signals",
}

# Keys of ``SignalInfoOut``. The ``source_*`` entries are present but null for every non-author.
SIGNAL_KEYS = {
    "index",
    "label",
    "sample_count",
    "sampling_rate",
    "is_annotation_channel",
    "signal_type",
    "physical_unit",
    "physical_min",
    "physical_max",
    "digital_min",
    "digital_max",
    "transducer_type",
    "prefiltering",
    "source_label",
    "source_transducer_type",
    "source_prefiltering",
    "source_index",
}

# Keys of ``RecordingSliceOut`` and its trimmed ``meta``.
SLICE_KEYS = {
    "hash",
    "original_name",
    "display_name",
    "has_custom_name",
    "file_extension",
    "file_size",
    "stored_hash",
    "content_hash",
    "status",
    "modality",
    "public_source",
    "created_at",
    "deleted_at",
    "meta",
    "t_start",
    "t_end",
    "events",
    "interruptions",
}
SLICE_META_KEYS = {
    "format",
    "duration",
    "data_record_count",
    "data_record_duration",
    "signal_count",
    "discontinuous",
    "channel_layout",
    "unresolved_channel_count",
    "channel_order_version",
    "deidentification_version",
    "annotation_text_preserved",
    "signals",
}

# Values that must never appear in any reader's response, whatever the key set.
FORBIDDEN_KEYS = {"file_hash", "id", "author_id", "author"}

READERS = ["grantee", "token", "peer"]
# The metadata slice takes no share token (the module docstring lists the two token-readable routes).
SLICE_READERS = ["grantee", "peer"]
ROUTE_READERS = [("detail", reader) for reader in READERS] + [("slice", reader) for reader in SLICE_READERS]


@pytest.fixture
def shape(db, user, make_user, tmp_path):
    """One processed recording with a grantee, a share token and a peer granted around it."""
    recording, _meta = _make_recording(user, tmp_path, n_channels=3, stored_name=f"{'5HAPE' * 6}5H.edf")
    grantee = make_user()
    _grant_with_middleware(user, grantee, recording)
    AccessRight.objects.create(
        content_type=AccessRight.objects.filter(object_id=str(recording.pk)).first().content_type,
        object_id=str(recording.pk),
        access_giver=user,
        public_share_token=TOKEN,
        can_read=True,
        apply_middleware=True,
    )
    peer = _make_peer(user)
    _peer_grant(peer, recording, user, apply_middleware=True)
    return {"recording": recording, "author": user, "grantee": grantee, "peer": peer}


def _hash(recording):
    return recording.stored_name.split(".")[0]


def _get(shape_data, reader, route):
    recording = shape_data["recording"]
    base = f"/recordings/api/v1/{_hash(recording)}"
    urls = {"detail": base, "list": "/recordings/api/v1/", "slice": f"{base}/slice?t_start=0&t_end=1"}
    url = urls[route]
    client = Client()
    if reader == "peer":
        with _as_peer(shape_data["peer"]):
            return client.get(url)
    if reader == "token":
        return client.get(url + ("&" if "?" in url else "?") + f"share_token={TOKEN}")
    client.force_login(shape_data[reader])
    return client.get(url)


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _to_milliseconds(dt: datetime) -> datetime:
    """The JSON encoder writes millisecond precision, so an exact comparison rounds the same way."""
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


@pytest.mark.django_db
class TestGranteeVisibleShape:
    @pytest.mark.parametrize("reader", READERS)
    def test_detail_key_set_is_exact(self, shape, reader):
        response = _get(shape, reader, "detail")
        assert response.status_code == 200, response.content
        body = response.json()
        assert set(body) == RECORDING_KEYS, _GIVEN_DATA_TABLE
        assert set(body["meta"]) == META_KEYS, _GIVEN_DATA_TABLE
        assert body["meta"]["signals"], "the fixture recording should carry channel rows"
        for signal in body["meta"]["signals"]:
            assert set(signal) == SIGNAL_KEYS, _GIVEN_DATA_TABLE

    @pytest.mark.parametrize("reader", ["grantee", "peer"])
    def test_listing_item_key_set_is_exact(self, shape, reader):
        response = _get(shape, reader, "list")
        assert response.status_code == 200, response.content
        items = [item for item in response.json() if item["hash"] == _hash(shape["recording"])]
        assert len(items) == 1
        assert set(items[0]) == RECORDING_KEYS, _GIVEN_DATA_TABLE

    @pytest.mark.parametrize("reader", SLICE_READERS)
    def test_slice_key_set_is_exact(self, shape, reader):
        response = _get(shape, reader, "slice")
        assert response.status_code == 200, response.content
        body = response.json()
        assert set(body) == SLICE_KEYS, _GIVEN_DATA_TABLE
        assert set(body["meta"]) == SLICE_META_KEYS, _GIVEN_DATA_TABLE

    @pytest.mark.parametrize(("route", "reader"), [*ROUTE_READERS, ("detail", "author"), ("slice", "author")])
    def test_no_forbidden_key_for_any_reader(self, shape, reader, route):
        body = _get(shape, reader, route).json()
        assert not FORBIDDEN_KEYS & set(body), f"{FORBIDDEN_KEYS & set(body)} served to {reader}"

    @pytest.mark.parametrize(("route", "reader"), ROUTE_READERS)
    def test_created_at_is_month_truncated_for_non_authors(self, shape, reader, route):
        body = _get(shape, reader, route).json()
        served = datetime.fromisoformat(body["created_at"])
        expected = _month_start(shape["recording"].created_at)
        assert served == expected, f"{reader} received {served}, an upload time finer than the month"

    @pytest.mark.parametrize("route", ["detail", "slice"])
    def test_created_at_is_exact_for_the_author(self, shape, route):
        body = _get(shape, "author", route).json()
        assert datetime.fromisoformat(body["created_at"]) == _to_milliseconds(shape["recording"].created_at)

    @pytest.mark.parametrize("reader", READERS)
    def test_author_private_values_are_null_for_non_authors(self, shape, reader):
        body = _get(shape, reader, "detail").json()
        assert body["original_name"] is None
        assert body["processing_error"] is None
        assert body["deleted_at"] is None
        for signal in body["meta"]["signals"]:
            assert signal["source_label"] is None
            assert signal["source_index"] is None

    @pytest.mark.parametrize("reader", ["author", *READERS])
    def test_stored_hash_is_the_digest_of_the_stored_bytes(self, shape, reader):
        body = _get(shape, reader, "detail").json()
        assert body["stored_hash"] == shape["recording"].stored_hash
        assert len(body["stored_hash"]) == 64


@pytest.mark.django_db
class TestStoredHashOfPreservedText:
    """A deliberate change of this contract (2026-09-28): ``stored_hash`` is withheld from a de-identifying reader
    of a recording stored with its annotation text.

    Such a reader receives the file with the text stripped, so the stored file's digest covers bytes they never
    receive. With every other byte in hand they could confirm a guessed annotation offline against it, and where
    ingest changed nothing the digest is also the uploaded file's. The key stays in the response and is served
    empty, as for a release-gated member's digest; the content pin answers 400. Authors and raw readers are
    unaffected.
    """

    @pytest.fixture
    def preserved(self, shape):
        from recordings.models import RecordingMeta

        RecordingMeta.objects.filter(object_id=str(shape["recording"].pk)).update(annotation_text_preserved=True)
        return shape

    @pytest.mark.parametrize(("route", "reader"), [*ROUTE_READERS, ("list", "grantee"), ("list", "peer")])
    def test_withheld_from_every_de_identifying_reader(self, preserved, reader, route):
        response = _get(preserved, reader, route)
        assert response.status_code == 200, response.content
        body = response.json()
        if route == "list":
            (body,) = [item for item in body if item["hash"] == _hash(preserved["recording"])]
        assert body["stored_hash"] == ""

    @pytest.mark.parametrize("route", ["detail", "slice"])
    def test_served_to_the_author(self, preserved, route):
        assert _get(preserved, "author", route).json()["stored_hash"] == preserved["recording"].stored_hash

    def test_served_to_a_raw_grantee(self, preserved, make_user):
        recording = preserved["recording"]
        raw = make_user()
        AccessRight.objects.create(
            content_type=AccessRight.objects.filter(object_id=str(recording.pk)).first().content_type,
            object_id=str(recording.pk),
            access_giver=preserved["author"],
            access_target=raw,
            can_read=True,
            apply_middleware=False,
        )
        preserved["raw"] = raw
        assert _get(preserved, "raw", "detail").json()["stored_hash"] == recording.stored_hash

    @pytest.mark.parametrize("path", ["file", "file/slice?t_start=0&t_end=1"])
    def test_the_content_pin_is_refused_to_a_de_identifying_reader(self, preserved, path):
        recording = preserved["recording"]
        client = Client()
        client.force_login(preserved["grantee"])
        separator = "&" if "?" in path else "?"
        url = f"/recordings/api/v1/{_hash(recording)}/{path}{separator}expect_stored_hash={recording.stored_hash}"
        response = client.get(url)
        assert response.status_code == 400, response.content

    def test_the_content_pin_still_answers_the_author(self, preserved):
        recording = preserved["recording"]
        client = Client()
        client.force_login(preserved["author"])
        response = client.get(f"/recordings/api/v1/{_hash(recording)}/file?expect_stored_hash={recording.stored_hash}")
        assert response.status_code == 200
