"""Media API contract tests.

Covers the core invariants of phase 1 plus the share-token + federation
auth paths added in phase 4:

- Upload extension allowlist (disabled / in-list / out-of-list).
- AccessRight grant to the author on upload.
- File download re-validation when the live allowlist no longer covers
  an already-uploaded extension (project switch scenario).
- GenericFK attachment to a parent Recording — set, read, detach, and
  orphan handling when the parent is purged.
- ``original_name`` PHI gating (visible to author + superuser only).
- Soft-delete excludes the row from listings.
- Anonymous share-token access on detail + download.
- Federated peer access on detail + download.
"""

import io
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client, override_settings

from epicurrents.models import AccessRight
from federation.models import FederatedPeer
from media.models import MediaFile
from recordings.models import Recording

UPLOAD_URL = "/media/api/v1/upload"
LIST_URL = "/media/api/v1/"


def _detail_url(content_hash: str) -> str:
    return f"/media/api/v1/{content_hash}"


def _download_url(content_hash: str) -> str:
    return f"/media/api/v1/{content_hash}/file"


def _make_pdf(name: str = "notes.pdf", data: bytes = b"%PDF-1.4 stub") -> io.BytesIO:
    f = io.BytesIO(data)
    f.name = name
    return f


def _make_mp4(name: str = "clip.mp4", data: bytes = b"\x00\x00\x00\x18ftypmp42abcd") -> io.BytesIO:
    f = io.BytesIO(data)
    f.name = name
    return f


# ── Allowlist enforcement on upload ───────────────────────────────────────────


@pytest.mark.django_db
def test_upload_disabled_when_allowlist_empty(auth_client, tmp_path):
    """Empty allowlist means uploads are disabled entirely (403)."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    assert res.status_code == 403


@pytest.mark.django_db
def test_upload_rejects_extension_outside_allowlist(auth_client, tmp_path):
    """An allowed list of ``[.pdf]`` rejects a markdown upload with 400."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        f = io.BytesIO(b"# heading")
        f.name = "notes.md"
        res = c.post(UPLOAD_URL, {"file": f}, format="multipart")
    assert res.status_code == 400


@pytest.mark.django_db
def test_upload_accepts_extension_in_allowlist(auth_client, tmp_path):
    """Happy-path upload: extension in the list creates a row + file on disk."""
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf", ".md"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["file_extension"] == ".pdf"
    assert body["is_supported"] is True
    media = MediaFile.objects.get(content_hash=body["content_hash"])
    assert media.author_id == user.pk
    assert Path(media.file_path).is_file()


@pytest.mark.django_db
def test_upload_stores_file_under_upload_root_not_staging(auth_client, tmp_path):
    """The stored file lands in MEDIA_UPLOAD_PATH and leaves staging empty.

    A row pointing into the staging directory would tie live data to a
    directory whose name invites cleanup, and in the compose stack staging
    and uploads are separate mounts of the backed-up media volume — only
    the uploads subtree is the permanent tier.
    """
    c, _ = auth_client
    staging_root = tmp_path / "staging"
    upload_root = tmp_path / "uploads"
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(staging_root),
        MEDIA_UPLOAD_PATH=str(upload_root),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart")
    assert res.status_code == 200, res.content
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    stored = Path(media.file_path)
    assert stored.is_file()
    assert stored.parent == upload_root
    assert list(staging_root.iterdir()) == []


@pytest.mark.django_db
def test_upload_grants_full_access_right_to_uploader(auth_client, tmp_path):
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    assert res.status_code == 200
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    ar = AccessRight.objects.filter(content_type=ct, object_id=str(media.pk), access_target=user).first()
    assert ar is not None
    assert ar.can_read and ar.can_write


# ── Download re-validation against the live allowlist ────────────────────────


@pytest.mark.django_db
def test_download_supported_extension_returns_file(auth_client, tmp_path):
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart")
        assert res.status_code == 200
        hash_ = res.json()["content_hash"]
        dl = c.get(_download_url(hash_))
    assert dl.status_code == 200
    assert b"%PDF" in b"".join(dl.streaming_content)


@pytest.mark.django_db
def test_download_returns_410_when_extension_no_longer_allowed(auth_client, tmp_path):
    """Simulates a project switch: file uploaded under .pdf-allowed, then
    the allowlist tightens; download must 410 (not silently serve)."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf", ".md"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart")
        hash_ = res.json()["content_hash"]
    # Project switch — .pdf is no longer in the list.
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".md"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        dl = c.get(_download_url(hash_))
    assert dl.status_code == 410


@pytest.mark.django_db
def test_detail_marks_unsupported_file_with_flag(auth_client, tmp_path):
    """List/detail surface ``is_supported: false`` so the frontend can grey
    out unsupported rows rather than hiding them."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart")
        hash_ = res.json()["content_hash"]
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".md"]):
        body = c.get(_detail_url(hash_)).json()
    assert body["is_supported"] is False


# ── GenericFK attachment ──────────────────────────────────────────────────────


@pytest.fixture
def attached_recording(user):
    """A minimal Recording the user can write, suitable as an attachment target."""
    rec = Recording.objects.create(
        author=user,
        stored_name=("A" * 32) + ".edf",
        original_name="case.edf",
        file_size=1024,
        status=Recording.Status.READY,
    )
    ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(rec.pk),
        access_giver=user,
        access_target=user,
        can_read=True,
        can_write=True,
    )
    return rec


@pytest.mark.django_db
def test_upload_with_recording_attachment(auth_client, tmp_path, attached_recording):
    c, _ = auth_client
    rec_hash = attached_recording.stored_name.split(".", 1)[0]
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        qs = urlencode({"attached_to_type": "recording", "attached_to_id": rec_hash})
        res = c.post(
            f"{UPLOAD_URL}?{qs}",
            {"file": _make_pdf()},
            format="multipart",
        )
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["attached_to"] == {"type": "recording", "id": rec_hash}


@pytest.mark.django_db
def test_upload_with_unknown_attachment_type_returns_400(auth_client, tmp_path):
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        qs = urlencode({"attached_to_type": "starfish", "attached_to_id": "X"})
        res = c.post(
            f"{UPLOAD_URL}?{qs}",
            {"file": _make_pdf()},
            format="multipart",
        )
    assert res.status_code == 400


@pytest.mark.django_db
def test_attachment_orphan_after_recording_purge(auth_client, tmp_path, attached_recording):
    """When the parent Recording is hard-deleted the media file survives,
    and its serialised ``attached_to`` quietly becomes null."""
    c, _ = auth_client
    rec_hash = attached_recording.stored_name.split(".", 1)[0]
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        qs = urlencode({"attached_to_type": "recording", "attached_to_id": rec_hash})
        res = c.post(
            f"{UPLOAD_URL}?{qs}",
            {"file": _make_pdf()},
            format="multipart",
        )
        media_hash = res.json()["content_hash"]
        # Hard-delete the parent (not soft-delete — we want to exercise the
        # orphan-pointer path).
        attached_recording.delete()
        body = c.get(_detail_url(media_hash)).json()
    assert body["attached_to"] is None


# ── Original-name PHI gating ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_original_name_hidden_from_non_author(
    auth_client,
    make_user,
    tmp_path,
    attached_recording,
):
    """A grantee can read the row but ``original_name`` is null."""
    c, _ = auth_client
    other = make_user()
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("private.pdf")}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    # Grant other user read access.
    ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(media.pk),
        access_giver=media.author,
        access_target=other,
        can_read=True,
    )
    from django.test import Client

    other_client = Client()
    other_client.force_login(other)
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        body = other_client.get(_detail_url(media.content_hash)).json()
    assert body["original_name"] is None
    assert body["display_name"]  # falls back to hash prefix


@pytest.mark.django_db
def test_original_name_visible_to_superuser(superuser_client, auth_client, tmp_path):
    c, _ = auth_client
    sc, _ = superuser_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("secret.pdf")}, format="multipart")
        body = sc.get(_detail_url(res.json()["content_hash"])).json()
    assert body["original_name"] == "secret.pdf"


@pytest.mark.django_db
def test_grantee_lists_media_attached_to_readable_recording(
    auth_client,
    make_user,
    tmp_path,
    attached_recording,
):
    """A read-only grantee of a recording can list its attached media.

    Listing must not require write access on the parent (attaching does, listing
    does not), and attachment-inherited media must not be dropped by the
    visibility pre-filter.
    """
    c, owner = auth_client
    rec = attached_recording
    rec_hash = rec.stored_name.split(".", 1)[0]
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        qs = urlencode({"attached_to_type": "recording", "attached_to_id": rec_hash})
        res = c.post(f"{UPLOAD_URL}?{qs}", {"file": _make_pdf("clip.pdf")}, format="multipart")
    media_hash = res.json()["content_hash"]

    reader = make_user(username="rec_reader")
    rec_ct = ContentType.objects.get_for_model(rec, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=rec_ct,
        object_id=str(rec.pk),
        access_giver=owner,
        access_target=reader,
        can_read=True,
        can_write=False,
    )
    from django.test import Client

    reader_client = Client()
    reader_client.force_login(reader)
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        resp = reader_client.get(f"{LIST_URL}?{qs}")
    assert resp.status_code == 200, resp.content
    assert media_hash in [m["content_hash"] for m in resp.json()]

    # A user with no access to the recording sees nothing — no 403, no leak.
    stranger = make_user(username="rec_stranger")
    stranger_client = Client()
    stranger_client.force_login(stranger)
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        resp2 = stranger_client.get(f"{LIST_URL}?{qs}")
    assert resp2.status_code == 200
    assert resp2.json() == []


# ── Soft delete ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_soft_delete_excludes_from_list_and_detail(auth_client, tmp_path):
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
        hash_ = res.json()["content_hash"]
        c.delete(_detail_url(hash_))
        detail = c.get(_detail_url(hash_))
        listing = c.get(LIST_URL)
    assert detail.status_code == 404
    assert all(item["content_hash"] != hash_ for item in listing.json())


@pytest.mark.django_db
def test_group_grant_surfaces_in_list(auth_client, make_user, tmp_path):
    """A media file granted via a group AccessRight appears in the
    grantee's listing — the pre-filter must match group rows, not only
    direct user rows."""
    from django.contrib.auth.models import Group
    from django.contrib.contenttypes.models import ContentType
    from django.test import Client

    from epicurrents.models import AccessRight
    from media.models import MediaFile

    c, author = auth_client
    member = make_user(username="group_member")
    group = Group.objects.create(name="media-readers")
    member.groups.add(group)

    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
        hash_ = res.json()["content_hash"]
        media = MediaFile.objects.get(content_hash=hash_)
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(media, for_concrete_model=False),
            object_id=str(media.pk),
            access_giver=author,
            access_target_group=group,
            can_read=True,
        )
        member_client = Client()
        member_client.force_login(member)
        listing = member_client.get(LIST_URL)
    assert any(item["content_hash"] == hash_ for item in listing.json())


# ── Share-token read access ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_detail_with_valid_share_token_succeeds(auth_client, tmp_path):
    """Anonymous request with a valid ``?share_token=`` reads the detail."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("public.pdf")}, format="multipart")
    assert res.status_code == 200
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(media.pk),
        access_giver=media.author,
        public_share_token="public-token-42",
        can_read=True,
    )
    anon = Client()
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        body = anon.get(
            _detail_url(media.content_hash) + "?share_token=public-token-42",
        )
    assert body.status_code == 200
    assert body.json()["content_hash"] == media.content_hash


@pytest.mark.django_db
def test_download_with_valid_share_token_succeeds(auth_client, tmp_path):
    """Anonymous file fetch with ``?share_token=`` streams the bytes."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf("public.pdf")}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    AccessRight.objects.create(
        content_type=ct,
        object_id=str(media.pk),
        access_giver=media.author,
        public_share_token="dl-token",
        can_read=True,
    )
    anon = Client()
    with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        dl = anon.get(_download_url(media.content_hash) + "?share_token=dl-token")
    assert dl.status_code == 200
    assert b"%PDF" in b"".join(dl.streaming_content)


@pytest.mark.django_db
def test_detail_without_auth_or_share_token_returns_401(auth_client, tmp_path):
    """No session, no FederatedBearer, no share_token → 401 (not 404)."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    anon = Client()
    body = anon.get(_detail_url(res.json()["content_hash"]))
    assert body.status_code == 401


@pytest.mark.django_db
def test_detail_with_wrong_share_token_returns_404(auth_client, tmp_path):
    """A share_token that exists but doesn't grant this media → 404, not 401."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    anon = Client()
    body = anon.get(_detail_url(res.json()["content_hash"]) + "?share_token=wrong")
    assert body.status_code == 404


# ── Video: upload, time_offset, range + inline serving, log dedup ─────────────


@contextmanager
def _video_project(tmp_path):
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".mp4"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        yield


def _upload_video(client, tmp_path, *, time_offset=None, attached_to_id=None):
    params = {"media_type": "video"}
    if time_offset is not None:
        params["time_offset"] = time_offset
    if attached_to_id is not None:
        params["attached_to_type"] = "recording"
        params["attached_to_id"] = attached_to_id
    qs = urlencode(params)
    return client.post(f"{UPLOAD_URL}?{qs}", {"file": _make_mp4()}, format="multipart")


@pytest.mark.django_db
def test_video_upload_records_type_and_time_offset(auth_client, tmp_path):
    c, _ = auth_client
    with _video_project(tmp_path):
        res = _upload_video(c, tmp_path, time_offset=12.5)
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["media_type"] == "video"
    assert body["time_offset"] == 12.5
    media = MediaFile.objects.get(content_hash=body["content_hash"])
    assert media.time_offset == 12.5


@pytest.mark.django_db
def test_video_upload_attached_to_recording(auth_client, tmp_path, attached_recording):
    c, _ = auth_client
    rec_hash = attached_recording.stored_name.split(".", 1)[0]
    with _video_project(tmp_path):
        res = _upload_video(c, tmp_path, time_offset=0, attached_to_id=rec_hash)
    body = res.json()
    assert body["attached_to"] == {"type": "recording", "id": rec_hash}
    assert body["time_offset"] == 0.0


@pytest.mark.django_db
def test_video_download_is_inline_with_video_content_type(auth_client, tmp_path):
    c, _ = auth_client
    with _video_project(tmp_path):
        hash_ = _upload_video(c, tmp_path).json()["content_hash"]
        dl = c.get(_download_url(hash_))
    assert dl.status_code == 200
    assert dl["Content-Type"] == "video/mp4"
    assert dl["Accept-Ranges"] == "bytes"
    assert dl["Content-Disposition"].startswith("inline")


@pytest.mark.django_db
def test_document_download_stays_attachment(auth_client, tmp_path):
    """Regression: enabling inline video must not flip documents to inline."""
    c, _ = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        hash_ = c.post(UPLOAD_URL, {"file": _make_pdf("a.pdf")}, format="multipart").json()["content_hash"]
        dl = c.get(_download_url(hash_))
    assert dl["Content-Disposition"].startswith("attachment")
    assert dl["Accept-Ranges"] == "bytes"


@pytest.mark.django_db
def test_download_range_request_returns_206(auth_client, tmp_path):
    c, _ = auth_client
    with _video_project(tmp_path):
        hash_ = _upload_video(c, tmp_path).json()["content_hash"]
        media = MediaFile.objects.get(content_hash=hash_)
        full = Path(media.file_path).read_bytes()
        dl = c.get(_download_url(hash_), HTTP_RANGE="bytes=0-3")
    assert dl.status_code == 206
    assert dl["Content-Range"] == f"bytes 0-3/{len(full)}"
    assert b"".join(dl.streaming_content) == full[:4]


@pytest.mark.django_db
def test_download_unsatisfiable_range_returns_416(auth_client, tmp_path):
    c, _ = auth_client
    with _video_project(tmp_path):
        hash_ = _upload_video(c, tmp_path).json()["content_hash"]
        dl = c.get(_download_url(hash_), HTTP_RANGE="bytes=99999-")
    assert dl.status_code == 416


@pytest.mark.django_db
def test_mid_file_range_seek_is_not_logged(auth_client, tmp_path):
    """A seek (range start > 0) must not write a media.download audit row;
    a full download or a bytes=0- range (playback start) is logged once."""
    from activity.models import Activity

    c, _ = auth_client
    with _video_project(tmp_path):
        hash_ = _upload_video(c, tmp_path).json()["content_hash"]

        Activity.objects.filter(verb="media.download").delete()
        seek = c.get(_download_url(hash_), HTTP_RANGE="bytes=4-")
        assert seek.status_code == 206
        assert not Activity.objects.filter(verb="media.download").exists()

        start = c.get(_download_url(hash_), HTTP_RANGE="bytes=0-")
        assert start.status_code == 206
        assert Activity.objects.filter(verb="media.download").count() == 1


@pytest.mark.django_db
def test_time_offset_set_and_cleared_via_patch(auth_client, tmp_path):
    c, _ = auth_client
    with _video_project(tmp_path):
        hash_ = _upload_video(c, tmp_path).json()["content_hash"]
        # Set.
        set_res = c.patch(
            _detail_url(hash_),
            data={"time_offset": 7.25},
            content_type="application/json",
        )
        assert set_res.json()["time_offset"] == 7.25
        # Clear with explicit null.
        clear_res = c.patch(
            _detail_url(hash_),
            data={"time_offset": None},
            content_type="application/json",
        )
        assert clear_res.json()["time_offset"] is None
        # Omitting the field leaves the (now-null) value unchanged.
        noop = c.patch(
            _detail_url(hash_),
            data={"display_name": "Clip A"},
            content_type="application/json",
        )
    assert noop.json()["time_offset"] is None
    assert noop.json()["display_name"] == "Clip A"


# ── Federation ────────────────────────────────────────────────────────────────

# Mirroring the recordings federation test pattern: patch
# ``media.api.v1.ninja._try_federated_auth`` to return ``(peer, remote_user_id)``
# instead of forging JWTs in every test. JWT verification itself is covered
# by the federation auth contract tests.
_FED_AUTH_MOCK = "media.api.v1.ninja._try_federated_auth"


@contextmanager
def _as_peer(peer, remote_user_id="user42"):
    with patch(_FED_AUTH_MOCK, return_value=(peer, remote_user_id)):
        yield


def _make_peer(user):
    return FederatedPeer.objects.create(
        url="https://peer.example.com",
        display_name="Test Peer",
        public_key="A" * 43,
        is_trusted=True,
        added_by=user,
    )


def _federated_grant(peer, media, giver, remote_user_id="user42"):
    ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(media.pk),
        access_giver=giver,
        federated_peer=peer,
        remote_user_id=remote_user_id,
        can_read=True,
    )


@pytest.mark.django_db
def test_detail_federated_access_granted(auth_client, tmp_path):
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    peer = _make_peer(user)
    _federated_grant(peer, media, giver=user)

    anon = Client()
    with _as_peer(peer), override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        body = anon.get(_detail_url(media.content_hash))
    assert body.status_code == 200


@pytest.mark.django_db
def test_detail_federated_access_denied_without_grant(auth_client, tmp_path):
    """Peer authenticates but no matching AccessRight → 403."""
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    peer = _make_peer(user)
    # No grant.

    anon = Client()
    with _as_peer(peer):
        body = anon.get(_detail_url(media.content_hash))
    assert body.status_code == 403


@pytest.mark.django_db
def test_download_federated_access_granted(auth_client, tmp_path):
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    peer = _make_peer(user)
    _federated_grant(peer, media, giver=user)

    anon = Client()
    with _as_peer(peer), override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
        dl = anon.get(_download_url(media.content_hash))
    assert dl.status_code == 200
    assert b"%PDF" in b"".join(dl.streaming_content)


# ── Federation inbound_check_object reaches MediaFile ────────────────────────


@pytest.mark.django_db
def test_federation_inbound_check_resolves_mediafile(auth_client, tmp_path):
    """``GET /federation/api/v1/inbound/objects/{ct_id}/{pk}/`` already works
    for MediaFile via its GenericFK-based AccessRight surface — no
    federation-side changes needed in phase 4."""
    c, user = auth_client
    with override_settings(
        MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"],
        MEDIA_STAGING_PATH=str(tmp_path / "staging"),
        MEDIA_UPLOAD_PATH=str(tmp_path / "uploads"),
    ):
        res = c.post(UPLOAD_URL, {"file": _make_pdf()}, format="multipart")
    media = MediaFile.objects.get(content_hash=res.json()["content_hash"])
    peer = _make_peer(user)
    _federated_grant(peer, media, giver=user)

    media_ct = ContentType.objects.get_for_model(media, for_concrete_model=False)
    inbound = f"/api/v1/federation/inbound/objects/{media_ct.pk}/{media.pk}/"

    anon = Client()
    with patch("federation.api.v1.ninja._require_federation_auth", return_value=(peer, "user42")):
        with override_settings(MEDIA_ALLOWED_UPLOAD_EXTENSIONS=[".pdf"]):
            body = anon.get(inbound)
    assert body.status_code == 200
    assert body.json()["content_type_id"] == media_ct.pk
