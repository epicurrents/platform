"""Tests for the ingest privacy overrides a project turns on to keep file-borne
identifiers out of the database.

Both settings default off, and both exist for a project whose data-protection
position is that no patient personal data reaches the platform at all. The
position is not that these settings anonymise anything — the client does that
before upload — but that the platform stops *retaining* what the client was
supposed to have removed, so a recording arriving some other way does not
silently falsify the claim.

The defaults are as load-bearing as the enabled behaviour: a platform that
discarded filenames or file-borne annotations by default would be destroying
information every other project legitimately wants.
"""

import io
import itertools
import pathlib
from datetime import UTC, datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from annotations.models import Annotation, Interruption
from recordings.pipelines import RecordingPipeline
from recordings.tasks import _save_edf_results


class _Anno:
    def __init__(self, onset, duration, label):
        self.onset = onset
        self.duration = duration
        self.label = label


class _Header:
    data_record_count = 10
    data_record_duration = 1.0
    signal_count = 1
    discontinuous = False
    data_format = "EDF"


class _Result:
    """Minimal stand-in for the EDF processing result `_save_edf_results` consumes."""

    def __init__(self, annotations=None, gaps=None):
        self.header = _Header()
        self.signal_infos = []
        self.annotations = annotations or []
        self.gaps = gaps or {}


@pytest.fixture
def recording(db, user):
    return baker.make("recordings.Recording", author=user, status="READY")


def _call_body(text: str, open_paren: int) -> str:
    """Return the argument text of the call whose ``(`` is at *open_paren*.

    Balanced rather than a fixed-size window: a window long enough for today's
    call sites is a scan that stops working when one grows, and it fails by
    finding nothing rather than by erroring.
    """
    depth = 0
    for pos in range(open_paren, len(text)):
        if text[pos] == "(":
            depth += 1
        elif text[pos] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : pos]
    return text[open_paren + 1 :]


_stem_counter = itertools.count()


#: A module-level pipeline instance, as an operator supplies one. A dotted path
#: in ``RECORDING_PIPELINES`` resolving to an instance is returned as given
#: rather than copied, which makes this the object the ingest task must not
#: write through.
SHARED_PIPELINE = RecordingPipeline()


def _process_one_preserving(user, tmp_path, secret: bytes) -> bytes:
    """Run the ingest task with ``preserve_annotations=True`` and return the stored bytes.

    The file is a real EDF+ carrying *secret* as annotation text, because the
    property under test is whether that text survives into the stored file. A
    plain EDF has no annotation channel, so asserting on its bytes would hold
    whatever the gate did.
    """
    import hashlib
    from unittest.mock import patch

    from recordings.models import Recording
    from recordings.tasks import process_recording
    from recordings.tests.test_edf_processor import _make_edfplus_file

    content = _make_edfplus_file(tals_per_record=[[b"+0.0\x14" + secret + b"\x14\x00"]])
    staging = tmp_path / "staging"
    uploads = tmp_path / "uploads"
    staging.mkdir(exist_ok=True)
    uploads.mkdir(exist_ok=True)

    stem = f"{next(_stem_counter):032d}"
    staged = staging / f"{stem}.edf"
    staged.write_bytes(content)
    recording = Recording.objects.create(
        author=user,
        original_name="t.edf",
        stored_name=f"{stem}.edf",
        file_extension=".edf",
        file_size=len(content),
        file_path=str(staged),
        file_hash=hashlib.sha256(content).hexdigest(),
        content_hash="",
        status=Recording.Status.PENDING,
    )

    with (
        override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
        patch("notifications.tasks.send_push_to_user.delay"),
    ):
        process_recording(recording.pk, preserve_annotations=True)

    recording.refresh_from_db()
    return pathlib.Path(recording.file_path).read_bytes()


def _annotation_names(recording):
    return set(Annotation.objects.filter(target_object_id=str(recording.pk)).values_list("name", flat=True))


@pytest.mark.django_db
class TestDiscardEmbeddedAnnotations:
    def test_off_by_default_the_file_annotations_are_kept(self, recording):
        _save_edf_results(recording, _Result(annotations=[_Anno(1.0, 0.5, "photic 10 Hz")]))
        assert "Original annotations" in _annotation_names(recording)

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_on_no_annotation_row_is_written(self, recording):
        _save_edf_results(recording, _Result(annotations=[_Anno(1.0, 0.5, "photic 10 Hz")]))
        assert _annotation_names(recording) == set()

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_the_discarded_text_is_nowhere_in_the_row_set(self, recording):
        # Asserting the row is absent is weaker than asserting the content is:
        # a future change that kept a "structural only" annotation carrying the
        # original labels would pass the test above and defeat the setting.
        secret = "Pt. reports aura, ref. Dr Hansen"
        _save_edf_results(recording, _Result(annotations=[_Anno(1.0, 0.5, secret)]))
        blob = "".join(str(a.content) for a in Annotation.objects.filter(target_object_id=str(recording.pk)))
        assert secret not in blob

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_gaps_are_still_recorded_because_they_are_geometry(self, recording):
        # A gap carries no text and the viewer and compute layer read data
        # positions derived from it. Discarding gaps would protect nobody and
        # would put every event after the first splice on the wrong signal.
        _save_edf_results(recording, _Result(gaps={4.0: 2.0}))
        interruptions = Interruption.objects.filter(target_object_id=str(recording.pk))
        assert interruptions.count() == 1
        assert interruptions.first().duration == 2.0

    # ── The converted-file route ──────────────────────────────────────────
    # A Nicolet .e file carries its events in a sidecar rather than in the EDF
    # the converter produces, so they reach the database through
    # save_sidecar_events and never through _save_edf_results. The setting is
    # documented as covering both; only the EDF one was tested, and deleting the
    # sidecar gate left the whole suite green.

    _SIDECAR = {
        "annotations": [{"onset_seconds": 1.0, "duration_seconds": 0.0, "text": "Pt. aura, ref. Dr Hansen"}],
        "events": [{"onset_seconds": 2.0, "duration_seconds": 0.0, "type": "Photic", "label": "10 Hz"}],
    }

    def test_off_by_default_the_sidecar_events_are_kept(self, recording):
        from recordings.converters.sidecar import save_sidecar_events

        save_sidecar_events(recording, self._SIDECAR)
        assert "Source events" in _annotation_names(recording)

    @override_settings(RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS=True)
    def test_on_the_sidecar_events_are_discarded_too(self, recording):
        from recordings.converters.sidecar import save_sidecar_events

        save_sidecar_events(recording, self._SIDECAR)
        blob = "".join(str(a.content) for a in Annotation.objects.filter(target_object_id=str(recording.pk)))
        assert _annotation_names(recording) == set()
        assert "Hansen" not in blob
        # The vendor vocabulary matters as much as the free text: it identifies
        # the acquisition software and through it the recording laboratory.
        assert "Photic" not in blob


@pytest.mark.django_db
class TestDiscardOriginalName:
    """The filename is replaced before the row is written, not scrubbed after.

    Exercised through the upload endpoint rather than by calling a helper,
    because the property is about what reaches the database on the real path.
    """

    def _upload(self, client, tmp_path, filename="Hansen_Ola_1950-12-12.edf"):
        f = io.BytesIO(b"fake edf data")
        f.name = filename
        with override_settings(
            RECORDINGS_STAGING_PATH=str(tmp_path / "staging"),
            RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
        ):
            (tmp_path / "staging").mkdir(parents=True, exist_ok=True)
            (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
            return client.post("/recordings/api/v1/upload", {"file": f}, format="multipart")

    def test_off_by_default_the_filename_is_kept(self, auth_client, tmp_path):
        from recordings.models import Recording

        client, user = auth_client
        resp = self._upload(client, tmp_path)
        assert resp.status_code == 202, resp.content
        rec = Recording.objects.filter(author=user).latest("created_at")
        assert rec.original_name == "Hansen_Ola_1950-12-12.edf"

    def test_on_the_patient_name_never_reaches_the_row(self, auth_client, tmp_path):
        from recordings.models import Recording

        client, user = auth_client
        with override_settings(RECORDINGS_DISCARD_ORIGINAL_NAME=True):
            resp = self._upload(client, tmp_path)
        assert resp.status_code == 202, resp.content
        rec = Recording.objects.filter(author=user).latest("created_at")
        assert "Hansen" not in rec.original_name
        assert rec.original_name.startswith("upload-")
        # The extension has to survive: Content-Disposition is built from
        # display_name plus file_extension, and the ingest pipeline rewrites the
        # stem when a conversion changes the format.
        assert rec.original_name.endswith(".edf")

    def test_the_replacement_is_a_usable_timestamp(self, auth_client, tmp_path):
        from recordings.models import Recording

        client, user = auth_client
        with override_settings(RECORDINGS_DISCARD_ORIGINAL_NAME=True):
            self._upload(client, tmp_path)
        rec = Recording.objects.filter(author=user).latest("created_at")
        stamp = rec.original_name[len("upload-") : -len(".edf")]
        # The Z is not decoration: the stamp is UTC, and parsing it back as aware
        # is what shows the name is a timestamp rather than merely timestamp-shaped.
        parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        assert abs((timezone.now() - parsed).total_seconds()) < 300

    def test_the_response_does_not_echo_the_discarded_name(self, auth_client, tmp_path):
        # The endpoint returns original_name to the author. If the discard only
        # reached the database the caller would still be handed the patient
        # name back, and a client that logs its own responses would store it.
        client, _ = auth_client
        with override_settings(RECORDINGS_DISCARD_ORIGINAL_NAME=True):
            resp = self._upload(client, tmp_path)
        assert "Hansen" not in resp.content.decode()


@pytest.mark.django_db
class TestDiscardSourceChannelMetadata:
    """The captured originals go; the cleaned values stay.

    The distinction is the whole point. `label`, `transducer_type` and
    `prefiltering` describe what the stored file actually contains and every
    reader needs them. The `source_*` columns are the pre-cleaning originals,
    kept so an uploader can see what their own file held — and they are the one
    place a raw submission would be preserved rather than merely stripped.
    """

    def _signal(self, **overrides):
        """A parsed channel carrying both the cleaned values and the captured originals."""
        from recordings.processors.edf import EdfSignalInfo

        signal = EdfSignalInfo(
            label="Fp1",
            transducer_type="",
            physical_unit="uV",
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            prefiltering="HP:0.5Hz LP:70Hz",
            sample_count=256,
            reserved="",
        )
        signal.source_label = "EEG Fp1-REF"
        signal.source_transducer_type = "AgAgCl cup electrodes"
        signal.source_prefiltering = "HP 0.5 Hz; LP 70 Hz; N 50"
        signal.source_index = 7
        for key, value in overrides.items():
            setattr(signal, key, value)
        return signal

    def test_off_by_default_the_originals_are_kept(self, recording):
        from recordings.models import SignalInfo

        result = _Result()
        result.signal_infos = [self._signal()]
        result.header.signal_count = 1
        _save_edf_results(recording, result)
        row = SignalInfo.objects.get()
        assert row.source_label == "EEG Fp1-REF"
        assert row.source_index == 7

    @override_settings(RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA=True)
    def test_on_no_original_header_field_is_stored(self, recording):
        from recordings.models import SignalInfo

        result = _Result()
        result.signal_infos = [self._signal()]
        result.header.signal_count = 1
        _save_edf_results(recording, result)
        row = SignalInfo.objects.get()
        assert row.source_label == ""
        assert row.source_transducer_type == ""
        assert row.source_prefiltering == ""
        assert row.source_index is None

    @override_settings(RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA=True)
    def test_the_cleaned_values_survive(self, recording):
        # The sanitised values the EDF writer produced are what the stored file
        # holds; dropping them would make the recording unreadable rather than
        # private.
        from recordings.models import SignalInfo

        result = _Result()
        result.signal_infos = [self._signal()]
        result.header.signal_count = 1
        _save_edf_results(recording, result)
        row = SignalInfo.objects.get()
        assert row.label == "Fp1"
        assert row.prefiltering == "HP:0.5Hz LP:70Hz"

    @override_settings(RECORDINGS_DISCARD_SOURCE_CHANNEL_METADATA=True)
    def test_a_metadata_refresh_does_not_reacquire_them(self, recording):
        # The refresh path rebuilds these rows and carries the previous ones'
        # source fields forward, so it is a second way the originals could
        # reappear on a deployment that had discarded them.
        from recordings.metadata import _create_signal_rows
        from recordings.models import SignalInfo

        meta = baker.make(
            "recordings.RecordingMeta",
            content_type=ContentType.objects.get_for_model(recording),
            object_id=str(recording.pk),
        )
        carried = {0: ("Fp1", ("EEG Fp1-REF", "AgAgCl", "HP 0.5", 7))}
        _create_signal_rows(meta, [self._signal()], source_fields=carried)
        row = SignalInfo.objects.filter(meta=meta).get()
        assert row.source_label == ""
        assert row.source_index is None


@pytest.mark.django_db
class TestPreserveAnnotationsProhibition:
    """Stripping annotation text from the stored file is already the default.

    This setting is the difference between a default and a prohibition: it is
    refused visibly at the endpoint so a client wired to send the flag finds
    out, and ignored at the point of use so the refusal holds for every route
    into processing — the API, the import command, or a project calling the
    processing task directly.
    """

    def _upload(self, client, tmp_path, preserve):
        f = io.BytesIO(b"fake edf data")
        f.name = "sample.edf"
        with override_settings(
            RECORDINGS_STAGING_PATH=str(tmp_path / "staging"),
            RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
        ):
            (tmp_path / "staging").mkdir(parents=True, exist_ok=True)
            (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
            # Everything but `file` binds as a query parameter — only `file` is
            # declared File(...) — so a body field would never reach the view.
            url = f"/recordings/api/v1/upload?preserve_annotations={str(preserve).lower()}"
            return client.post(url, {"file": f}, format="multipart")

    def test_allowed_by_default(self, auth_client, tmp_path):
        client, _ = auth_client
        assert self._upload(client, tmp_path, True).status_code == 202

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_refused_when_the_deployment_forbids_it(self, auth_client, tmp_path):
        client, _ = auth_client
        resp = self._upload(client, tmp_path, True)
        assert resp.status_code == 400
        assert "preserve_annotations" in resp.content.decode()

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_an_ordinary_upload_still_works(self, auth_client, tmp_path):
        # The prohibition must refuse the request, not the endpoint.
        client, _ = auth_client
        assert self._upload(client, tmp_path, False).status_code == 202

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_the_refused_upload_leaves_no_row_and_no_staged_file(self, auth_client, tmp_path, user):
        # A 400 raised after the file is staged would otherwise leak a file the
        # deployment just refused to accept on those terms.
        from recordings.models import Recording

        client, _ = auth_client
        before = Recording.objects.filter(author=user).count()
        self._upload(client, tmp_path, True)
        assert Recording.objects.filter(author=user).count() == before
        staged = list((tmp_path / "staging").glob("*"))
        assert staged == [], staged


@pytest.mark.django_db
class TestPreserveAnnotationsRefusalOnEveryRoute:
    """The prohibition has to hold for callers that never touch the API.

    The endpoint's 400 is the visible half. These are the other two routes into
    processing, named in the design and — until now — untested: the bulk-import
    command, and a project invoking the processing task directly. A prohibition
    that only covers the route with a test is a default wearing a prohibition's
    name.
    """

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_the_import_command_refuses_rather_than_silently_stripping(self, user, tmp_path):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        # A real directory and a real user: the refusal sits after the command's
        # own validity checks, so a bad path would mask it.
        source = tmp_path / "incoming"
        source.mkdir()
        with pytest.raises(CommandError, match="not permitted"):
            call_command(
                "import_recordings",
                str(source),
                "--username",
                user.get_username(),
                "--preserve-annotations",
            )

    def test_the_import_command_allows_it_by_default(self, user, tmp_path):
        # The other half: the refusal is the deployment's choice, not the norm.
        from django.core.management import call_command

        source = tmp_path / "incoming"
        source.mkdir()
        call_command(
            "import_recordings",
            str(source),
            "--username",
            user.get_username(),
            "--preserve-annotations",
        )

    @override_settings(RECORDINGS_ALLOW_PRESERVE_ANNOTATIONS=False)
    def test_the_task_ignores_the_flag_a_direct_caller_passed(self, user, tmp_path):
        # The backstop. A project calling process_recording itself bypasses the
        # endpoint entirely, so the refusal cannot live only there.
        #
        # Asserted against the bytes on disk. The first version of this test
        # recomputed the task's own condition inline and asserted the result was
        # False, which is a statement about `and` rather than about the task:
        # deleting the deployment gate from recordings/tasks.py left the whole
        # recordings suite green.
        secret = b"Seizure onset, left temporal"
        stored = _process_one_preserving(user, tmp_path, secret)
        assert secret not in stored, "a direct caller's preserve_annotations must not take effect"

    def test_a_direct_caller_may_preserve_when_the_deployment_allows_it(self, user, tmp_path):
        # The other half, and the reason the assertion above is not vacuous: with
        # the deployment permitting it, the same call keeps the text.
        secret = b"Seizure onset, left temporal"
        stored = _process_one_preserving(user, tmp_path, secret)
        assert secret in stored, "the flag must still work where it is permitted"


@pytest.mark.django_db
class TestPipelineTuningCannotEscapeTheRun:
    """A per-run pipeline change must not outlive the run.

    `get_pipeline` used to hand back module-level singletons. The ingest task
    sets `header.strip_annotation_text = False` when an upload asks for its
    annotations preserved, and a Celery worker is long-lived — so one such
    upload flipped the shared "web" pipeline and every later recording that
    worker processed kept its embedded annotation text. That text is clinical
    free text the platform otherwise strips, the affected recordings never
    asked for it, and nothing recorded that they had inherited it.

    Found when a test that ran the import command with --preserve-annotations
    made an unrelated test fail later in the same session.
    """

    def test_two_calls_return_independent_objects(self):
        from recordings.pipelines import get_pipeline

        first = get_pipeline("web")
        second = get_pipeline("web")
        assert first is not second
        assert first.header is not second.header

    def test_mutating_one_does_not_reach_the_next_caller(self):
        from recordings.pipelines import get_pipeline

        mutated = get_pipeline("web")
        mutated.header.strip_annotation_text = False
        assert get_pipeline("web").header.strip_annotation_text is True

    @override_settings(RECORDING_PIPELINES={"web": "recordings.tests.test_ingest_privacy_overrides.SHARED_PIPELINE"})
    def test_the_task_does_not_mutate_an_operator_supplied_pipeline(self, user, tmp_path):
        # The route the copy does not cover, and the reason the task keeps its
        # own local instead of writing through the object. get_pipeline copies
        # only the built-ins; a dotted path resolving to a module-level instance
        # is returned as given and outlives every run in a long-lived worker, so
        # writing to it here would flip stripping off for every later recording
        # the worker handled. The copy cannot help — this pipeline is never one.
        assert SHARED_PIPELINE.header.strip_annotation_text is True
        _process_one_preserving(user, tmp_path, b"Seizure onset, left temporal")
        assert SHARED_PIPELINE.header.strip_annotation_text is True, (
            "the task wrote through the shared pipeline; every later recording "
            "this worker handles would keep its annotation text"
        )

    def test_the_default_survives_an_import_run_that_preserved_annotations(self, user, tmp_path):
        # The regression in the shape it actually occurred: a caller legitimately
        # turns stripping off for its own run, and the next caller must not
        # inherit it.
        from django.core.management import call_command

        from recordings.pipelines import get_pipeline

        source = tmp_path / "incoming"
        source.mkdir()
        call_command(
            "import_recordings",
            str(source),
            "--username",
            user.get_username(),
            "--preserve-annotations",
        )
        assert get_pipeline("web").header.strip_annotation_text is True
        assert get_pipeline("import").header.strip_annotation_text is True


@pytest.mark.django_db
class TestTheWorkerDoesNotCarryPreservationForward:
    """The regression in the shape it occurred: the ingest task, run twice.

    `process_recording` sets `header.strip_annotation_text = False` when an
    upload asks for its annotations preserved. A Celery worker is a long-lived
    process handling one task after another, so the question is not whether the
    first recording keeps its annotations — it asked to — but whether the second
    one, which did not ask, inherits the setting.

    The unit-level isolation tests above cover the mechanism. This covers the
    path that actually leaked.
    """

    def _pending(self, user, staging_dir, content, stem):
        import hashlib

        from recordings.models import Recording

        staged = staging_dir / f"{stem}.edf"
        staged.write_bytes(content)
        return Recording.objects.create(
            author=user,
            original_name="test.edf",
            stored_name=f"{stem}.edf",
            file_extension=".edf",
            file_size=len(content),
            file_path=str(staged),
            file_hash=hashlib.sha256(content).hexdigest(),
            content_hash="",
            status=Recording.Status.PENDING,
        )

    def test_a_preserving_run_does_not_change_what_the_next_run_strips(self, user, tmp_path):
        from unittest.mock import patch

        from recordings.pipelines import get_pipeline
        from recordings.tasks import process_recording
        from recordings.testing import make_edf_bytes

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        content = make_edf_bytes()

        first = self._pending(user, staging, content, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        second = self._pending(user, staging, content, "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            # The first upload asks to keep its annotations, as it is entitled to.
            process_recording(first.pk, preserve_annotations=True)
            # The second asks for nothing, and must get the platform default.
            process_recording(second.pk)

        assert get_pipeline("web").header.strip_annotation_text is True, (
            "the shared pipeline was mutated by the first run and the next recording would inherit it"
        )

        # The pipeline state above is a proxy for what matters. The artifact
        # itself is asserted separately, below, against a file that actually
        # carries annotation text — this fixture is a plain EDF with no
        # annotation channel, so an assertion on its bytes would be vacuous.


@pytest.mark.django_db
class TestTheFilenameRuleCoversEveryRoute:
    """`RECORDINGS_DISCARD_ORIGINAL_NAME` shipped covering one route of two.

    It was enforced in the upload endpoint only. `import_recordings` wrote the
    real filename unconditionally, so a deployment that had turned the setting
    on — and documented it as load-bearing for its data-protection position —
    still put patient filenames in the database for every bulk-imported
    recording. Found by the phi-exposure review agent, not by the tests that
    shipped with the setting, which only ever exercised the endpoint.
    """

    def test_the_import_command_honours_the_setting(self, user, tmp_path):
        from django.core.management import call_command

        from recordings.models import Recording
        from recordings.testing import make_edf_bytes

        source = tmp_path / "incoming"
        source.mkdir()
        (source / "Hansen_Ola_1950-12-12.edf").write_bytes(make_edf_bytes())

        with override_settings(
            RECORDINGS_DISCARD_ORIGINAL_NAME=True,
            RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads"),
        ):
            (tmp_path / "uploads").mkdir(exist_ok=True)
            call_command("import_recordings", str(source), "--username", user.get_username())

        names = list(Recording.objects.filter(author=user).values_list("original_name", flat=True))
        assert names, "the import produced no recording to check"
        assert all("Hansen" not in n for n in names), names
        assert all(n.startswith("upload-") for n in names), names

    def test_the_import_command_keeps_the_filename_by_default(self, user, tmp_path):
        from django.core.management import call_command

        from recordings.models import Recording
        from recordings.testing import make_edf_bytes

        source = tmp_path / "incoming"
        source.mkdir()
        (source / "study-042.edf").write_bytes(make_edf_bytes())

        with override_settings(RECORDINGS_UPLOAD_PATH=str(tmp_path / "uploads")):
            (tmp_path / "uploads").mkdir(exist_ok=True)
            call_command("import_recordings", str(source), "--username", user.get_username())

        names = list(Recording.objects.filter(author=user).values_list("original_name", flat=True))
        assert names == ["study-042.edf"], names

    def test_no_creation_site_assigns_the_filename_without_the_helper(self):
        """A source scan, because the defect was a route nobody thought about.

        Scoped to Recording *creation*: an extension rewrite on an existing row,
        the model field declaration, and the preservation manifest all mention
        original_name legitimately and must not be flagged. What must not happen
        is a new route creating a Recording with a filename that never passed
        through the rule — which is precisely how the import command shipped,
        with no behavioural test noticing because the tests exercised the one
        route that did.
        """
        import re
        from pathlib import Path

        # Every way a row can come into being, not just the one the defect used:
        # a direct `Recording(...)` construction and a get_or_create `defaults`
        # dict reach the field as surely as `objects.create` does, and a scan
        # that recognises one form of several is the shape of the defect itself.
        creation = re.compile(
            r"(?<![.\w])Recording(?:\.objects\.(?:create|get_or_create|update_or_create|bulk_create))?\("
        )
        root = Path(__file__).resolve().parents[2]
        offenders = []
        for path in list(root.glob("recordings/**/*.py")) + list(root.glob("projects/**/*.py")):
            if "/tests/" in str(path):
                continue
            text = path.read_text()
            for match in creation.finditer(text):
                block = _call_body(text, match.end() - 1)
                # Both spellings: a keyword argument, and the dict-literal form a
                # get_or_create `defaults=` takes. Matching only the first would
                # leave the scan blind to the one call shape that has to spell
                # the field as a string.
                assigned = re.search(r"""original_name\s*=\s*([^,\n]+)|["']original_name["']\s*:\s*([^,\n}]+)""", block)
                if assigned is None:
                    continue
                value = (assigned.group(1) or assigned.group(2)).strip()
                if value.startswith("stored_original_name(") or value == "name_for_db":
                    continue
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line} -> original_name={value}")
        assert not offenders, (
            "every Recording creation must take its filename from stored_original_name(): " + "; ".join(offenders)
        )


@pytest.mark.django_db
class TestTheStoredFileItselfLosesTheAnnotationText:
    """The artifact assertion the pipeline-state tests are a proxy for.

    Uses an EDF+ carrying real annotation text, because asserting on the bytes
    of a plain EDF that never had an annotation channel proves nothing. The
    first recording is allowed to keep its text; the second must not inherit
    that, and the check is the absence of the text in the file on disk rather
    than the state of a shared object.
    """

    def test_the_second_recording_does_not_keep_the_first_ones_permission(self, user, tmp_path):
        import hashlib
        from unittest.mock import patch

        from recordings.models import Recording
        from recordings.tasks import process_recording
        from recordings.tests.test_edf_processor import _make_edfplus_file

        secret = b"Seizure onset, left temporal"
        content = _make_edfplus_file(tals_per_record=[[b"+0.0\x14" + secret + b"\x14\x00"]])

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()

        def pending(stem):
            staged = staging / f"{stem}.edf"
            staged.write_bytes(content)
            return Recording.objects.create(
                author=user,
                original_name="t.edf",
                stored_name=f"{stem}.edf",
                file_extension=".edf",
                file_size=len(content),
                file_path=str(staged),
                file_hash=hashlib.sha256(content).hexdigest(),
                content_hash="",
                status=Recording.Status.PENDING,
            )

        first = pending("C" * 32)
        second = pending("D" * 32)

        with (
            override_settings(RECORDINGS_STAGING_PATH=str(staging), RECORDINGS_UPLOAD_PATH=str(uploads)),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(first.pk, preserve_annotations=True)
            process_recording(second.pk)

        second.refresh_from_db()
        stored = pathlib.Path(second.file_path).read_bytes()
        assert secret not in stored, "the second recording kept clinical annotation text it never asked to keep"
