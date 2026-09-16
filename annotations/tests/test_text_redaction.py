"""Contract tests for the annotation-text rule.

A read grant carrying ``apply_middleware`` de-identifies the bytes a caller
receives: the EDF header loses its patient identification and the annotation text
inside the signal file is replaced by timekeeping records. These rows hold the same
text, so they follow the same decision. See AGENTS.md → *Annotation text follows
``apply_middleware``* and [annotations/redaction.py](../redaction.py).

Every case asserts on a response body rather than on resolved state: the defect
this closes was a serialiser ignoring terms the resolver had already produced
correctly, which a test of the resolver alone cannot see.

The recordings time-slice endpoint is covered here rather than with the recordings
tests because it serves the same rows under the same rule, and the load-bearing
registry names one contract test for it. The federated half of that endpoint is in
[recordings/tests/test_federation.py](../../recordings/tests/test_federation.py),
where the peer-impersonation helpers live.
"""

import ast
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from model_bakery import baker

from annotations.models import Annotation, Code, Event, Interruption, Label
from epicurrents.models import AccessRight

ANNOTATIONS_URL = "/annotations/api/v1/annotations/"
EVENTS_URL = "/annotations/api/v1/events/"
INTERRUPTIONS_URL = "/annotations/api/v1/interruptions/"
LABELS_URL = "/annotations/api/v1/labels/"
DETAIL_SLICE_URL = "/recordings/api/v1/{hash}/slice"
RECORDING_BUNDLES_URL = "/recordings/api/v1/{hash}/annotations"

NINJA_MODULE = Path(__file__).resolve().parents[1] / "api" / "v1" / "ninja.py"
TEXT_SERIALISERS = {"_serialize_annotation", "_serialize_event", "_serialize_label"}


def _ct(obj):
    return ContentType.objects.get_for_model(obj, for_concrete_model=False)


#: The recordings endpoints address a recording by the 32-character prefix of its
#: stored name, so the fixture supplies one of that shape rather than letting baker
#: generate a random string the URL cannot carry.
STORED_HASH = "0123456789ABCDEF0123456789ABCDEF"


def _recording(author):
    from recordings.models import Recording

    return baker.make(
        Recording,
        author=author,
        file_size=1,
        file_extension=".edf",
        stored_name=f"{STORED_HASH}.edf",
        status=Recording.Status.READY,
    )


def _grant(recording, giver, target, *, apply_middleware):
    return AccessRight.objects.create(
        content_type=_ct(recording),
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=target,
        can_read=True,
        apply_middleware=apply_middleware,
    )


def _list(client, url, recording):
    ct = _ct(recording)
    return client.get(f"{url}?target_content_type_id={ct.pk}&target_object_id={recording.pk}")


def _row(payload, object_hash):
    """Return the serialised row with *object_hash*, so order never decides a test."""
    match = [row for row in payload if row["object_hash"] == object_hash]
    assert match, f"no row with object_hash {object_hash} in {payload}"
    return match[0]


@pytest.fixture
def scene(make_user):
    """An owner's recording carrying rows from the owner, the grantee and a machine."""
    from compute.models import RunAnnotation

    owner = make_user(username="text-owner")
    grantee = make_user(username="text-grantee")
    recording = _recording(owner)
    ct = _ct(recording)
    common = {"target_content_type": ct, "target_object_id": str(recording.pk)}

    event = baker.make(
        Event, author=owner, name="Seizure onset", value={"note": "left temporal"}, timestamp=1.0, **common
    )
    label = baker.make(Label, author=owner, name="Reviewed", value={"grade": 2}, **common)
    bundle = baker.make(Annotation, author=owner, content={"text": "clinical summary"}, **common)
    interruption = baker.make(Interruption, author=owner, timestamp=3.0, duration=1.0, **common)
    baker.make(
        Code,
        content_type=_ct(interruption),
        object_id=str(interruption.pk),
        standard="hed",
        value="Artifact",
        meta={"note": "electrode replaced mid-study"},
    )
    baker.make(
        Code,
        content_type=_ct(event),
        object_id=str(event.pk),
        standard="hed",
        value="Event/Seizure",
        meta={"note": "left temporal"},
    )
    own_event = baker.make(Event, author=grantee, name="My own marker", timestamp=2.0, **common)
    machine_event = baker.make(Event, author=owner, name="spike", value={"confidence": 0.9}, timestamp=4.0, **common)
    baker.make(RunAnnotation, event=machine_event)

    return {
        "owner": owner,
        "grantee": grantee,
        "recording": recording,
        "event": event,
        "label": label,
        "bundle": bundle,
        "interruption": interruption,
        "own_event": own_event,
        "machine_event": machine_event,
    }


@pytest.mark.django_db
class TestListsUnderASanitisingGrant:
    def test_another_authors_event_text_is_withheld(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["event"].object_hash)

        assert row["name"] == ""
        assert row["value"] is None
        assert row["text_withheld"] is True
        assert row["timestamp"] == 1.0

    def test_label_and_bundle_text_is_withheld(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        label = _row(_list(client, LABELS_URL, scene["recording"]).json(), scene["label"].object_hash)
        bundle = _row(_list(client, ANNOTATIONS_URL, scene["recording"]).json(), scene["bundle"].object_hash)

        assert label["name"] == ""
        assert label["value"] is None
        assert bundle["text_withheld"] is True
        assert "text" not in bundle

    def test_the_callers_own_rows_keep_their_text(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["own_event"].object_hash)

        assert row["name"] == "My own marker"
        assert row["text_withheld"] is False

    def test_machine_produced_findings_keep_their_text(self, client, scene):
        """A run's findings are computed from the de-identified signal, not copied from it."""
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["machine_event"].object_hash)

        assert row["name"] == "spike"
        assert row["value"] == {"confidence": 0.9}
        assert row["text_withheld"] is False

    def test_a_code_keeps_its_vocabulary_value_but_not_its_free_meta(self, client, scene):
        """A code's value names a concept from a registered vocabulary; its meta names nothing."""
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["event"].object_hash)

        assert len(row["codes"]) == 1
        assert row["codes"][0]["standard"] == "hed"
        assert row["codes"][0]["value"] == "Event/Seizure"
        assert row["codes"][0]["meta"] is None

    def test_an_interruptions_timing_survives_but_its_code_meta_does_not(self, client, scene):
        """An interruption holds no text of its own; the codes hanging off it can."""
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        row = _row(_list(client, INTERRUPTIONS_URL, scene["recording"]).json(), scene["interruption"].object_hash)

        assert row["timestamp"] == 3.0
        assert row["duration"] == 1.0
        assert "name" not in row
        assert row["codes"][0]["value"] == "Artifact"
        assert row["codes"][0]["meta"] is None


@pytest.mark.django_db
class TestCallersWhoSeeEverything:
    def test_a_raw_grant_serves_the_text(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=False)
        client.force_login(scene["grantee"])

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["event"].object_hash)

        assert row["name"] == "Seizure onset"
        assert row["value"] == {"note": "left temporal"}
        assert row["text_withheld"] is False

    def test_a_superuser_sees_the_text(self, client, scene, make_superuser):
        """The resolver's superuser path carries no de-identification, and nothing adds one."""
        client.force_login(make_superuser(username="text-super"))

        row = _row(_list(client, EVENTS_URL, scene["recording"]).json(), scene["event"].object_hash)

        assert row["name"] == "Seizure onset"
        assert row["text_withheld"] is False


@pytest.mark.django_db
class TestTimeSliceEndpoint:
    """The same rows reach a caller through the recordings time-slice endpoint."""

    def _meta(self, recording):
        from recordings.models import RecordingMeta

        return baker.make(
            RecordingMeta,
            content_type=_ct(recording),
            object_id=str(recording.pk),
            data_record_count=10,
            data_record_duration=1.0,
        )

    def _slice(self, client, recording):
        self._meta(recording)
        stored_hash = recording.stored_name.split(".")[0]
        return client.get(f"{DETAIL_SLICE_URL.format(hash=stored_hash)}?t_start=0&t_end=5")

    def test_event_text_is_withheld_under_a_sanitising_grant(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        resp = self._slice(client, scene["recording"])

        assert resp.status_code == 200
        row = _row(resp.json()["events"], scene["event"].object_hash)
        assert row["name"] == ""
        assert row["value"] is None
        assert row["text_withheld"] is True

    def test_event_text_is_served_under_a_raw_grant(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=False)
        client.force_login(scene["grantee"])

        resp = self._slice(client, scene["recording"])

        assert resp.status_code == 200
        row = _row(resp.json()["events"], scene["event"].object_hash)
        assert row["name"] == "Seizure onset"
        assert row["text_withheld"] is False


@pytest.mark.django_db
class TestRecordingBundleEndpoint:
    """`GET /recordings/api/v1/{hash}/annotations` serves the same bundles from its own serialiser."""

    def _bundles(self, client, recording):
        stored_hash = recording.stored_name.split(".")[0]
        return client.get(RECORDING_BUNDLES_URL.format(hash=stored_hash))

    def test_bundle_content_is_withheld_under_a_sanitising_grant(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=True)
        client.force_login(scene["grantee"])

        resp = self._bundles(client, scene["recording"])

        assert resp.status_code == 200
        row = _row(resp.json(), scene["bundle"].object_hash)
        assert row["text_withheld"] is True
        assert row["value"] is None
        assert "text" not in row

    def test_bundle_content_is_served_under_a_raw_grant(self, client, scene):
        _grant(scene["recording"], scene["owner"], scene["grantee"], apply_middleware=False)
        client.force_login(scene["grantee"])

        resp = self._bundles(client, scene["recording"])

        assert resp.status_code == 200
        row = _row(resp.json(), scene["bundle"].object_hash)
        assert row["text_withheld"] is False
        assert row["text"] == "clinical summary"

    def test_the_author_reads_their_own_recordings_bundles(self, client, scene):
        client.force_login(scene["owner"])

        resp = self._bundles(client, scene["recording"])

        assert resp.status_code == 200
        row = _row(resp.json(), scene["bundle"].object_hash)
        assert row["text"] == "clinical summary"


class TestEverySerialiserCallDecides:
    """No call may serialise annotation text without saying whose text it is.

    The three text-bearing serialisers take ``withhold_text`` as a required keyword
    argument, so an omission is a TypeError rather than a silent disclosure. This
    scan is what keeps that true for call sites a behavioural test never reaches —
    a new endpoint, or one added by a project.
    """

    def test_text_serialiser_calls_pass_withhold_text(self):
        tree = ast.parse(NINJA_MODULE.read_text(encoding="utf-8"))
        offenders = [
            f"{node.func.id} at line {node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in TEXT_SERIALISERS
            and not any(keyword.arg == "withhold_text" for keyword in node.keywords)
        ]

        assert not offenders, f"serialiser calls without an explicit decision: {offenders}"
