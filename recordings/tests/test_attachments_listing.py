"""The attachments listing: what is attached to each named recording, for what the caller may read.

A recording is named by its hash and the listing names it back the same way; its sequential key never appears. A
recording the caller cannot read, or that is hidden from them, lists nothing and is not told apart from one with
nothing attached. Each attachment is decided by its own read rule, and carries the pass the platform applied to it.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.models import Activity
from epicurrents.models import AccessRight
from media.models import MediaFile
from recordings.api.v1.ninja import ATTACHMENT_LISTING_LIMIT
from recordings.models import Recording

URL = "/recordings/api/v1/attachments/"


def _recording(author, letter="A", status=Recording.Status.READY):
    """A recording with its author's own read right, which an upload writes alongside it."""
    rec = Recording.objects.create(
        author=author,
        stored_name=(letter * 32) + ".edf",
        original_name="case.edf",
        file_size=1024,
        status=status,
    )
    _grant(rec, author, access_target=author)
    return rec


def _media(author, parent, letter="M", name="Report", media_type=MediaFile.MediaType.DOCUMENT):
    media = MediaFile.objects.create(
        author=author,
        media_type=media_type,
        original_name="MRN12345-report.pdf",
        display_name=name,
        stored_name=(letter * 32) + ".pdf",
        file_extension=".pdf",
        file_size=4,
        file_path="/tmp/report.pdf",
        file_hash="x" * 64,
        content_hash=letter.lower() * 32,
        attachment_content_type=ContentType.objects.get_for_model(parent, for_concrete_model=False),
        attachment_object_id=str(parent.pk),
    )
    return media


def _grant(obj, giver, **target):
    return AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(obj, for_concrete_model=False),
        object_id=str(obj.pk),
        access_giver=giver,
        can_read=True,
        **target,
    )


@pytest.mark.django_db
class TestAttachmentsListing:
    def test_author_reads_the_attachments_of_their_recording(self, client, user):
        rec = _recording(user)
        _media(user, rec)
        client.force_login(user)
        items = client.get(URL, {"recordings": "a" * 32}).json()
        assert items == [
            {
                "parent": "A" * 32,
                "type": "mediafile",
                "hash": "m" * 32,
                "name": "Report",
                "platform_pass": "none",
                "details": {
                    "file_extension": ".pdf",
                    "is_supported": items[0]["details"]["is_supported"],
                    "media_type": "document",
                    "time_offset": None,
                },
            }
        ]

    def test_grantee_of_the_recording_reads_its_attachments(self, client, user, make_user):
        reader = make_user()
        rec = _recording(user)
        _media(user, rec)
        _grant(rec, user, access_target=reader)
        client.force_login(reader)
        assert [i["hash"] for i in client.get(URL, {"recordings": "A" * 32}).json()] == ["m" * 32]

    def test_a_recording_the_caller_cannot_read_lists_nothing(self, client, user, make_user):
        rec = _recording(user)
        _media(user, rec)
        client.force_login(make_user())
        response = client.get(URL, {"recordings": "A" * 32})
        assert response.status_code == 200
        assert response.json() == []

    def test_a_failed_recording_lists_nothing_to_a_grantee(self, client, user, make_user):
        reader = make_user()
        rec = _recording(user, status=Recording.Status.FAILED)
        _media(user, rec)
        _grant(rec, user, access_target=reader)
        client.force_login(reader)
        assert client.get(URL, {"recordings": "A" * 32}).json() == []

    def test_trashed_media_is_not_listed(self, client, user):
        from django.utils import timezone

        rec = _recording(user)
        media = _media(user, rec)
        MediaFile.objects.filter(pk=media.pk).update(deleted_at=timezone.now())
        client.force_login(user)
        assert client.get(URL, {"recordings": "A" * 32}).json() == []

    def test_items_follow_the_named_order_and_name_within_a_recording(self, client, user):
        first, second = _recording(user, "A"), _recording(user, "B")
        _media(user, second, "N", name="zeta")
        _media(user, first, "M", name="beta")
        _media(user, first, "P", name="Alpha")
        client.force_login(user)
        items = client.get(URL, {"recordings": f"{'A' * 32},{'B' * 32}"}).json()
        assert [(i["parent"][0], i["name"]) for i in items] == [("A", "Alpha"), ("A", "beta"), ("B", "zeta")]

    def test_no_sequential_key_or_author_private_field_is_served(self, client, user):
        rec = _recording(user)
        media = _media(user, rec)
        client.force_login(user)
        response = client.get(URL, {"recordings": "A" * 32})
        body = response.content.decode()
        assert "MRN12345" not in body
        assert "x" * 64 not in body
        # The exact keys, each one a hash, a label or a property of the file: there is no key a primary key could
        # travel under. Searching the values for the number instead proves nothing, since a small key equals a bool.
        (item,) = response.json()
        assert set(item) == {"parent", "type", "hash", "name", "platform_pass", "details"}
        assert set(item["details"]) == {"file_extension", "is_supported", "media_type", "time_offset"}
        assert (item["parent"], item["hash"]) == ("A" * 32, media.content_hash)

    def test_share_token_reads_the_attachments_of_the_shared_recording(self, client, user):
        rec = _recording(user)
        _media(user, rec)
        _grant(rec, user, public_share_token="tok-attach")
        items = client.get(URL, {"recordings": "A" * 32, "share_token": "tok-attach"}).json()
        assert [i["hash"] for i in items] == ["m" * 32]

    def test_no_session_and_no_token_is_refused(self, client, user):
        assert client.get(URL, {"recordings": "A" * 32}).status_code == 401

    def test_a_signed_in_reader_presenting_a_token_reads_through_it(self, client, user, make_user):
        rec = _recording(user)
        _media(user, rec)
        _grant(rec, user, public_share_token="tok-attach")
        client.force_login(make_user())
        assert client.get(URL, {"recordings": "A" * 32}).json() == []
        items = client.get(URL, {"recordings": "A" * 32, "share_token": "tok-attach"}).json()
        assert [i["hash"] for i in items] == ["m" * 32]

    @pytest.mark.parametrize("value", ["", "short", ",".join(["A" * 32] * (ATTACHMENT_LISTING_LIMIT + 1))])
    def test_malformed_or_oversized_lists_are_refused(self, client, user, value):
        client.force_login(user)
        assert client.get(URL, {"recordings": value}).status_code == 400

    def test_a_full_list_fits_the_request_line_and_is_answered(self, client, user):
        from urllib.parse import urlencode

        rec = _recording(user)
        _media(user, rec)
        names = ["A" * 32] + [f"{i:032X}" for i in range(1, ATTACHMENT_LISTING_LIMIT)]
        query = urlencode({"recordings": ",".join(names)})
        # gunicorn's default limit_request_line, which a production deployment runs with.
        assert len(f"GET {URL}?{query} HTTP/1.1") < 4094
        client.force_login(user)
        response = client.get(f"{URL}?{query}")
        assert response.status_code == 200
        assert [i["hash"] for i in response.json()] == ["m" * 32]

    def test_a_reupload_under_one_hash_names_the_newest_recording(self, client, user):
        from datetime import timedelta

        older = _recording(user)
        Recording.objects.filter(pk=older.pk).update(stored_name=("A" * 32) + ".bdf")
        newer = _recording(user)
        Recording.objects.filter(pk=older.pk).update(created_at=newer.created_at - timedelta(days=1))
        _media(user, older, "N", name="old")
        _media(user, newer, "M", name="new")
        client.force_login(user)
        assert [i["name"] for i in client.get(URL, {"recordings": "A" * 32}).json()] == ["new"]

    def test_the_audit_row_carries_counts_only(self, client, user):
        rec = _recording(user)
        _media(user, rec)
        client.force_login(user)
        client.get(URL, {"recordings": "A" * 32})
        row = Activity.objects.filter(verb="recordings.attachments.list").latest("pk")
        assert row.metadata == {
            "recordings_named": 1,
            "recordings_readable": 1,
            "attachment_count": 1,
            "share_token_used": False,
        }


@pytest.mark.django_db
class TestGatedMembers:
    """A member of a release-gated dataset the caller may not see lists nothing, as a recording that does not exist.

    The listing's form of the 404 the per-recording endpoints answer through ``_hidden_for_caller``: it names no
    recording that fails, so a hidden one contributes nothing and reads as one with nothing attached.
    """

    def _member(self, author, *, released):
        from library.tests.test_release import _gated_dataset, _release

        rec = _recording(author)
        _media(author, rec)
        dataset, items = _gated_dataset(author, rec)
        if released:
            _release(dataset, *items)
        return rec, dataset

    def test_an_unreleased_member_lists_nothing_to_a_reader_of_the_dataset(self, client, user, make_user):
        reader = make_user()
        rec, dataset = self._member(user, released=False)
        _grant(dataset, user, access_target=reader)
        _grant(rec, user, access_target=reader)
        client.force_login(reader)
        response = client.get(URL, {"recordings": "A" * 32})
        assert response.status_code == 200
        assert response.json() == []

    def test_a_released_member_lists_to_a_reader_of_the_dataset(self, client, user, make_user):
        reader = make_user()
        _rec, dataset = self._member(user, released=True)
        _grant(dataset, user, access_target=reader)
        client.force_login(reader)
        assert [i["hash"] for i in client.get(URL, {"recordings": "A" * 32}).json()] == ["m" * 32]

    @pytest.mark.parametrize("released", [True, False])
    def test_no_member_lists_for_a_share_token(self, client, user, released):
        rec, dataset = self._member(user, released=released)
        _grant(rec, user, public_share_token="tok-member")
        _grant(dataset, user, public_share_token="tok-pool")
        for token in ("tok-member", "tok-pool"):
            response = client.get(URL, {"recordings": "A" * 32, "share_token": token})
            assert response.status_code == 200
            assert response.json() == []

    def test_a_token_hides_a_released_member_from_a_signed_in_reader_too(self, client, user, make_user):
        reader = make_user()
        rec, dataset = self._member(user, released=True)
        _grant(dataset, user, access_target=reader)
        _grant(rec, user, public_share_token="tok-member")
        client.force_login(reader)
        assert client.get(URL, {"recordings": "A" * 32, "share_token": "tok-member"}).json() == []
        row = Activity.objects.filter(verb="recordings.attachments.list").latest("pk")
        assert (row.metadata["recordings_readable"], row.metadata["share_token_used"]) == (0, True)


@pytest.mark.django_db
class TestTypeServingNoShareToken:
    """A type whose endpoints take no token is listed by the session's own access, whatever token is presented."""

    @pytest.fixture(autouse=True)
    def tokenless_media(self, monkeypatch):
        from dataclasses import replace

        from epicurrents import attachments

        media_type = attachments._TYPES["mediafile"]
        monkeypatch.setattr(attachments, "_TYPES", {"mediafile": replace(media_type, serves_share_tokens=False)})

    def _shared(self, author):
        rec = _recording(author)
        _media(author, rec)
        _grant(rec, author, public_share_token="tok-attach")
        return rec

    def test_not_listed_to_a_token_reader_without_an_account(self, client, user):
        self._shared(user)
        assert client.get(URL, {"recordings": "A" * 32, "share_token": "tok-attach"}).json() == []

    def test_not_listed_to_a_signed_in_reader_who_holds_only_the_token(self, client, user, make_user):
        self._shared(user)
        client.force_login(make_user())
        assert client.get(URL, {"recordings": "A" * 32, "share_token": "tok-attach"}).json() == []
        # The recording itself was readable through the token; only the attachment was decided without it.
        row = Activity.objects.filter(verb="recordings.attachments.list").latest("pk")
        assert (row.metadata["recordings_readable"], row.metadata["attachment_count"]) == (1, 0)

    def test_listed_to_a_signed_in_reader_with_access_of_their_own(self, client, user, make_user):
        reader = make_user()
        rec = self._shared(user)
        _grant(rec, user, access_target=reader)
        client.force_login(reader)
        items = client.get(URL, {"recordings": "A" * 32, "share_token": "tok-attach"}).json()
        assert [i["hash"] for i in items] == ["m" * 32]
