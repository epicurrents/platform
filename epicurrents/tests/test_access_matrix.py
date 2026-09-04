"""Cross-cutting access-outcome matrix: caller class × object state × route, asserted in one table.

Each cross-cutting guarantee in this codebase — FAILED-hidden, soft-delete hiding, author-private
fields, sanitised bytes for middleware grants — is enforced per route, which is why the recurring
defect is a rule missed on the route nobody enumerated. This table makes the enumeration
executable: one place lists the expected outcome for every combination that matters, so a new
route or a narrowed check changes a cell here instead of silently diverging.

The table pins recording-scoped surfaces (detail, file, status, annotations, and the annotations
app's generic PK-addressed listing). Extend it
with a row when adding a caller class, a column when adding a surface — the review pass that
walks an app adds its combinations here.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.utils import timezone

from epicurrents.models import AccessRight
from library.models import Dataset, DatasetItem
from recordings.models import Recording
from recordings.tests.test_serve_pipeline_parity import _make_recording_with_meta

TOKEN = "matrix-share-token"


def _grant(recording, giver, *, target=None, token=None, apply_middleware=False):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=target,
        public_share_token=token,
        can_read=True,
        apply_middleware=apply_middleware,
    )


@pytest.fixture
def matrix(db, user, make_user, make_superuser, tmp_path):
    """One recording with the full caller cast granted around it."""
    recording, header_bytes, eeg_data, content = _make_recording_with_meta(user, tmp_path)
    author = user
    _grant(recording, author, target=author)  # the upload-time author row

    grantee = make_user()
    _grant(recording, author, target=grantee, apply_middleware=True)

    raw_grantee = make_user()
    _grant(recording, author, target=raw_grantee, apply_middleware=False)

    _grant(recording, author, token=TOKEN, apply_middleware=True)

    dataset_grantee = make_user()
    dataset = Dataset.objects.create(author=author, name="matrix-set")
    DatasetItem.objects.create(
        dataset=dataset,
        content_type=ContentType.objects.get_for_model(recording, for_concrete_model=False),
        object_id=str(recording.pk),
    )
    AccessRight.objects.create(
        content_type=ContentType.objects.get_for_model(dataset, for_concrete_model=False),
        object_id=str(dataset.pk),
        access_giver=author,
        access_target=dataset_grantee,
        can_read=True,
    )

    unrelated = make_user()
    staff = make_user()
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    superuser = make_superuser()

    return {
        "recording": recording,
        "content": content,
        "callers": {
            "author": author,
            "grantee": grantee,
            "raw_grantee": raw_grantee,
            "dataset_grantee": dataset_grantee,
            "unrelated": unrelated,
            "staff": staff,
            "superuser": superuser,
        },
    }


def _client_for(matrix_data, caller):
    client = Client()
    if caller not in ("anonymous", "token"):
        client.force_login(matrix_data["callers"][caller])
    return client


def _hash(recording):
    return recording.stored_name.split(".")[0]


def _get(matrix_data, caller, route):
    recording = matrix_data["recording"]
    client = _client_for(matrix_data, caller)
    urls = {
        "detail": f"/recordings/api/v1/{_hash(recording)}",
        "file": f"/recordings/api/v1/{_hash(recording)}/file",
        "status": f"/recordings/api/v1/status/{_hash(recording)}",
        "annotations": f"/recordings/api/v1/{_hash(recording)}/annotations",
    }
    if route == "generic_annotations":
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        params = {"target_content_type_id": ct.pk, "target_object_id": str(recording.pk)}
        if caller == "token":
            params["share_token"] = TOKEN
        return client.get("/annotations/api/v1/annotations/", params)
    url = urls[route]
    if caller == "token":
        url += f"?share_token={TOKEN}"
    return client.get(url)


def _set_state(recording, state):
    if state == "failed":
        recording.status = Recording.Status.FAILED
        recording.save(update_fields=["status"])
    elif state == "trashed":
        recording.deleted_at = timezone.now()
        recording.save(update_fields=["deleted_at"])


# (route, state, caller) → expected status code. Cells encode the documented
# invariants: FAILED is 404 for everyone but the author and superusers (the
# status route is the documented polling exemption), soft-deleted is off every
# surface, share tokens read but never see author-private data, and dataset
# membership grants the same read a direct row does. A denied caller on a
# READY recording gets 403, not 404 — the hash is a random token, so
# confirming existence to an authenticated local caller discloses nothing
# useful; the federation inbound surface is the one that collapses every
# no-result outcome to 404, and it has its own contract tests.
#
# The generic_annotations route is the annotations app's PK-addressed list
# endpoint, which enforces hiding purely through the resolver's read-visibility
# gate rather than a hand-rolled check, so its denial is the resolver's 403 on
# every state. That shape is uniform with the surface's own no-permission
# denial (unrelated caller on READY also gets 403), so the response does not
# distinguish FAILED, trashed, and revoked — the failure status itself stays
# undisclosed even though the PK-addressed surface confirms row existence.
MATRIX = [
    # detail
    ("detail", "ready", "author", 200),
    ("detail", "ready", "grantee", 200),
    ("detail", "ready", "raw_grantee", 200),
    ("detail", "ready", "dataset_grantee", 200),
    ("detail", "ready", "token", 200),
    ("detail", "ready", "unrelated", 403),
    ("detail", "ready", "staff", 403),
    ("detail", "ready", "superuser", 200),
    ("detail", "ready", "anonymous", 401),
    ("detail", "failed", "author", 200),
    ("detail", "failed", "grantee", 404),
    ("detail", "failed", "dataset_grantee", 404),
    ("detail", "failed", "token", 404),
    ("detail", "failed", "unrelated", 404),
    ("detail", "failed", "staff", 404),
    ("detail", "failed", "superuser", 200),
    ("detail", "trashed", "author", 404),
    ("detail", "trashed", "grantee", 404),
    ("detail", "trashed", "superuser", 404),
    # file
    ("file", "ready", "author", 200),
    ("file", "ready", "grantee", 200),
    ("file", "ready", "dataset_grantee", 200),
    ("file", "ready", "token", 200),
    ("file", "ready", "unrelated", 403),
    ("file", "ready", "anonymous", 401),
    ("file", "failed", "grantee", 404),
    ("file", "failed", "token", 404),
    ("file", "trashed", "author", 404),
    ("file", "trashed", "grantee", 404),
    # status
    ("status", "ready", "author", 200),
    ("status", "ready", "grantee", 200),
    ("status", "ready", "unrelated", 403),
    ("status", "failed", "author", 200),
    ("status", "failed", "grantee", 404),
    # annotations
    ("annotations", "ready", "author", 200),
    ("annotations", "ready", "grantee", 200),
    ("annotations", "ready", "unrelated", 403),
    ("annotations", "failed", "grantee", 404),
    ("annotations", "trashed", "grantee", 404),
    # generic annotations (annotations app, PK-addressed; gate-enforced)
    ("generic_annotations", "ready", "author", 200),
    ("generic_annotations", "ready", "grantee", 200),
    ("generic_annotations", "ready", "dataset_grantee", 200),
    ("generic_annotations", "ready", "unrelated", 403),
    ("generic_annotations", "ready", "anonymous", 401),
    ("generic_annotations", "failed", "author", 200),
    ("generic_annotations", "failed", "grantee", 403),
    ("generic_annotations", "failed", "superuser", 200),
    ("generic_annotations", "trashed", "grantee", 403),
    ("generic_annotations", "trashed", "author", 403),
]


@pytest.mark.django_db
class TestAccessMatrix:
    @pytest.mark.parametrize(
        ("route", "state", "caller", "expected"),
        MATRIX,
        ids=[f"{r}-{s}-{c}" for r, s, c, _ in MATRIX],
    )
    def test_outcome(self, matrix, route, state, caller, expected):
        _set_state(matrix["recording"], state)
        response = _get(matrix, caller, route)
        assert response.status_code == expected, (
            f"{route} as {caller} with recording {state}: expected {expected}, got {response.status_code}"
        )

    def test_author_private_fields_null_for_every_non_author_reader(self, matrix):
        for caller in ("grantee", "raw_grantee", "dataset_grantee", "token"):
            body = _get(matrix, caller, "detail").json()
            assert body.get("original_name") is None, f"original_name leaked to {caller}"
            assert body.get("processing_error") is None, f"processing_error leaked to {caller}"
        assert _get(matrix, "author", "detail").json().get("original_name") == "parity.edf"

    def test_middleware_grants_receive_sanitised_bytes_raw_grants_do_not(self, matrix):
        raw = matrix["content"]
        for caller in ("grantee", "token"):
            response = _get(matrix, caller, "file")
            assert response.status_code == 200
            body = b"".join(response.streaming_content)
            assert body != raw, f"{caller} with apply_middleware=True received the raw file"
            assert b"X X X X" in body[:88], f"{caller}'s header is not anonymised"
        for caller in ("author", "raw_grantee"):
            response = _get(matrix, caller, "file")
            assert response.status_code == 200
            assert b"".join(response.streaming_content) == raw, f"{caller} should receive raw bytes"
