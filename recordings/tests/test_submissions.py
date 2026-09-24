"""The validating submission path: the gate, the batch endpoints, the pooled ingest run, and the dataset field.

Covers ``recordings.submissions`` (profile registry and every generic check, pinned to what the
de-identifier actually writes), the four ``/submissions/...`` endpoints (who may submit, a refusal
writing nothing, an acceptance writing the spool and a row with no client filename, the audit rows
carrying codes and never a message), ``ingest_pooled_submissions`` (the delay, the system author,
the dataset membership, the deleted file row, the profile's ingest callable, the failure path),
``Dataset.submission_group`` on the PATCH surface, and the ``library.release_dataset`` maintenance
operation.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from activity.models import Activity, ObjectChangeLog
from conftest import patch_json, post_json
from epicurrents.models import AccessRight
from epicurrents.permissions import can_read_object
from epicurrents.system_user import get_system_user
from library.models import Dataset, DatasetItem
from maintenance.operations import get_operation
from recordings.models import Recording, SubmissionBatch, SubmissionFile
from recordings.processors.edf import _build_clean_header, parse_edf_header, parse_signal_infos
from recordings.submissions import (
    BLANK_PATIENT,
    BLANK_RECORDING,
    BLANK_START_DATE,
    BLANK_START_TIME,
    IngestProfile,
    Violation,
    can_submit_to_dataset,
    get_ingest_profile,
    register_ingest_profile,
    reset_ingest_profiles,
    validate_submission,
)
from recordings.tasks import ingest_pooled_submissions
from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

pytestmark = pytest.mark.django_db

BATCHES_URL = "/recordings/api/v1/submissions/batches"

SIGNALS = [
    {"label": "EEG Fp1", "unit": "uV", "phys_min": -100.0, "phys_max": 100.0, "sample_count": 256},
    {"label": "EEG Fp2", "unit": "uV", "phys_min": -100.0, "phys_max": 100.0, "sample_count": 256},
]


def _profile(**overrides) -> IngestProfile:
    fields = {
        "key": "test.pool",
        "channels": ("Fp1", "Fp2"),
        "sampling_rate": 256.0,
        "physical_unit": "uV",
        "physical_min": -100.0,
        "physical_max": 100.0,
        "digital_min": -32768,
        "digital_max": 32767,
        "durations_seconds": (2.0,),
        "required_sidecar_keys": ("profile_version",),
    }
    fields.update(overrides)
    return IngestProfile(**fields)


def _edf(*, n_records=2, signals=None, **header) -> bytes:
    fields = {
        "patient": BLANK_PATIENT,
        "recording": BLANK_RECORDING,
        "startdate": BLANK_START_DATE,
        "starttime": BLANK_START_TIME,
    }
    fields.update(header)
    signals = SIGNALS if signals is None else signals
    return _make_edf_header(signals=signals, n_records=n_records, **fields) + _make_edf_data(
        signals, n_records=n_records
    )


def _sidecar(data: bytes, **extra) -> dict:
    return {"recording_sha256": hashlib.sha256(data).hexdigest(), "profile_version": "1", **extra}


def _codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


@pytest.fixture(autouse=True)
def _registry():
    reset_ingest_profiles()
    yield
    reset_ingest_profiles()


@pytest.fixture
def spool(settings, tmp_path):
    settings.RECORDINGS_SUBMISSION_SPOOL_PATH = str(tmp_path / "spool")
    settings.RECORDINGS_STAGING_PATH = str(tmp_path / "staging")
    settings.RECORDINGS_UPLOAD_PATH = str(tmp_path / "uploads")
    return tmp_path / "spool"


@pytest.fixture
def pool(make_user):
    """A gated dataset with a submission group holding one contributor; returns (dataset, contributor, group)."""
    author = make_user()
    group = Group.objects.create(name="Contributors")
    dataset = Dataset.objects.create(author=author, name="Pool", release_gated=True, submission_group=group)
    contributor = make_user()
    contributor.groups.add(group)
    register_ingest_profile(_profile())
    return dataset, contributor, group


def _client(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _open_batch(client, dataset, profile="test.pool"):
    response = post_json(client, BATCHES_URL, {"dataset": dataset.object_hash, "profile": profile})
    assert response.status_code == 201, response.content
    return response.json()


def _submit(client, batch_hash, data: bytes, sidecar, *, filename="prepared.edf"):
    body = sidecar if isinstance(sidecar, bytes) else json.dumps(sidecar).encode()
    return client.post(
        f"{BATCHES_URL}/{batch_hash}/files",
        {
            "file": SimpleUploadedFile(filename, data, content_type="application/octet-stream"),
            "sidecar": SimpleUploadedFile("prepared.json", body, content_type="application/json"),
        },
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_blank_values_are_what_the_deidentifier_writes(self):
        data = _edf(
            patient="Jane Doe", recording="Startdate 01-JAN-2024 A B C", startdate="01.01.24", starttime="12.00.00"
        )
        header = parse_edf_header(data)
        clean = _build_clean_header(header, parse_signal_infos(data, header))
        assert clean[8:88].decode().strip() == BLANK_PATIENT
        assert clean[88:168].decode().strip() == BLANK_RECORDING
        assert clean[168:176].decode() == BLANK_START_DATE
        assert clean[176:184].decode() == BLANK_START_TIME

    def test_prepared_file_passes(self):
        data = _edf()
        assert validate_submission(_profile(), data, _sidecar(data)) == []

    def test_not_an_edf(self):
        junk = b"not a file at all" * 20
        assert _codes(validate_submission(_profile(), junk, _sidecar(junk))) == {"format"}
        assert _codes(validate_submission(_profile(), b"", _sidecar(b""))) == {"format"}

    @pytest.mark.parametrize(
        "field",
        [
            {"patient": "Jane Doe"},
            {"recording": "Startdate 01-JAN-2024 A B C"},
            {"startdate": "01.01.24"},
            {"starttime": "12.00.00"},
        ],
    )
    def test_identification_must_be_blanked(self, field):
        data = _edf(**field)
        assert "identification" in _codes(validate_submission(_profile(), data, _sidecar(data)))

    def test_annotation_channel_refused(self):
        signals = [*SIGNALS, {"label": "EDF Annotations", "sample_count": 32}]
        data = _edf(signals=signals, reserved="EDF+C")
        assert "annotations" in _codes(validate_submission(_profile(), data, _sidecar(data)))

    def test_truncated_file_refused(self):
        data = _edf()[:-10]
        assert "truncated" in _codes(validate_submission(_profile(), data, _sidecar(data)))

    def test_channel_set_and_order(self):
        data = _edf(signals=list(reversed(SIGNALS)))
        codes = _codes(validate_submission(_profile(), data, _sidecar(data)))
        assert "channels" in codes
        extra = _edf(
            signals=[
                *SIGNALS,
                {"label": "ECG", "unit": "mV", "phys_min": -100.0, "phys_max": 100.0, "sample_count": 256},
            ]
        )
        assert "channels" in _codes(validate_submission(_profile(), extra, _sidecar(extra)))

    def test_rate_unit_and_ranges(self):
        rate = _edf(signals=[{**s, "sample_count": 200} for s in SIGNALS])
        assert "sampling_rate" in _codes(validate_submission(_profile(), rate, _sidecar(rate)))
        unit = _edf(signals=[{**s, "unit": "mV"} for s in SIGNALS])
        assert "unit" in _codes(validate_submission(_profile(), unit, _sidecar(unit)))
        phys = _edf(signals=[{**s, "phys_max": 200.0} for s in SIGNALS])
        assert "range" in _codes(validate_submission(_profile(), phys, _sidecar(phys)))
        dig = _edf(signals=[{**s, "dig_min": -2048, "dig_max": 2047} for s in SIGNALS])
        assert "range" in _codes(validate_submission(_profile(), dig, _sidecar(dig)))

    def test_duration_must_be_one_of_the_fixed_lengths(self):
        data = _edf(n_records=3)
        assert "duration" in _codes(validate_submission(_profile(), data, _sidecar(data)))
        assert "duration" not in _codes(
            validate_submission(_profile(durations_seconds=(2.0, 3.0)), data, _sidecar(data))
        )

    def test_unpinned_profile_checks_nothing_about_the_file(self):
        data = _edf(n_records=3, signals=[{**s, "sample_count": 200, "unit": "mV"} for s in SIGNALS])
        assert validate_submission(IngestProfile(key="loose"), data, _sidecar(data)) == []

    def test_sidecar_must_be_an_object(self):
        data = _edf()
        assert _codes(validate_submission(_profile(), data, ["list"])) == {"sidecar_shape"}
        assert _codes(validate_submission(_profile(), data, None)) == {"sidecar_shape"}

    def test_forbidden_keys_refused_anywhere(self):
        data = _edf()
        top = _sidecar(data, annotations=[])
        assert "sidecar_forbidden_key" in _codes(validate_submission(_profile(), data, top))
        nested = _sidecar(data, events=[{"onset_seconds": 1, "code": "x", "text": "a spike"}])
        violations = validate_submission(_profile(), data, nested)
        assert "sidecar_forbidden_key" in _codes(violations)
        assert any("events[0].text" in v.message for v in violations)
        own = _sidecar(data, centre="here")
        assert "sidecar_forbidden_key" in _codes(
            validate_submission(_profile(forbidden_sidecar_keys=("centre",)), data, own)
        )

    def test_required_keys_and_declared_hash(self):
        data = _edf()
        missing = {"recording_sha256": hashlib.sha256(data).hexdigest()}
        assert "sidecar_missing_key" in _codes(validate_submission(_profile(), data, missing))
        assert "sidecar_missing_key" in _codes(validate_submission(_profile(), data, {"profile_version": "1"}))
        wrong = _sidecar(data)
        wrong["recording_sha256"] = hashlib.sha256(b"other").hexdigest()
        assert "sidecar_hash" in _codes(validate_submission(_profile(), data, wrong))
        upper = _sidecar(data)
        upper["recording_sha256"] = upper["recording_sha256"].upper()
        assert validate_submission(_profile(), data, upper) == []
        not_hex = _sidecar(data)
        not_hex["recording_sha256"] = 12
        assert "sidecar_hash" in _codes(validate_submission(_profile(), data, not_hex))

    def test_profile_validator_runs_after_shape_checks_pass(self):
        seen = []

        def check(sidecar):
            seen.append(sidecar)
            return [Violation("age_band", "unknown band")] if sidecar.get("age_band") == "?" else []

        data = _edf()
        profile = _profile(validate_sidecar=check)
        assert validate_submission(profile, data, _sidecar(data, age_band="?")) == [
            Violation("age_band", "unknown band")
        ]
        assert validate_submission(profile, data, _sidecar(data, age_band="adult")) == []
        assert "sidecar_hash" in _codes(
            validate_submission(profile, data, {"recording_sha256": "00", "profile_version": "1"})
        )
        assert len(seen) == 2

    def test_registry(self):
        register_ingest_profile(_profile())
        assert get_ingest_profile("test.pool") == _profile()
        register_ingest_profile(_profile(sampling_rate=500.0))
        assert get_ingest_profile("test.pool").sampling_rate == 500.0
        assert get_ingest_profile("missing") is None
        with pytest.raises(ValueError):
            register_ingest_profile(IngestProfile(key="not a key!"))


# ---------------------------------------------------------------------------
# Who may submit
# ---------------------------------------------------------------------------


class TestWhoMaySubmit:
    def test_group_member_on_gated_dataset_only(self, pool, make_user, superuser):
        dataset, contributor, group = pool
        assert can_submit_to_dataset(contributor, dataset)
        assert not can_submit_to_dataset(make_user(), dataset)
        assert not can_submit_to_dataset(dataset.author, dataset)
        assert not can_submit_to_dataset(superuser, dataset)
        assert not can_submit_to_dataset(None, dataset)
        dataset.release_gated = False
        assert not can_submit_to_dataset(contributor, dataset)
        dataset.release_gated = True
        dataset.submission_group = None
        assert not can_submit_to_dataset(contributor, dataset)

    def test_endpoints_refuse_outsiders(self, pool, make_user):
        dataset, contributor, _group = pool
        assert (
            Client()
            .post(
                BATCHES_URL,
                json.dumps({"dataset": dataset.object_hash, "profile": "test.pool"}),
                content_type="application/json",
            )
            .status_code
            == 401
        )
        outsider = _client(make_user())
        response = post_json(outsider, BATCHES_URL, {"dataset": dataset.object_hash, "profile": "test.pool"})
        assert response.status_code == 403
        assert SubmissionBatch.objects.count() == 0
        ungated = Dataset.objects.create(author=dataset.author, name="Open", submission_group=dataset.submission_group)
        assert (
            post_json(
                _client(contributor), BATCHES_URL, {"dataset": ungated.object_hash, "profile": "test.pool"}
            ).status_code
            == 403
        )
        assert (
            post_json(_client(contributor), BATCHES_URL, {"dataset": "nope", "profile": "test.pool"}).status_code == 404
        )

    def test_unknown_profile(self, pool):
        dataset, contributor, _group = pool
        response = post_json(_client(contributor), BATCHES_URL, {"dataset": dataset.object_hash, "profile": "other"})
        assert response.status_code == 400
        assert SubmissionBatch.objects.count() == 0


# ---------------------------------------------------------------------------
# Batches and files
# ---------------------------------------------------------------------------


class TestBatchEndpoints:
    def test_open_list_and_read_own_batches(self, pool, make_user):
        dataset, contributor, group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        assert set(batch) == {
            "hash",
            "dataset_hash",
            "profile",
            "pending_count",
            "failed_count",
            "ingested_count",
            "created_at",
        }
        assert batch["dataset_hash"] == dataset.object_hash
        assert batch["pending_count"] == 0
        row = SubmissionBatch.objects.get(object_hash=batch["hash"])
        assert row.contributor == contributor
        activity = Activity.objects.filter(verb="recordings.submission.batch.create").latest("pk")
        assert activity.target_object_id == str(row.pk)

        listing = client.get(BATCHES_URL)
        assert [b["hash"] for b in listing.json()] == [batch["hash"]]
        assert client.get(BATCHES_URL, {"dataset": dataset.object_hash}).json()[0]["hash"] == batch["hash"]
        assert client.get(f"{BATCHES_URL}/{batch['hash']}").json()["hash"] == batch["hash"]

        other = make_user()
        other.groups.add(group)
        other_client = _client(other)
        assert other_client.get(BATCHES_URL).json() == []
        assert other_client.get(f"{BATCHES_URL}/{batch['hash']}").status_code == 404
        assert _submit(other_client, batch["hash"], _edf(), _sidecar(_edf())).status_code == 404

    def test_rejected_file_writes_nothing(self, pool, spool):
        dataset, contributor, _group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        data = _edf(patient="Jane Doe")
        sidecar = _sidecar(data, annotations=[{"text": "Jane had a seizure"}])
        response = _submit(client, batch["hash"], data, sidecar)
        assert response.status_code == 422, response.content
        body = response.json()
        assert body["accepted"] is False
        codes = {v["code"] for v in body["violations"]}
        assert {"identification", "sidecar_forbidden_key"} <= codes
        assert SubmissionFile.objects.count() == 0
        assert not spool.exists() or not any(spool.iterdir())
        activity = Activity.objects.filter(verb="recordings.submission.file.reject").latest("pk")
        assert activity.target_object_id == str(SubmissionBatch.objects.get().pk)
        assert activity.metadata["violation_count"] == len(body["violations"])
        assert set(activity.metadata["violation_codes"]) == codes
        assert "Jane" not in json.dumps(activity.metadata)
        assert client.get(f"{BATCHES_URL}/{batch['hash']}").json()["pending_count"] == 0

    def test_invalid_json_sidecar_is_a_shape_violation(self, pool, spool):
        dataset, contributor, _group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        response = _submit(client, batch["hash"], _edf(), b"{not json")
        assert response.status_code == 422
        assert {v["code"] for v in response.json()["violations"]} == {"sidecar_shape"}
        assert SubmissionFile.objects.count() == 0

    def test_accepted_file_waits_in_the_spool_without_its_filename(self, pool, spool):
        dataset, contributor, _group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        data = _edf()
        response = _submit(client, batch["hash"], data, _sidecar(data), filename="patient-4711.edf")
        assert response.status_code == 202, response.content
        assert response.json() == {"accepted": True, "pending_count": 1}
        row = SubmissionFile.objects.get()
        assert row.status == SubmissionFile.Status.PENDING
        assert row.file_extension == ".edf"
        assert row.file_hash == hashlib.sha256(data).hexdigest()
        assert row.sidecar["profile_version"] == "1"
        assert "patient-4711" not in row.stored_name
        assert "patient-4711" not in json.dumps(row.sidecar)
        files = list(spool.iterdir())
        assert [p.name for p in files] == [row.stored_name]
        assert files[0].read_bytes() == data
        assert Activity.objects.filter(verb="recordings.submission.file.accept").exists()
        assert client.get(f"{BATCHES_URL}/{batch['hash']}").json()["pending_count"] == 1

    def test_only_edf_or_bdf_and_size_cap(self, pool, spool, settings):
        dataset, contributor, _group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        data = _edf()
        assert _submit(client, batch["hash"], data, _sidecar(data), filename="x.csv").status_code == 400
        settings.RECORDINGS_SUBMISSION_MAX_SIZE = 100
        assert _submit(client, batch["hash"], data, _sidecar(data)).status_code == 413
        assert SubmissionFile.objects.count() == 0

    def test_membership_withdrawn_closes_the_batch(self, pool, spool):
        dataset, contributor, group = pool
        client = _client(contributor)
        batch = _open_batch(client, dataset)
        contributor.groups.remove(group)
        data = _edf()
        assert _submit(client, batch["hash"], data, _sidecar(data)).status_code == 403
        assert SubmissionFile.objects.count() == 0

    def test_erasing_the_contributor_keeps_the_batch_unlinked(self, pool):
        dataset, contributor, _group = pool
        batch = SubmissionBatch.objects.create(dataset=dataset, contributor=contributor, profile_key="test.pool")
        contributor.delete()
        batch.refresh_from_db()
        assert batch.contributor is None


# ---------------------------------------------------------------------------
# The pooled run
# ---------------------------------------------------------------------------


_NAMES = itertools.count(1)


def _spooled(pool_fixture, spool, *, age_hours=48, sidecar_extra=None, batch=None):
    dataset, contributor, _group = pool_fixture
    batch = batch or SubmissionBatch.objects.create(dataset=dataset, contributor=contributor, profile_key="test.pool")
    data = _edf()
    spool.mkdir(parents=True, exist_ok=True)
    stored_name = f"{next(_NAMES):032X}.edf"
    path = spool / stored_name
    path.write_bytes(data)
    sidecar = _sidecar(data, **(sidecar_extra or {}))
    row = SubmissionFile.objects.create(
        batch=batch,
        stored_name=stored_name,
        file_extension=".edf",
        file_path=str(path),
        file_size=len(data),
        file_hash=hashlib.sha256(data).hexdigest(),
        sidecar_hash=hashlib.sha256(json.dumps(sidecar, sort_keys=True).encode()).hexdigest(),
        sidecar=sidecar,
    )
    SubmissionFile.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=age_hours))
    row.refresh_from_db()
    return row


class TestPooledIngest:
    def test_nothing_waiting(self):
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 0}
        assert not Activity.objects.filter(verb="recordings.submission.ingest").exists()

    def test_respects_the_pooling_delay(self, pool, spool, settings):
        settings.RECORDINGS_SUBMISSION_POOLING_DELAY_HOURS = 24
        _spooled(pool, spool, age_hours=1)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 0}
        assert SubmissionFile.objects.count() == 1
        assert Recording.objects.count() == 0

    def test_ingests_under_the_system_user_into_the_dataset(
        self, pool, spool, make_user, django_capture_on_commit_callbacks
    ):
        dataset, contributor, _group = pool
        ingested = []
        register_ingest_profile(_profile(ingest=lambda recording, sidecar: ingested.append((recording.pk, sidecar))))
        row = _spooled(pool, spool, sidecar_extra={"age_band": "adult"})
        reader = make_user()
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset),
            object_id=str(dataset.pk),
            access_giver=dataset.author,
            access_target=reader,
            can_read=True,
        )

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            result = ingest_pooled_submissions()
        assert result == {"ingested": 1, "failed": 0}
        assert len(callbacks) == 1

        recording = Recording.objects.get()
        assert recording.author == get_system_user()
        assert recording.stored_name == row.stored_name
        assert recording.file_hash == row.file_hash
        assert recording.original_name == row.stored_name
        assert recording.display_name is None
        assert recording.status == Recording.Status.PENDING
        assert AccessRight.objects.filter(object_id=str(recording.pk), access_target=get_system_user()).exists()
        item = DatasetItem.objects.get(dataset=dataset, object_id=str(recording.pk))
        assert item.release_id is None
        assert not can_read_object(reader, recording)
        assert not can_read_object(contributor, recording)
        assert ingested == [(recording.pk, row.sidecar)]
        assert not SubmissionFile.objects.filter(pk=row.pk).exists()
        assert SubmissionBatch.objects.get().ingested_count == 1
        assert not any(f.name == "batch" or f.name == "submission" for f in Recording._meta.get_fields())
        activity = Activity.objects.get(verb="recordings.submission.ingest")
        assert activity.metadata == {"file_count": 1, "batch_count": 1}
        assert activity.actor is None
        assert activity.target_content_type is None

    def test_takes_every_batch_in_one_run(self, pool, spool, make_user, django_capture_on_commit_callbacks):
        dataset, _contributor, group = pool
        other = make_user()
        other.groups.add(group)
        other_batch = SubmissionBatch.objects.create(dataset=dataset, contributor=other, profile_key="test.pool")
        _spooled(pool, spool)
        _spooled(pool, spool, batch=other_batch)
        _spooled(pool, spool, batch=other_batch)
        with django_capture_on_commit_callbacks(execute=False):
            assert ingest_pooled_submissions() == {"ingested": 3, "failed": 0}
        assert Recording.objects.count() == 3
        assert SubmissionFile.objects.count() == 0
        assert Activity.objects.get(verb="recordings.submission.ingest").metadata["batch_count"] == 2
        assert sorted(SubmissionBatch.objects.values_list("ingested_count", flat=True)) == [1, 2]

    def test_failure_keeps_the_row_for_the_operator(self, pool, spool):
        register_ingest_profile(_profile(ingest=lambda recording, sidecar: 1 / 0))
        row = _spooled(pool, spool)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 1}
        row.refresh_from_db()
        assert row.status == SubmissionFile.Status.FAILED
        assert "ZeroDivisionError" in row.error
        assert Recording.objects.count() == 0
        assert SubmissionBatch.objects.get().ingested_count == 0

    def test_missing_profile_or_file_fails_that_submission_only(self, pool, spool, django_capture_on_commit_callbacks):
        good = _spooled(pool, spool)
        gone = _spooled(pool, spool)
        (spool / gone.stored_name).unlink()
        with django_capture_on_commit_callbacks(execute=False):
            assert ingest_pooled_submissions() == {"ingested": 1, "failed": 1}
        assert not SubmissionFile.objects.filter(pk=good.pk).exists()
        gone.refresh_from_db()
        assert gone.status == SubmissionFile.Status.FAILED
        reset_ingest_profiles()
        stranded = _spooled(pool, spool)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 1}
        stranded.refresh_from_db()
        assert "no longer registered" in stranded.error

    def test_failed_rows_are_purged_after_the_retention_window(self, pool, spool, settings):
        settings.RECORDINGS_SUBMISSION_FAILED_RETENTION_DAYS = 30
        register_ingest_profile(_profile(ingest=lambda recording, sidecar: 1 / 0))
        old = _spooled(pool, spool, age_hours=31 * 24)
        recent = _spooled(pool, spool)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 2}
        SubmissionFile.objects.filter(pk=old.pk).update(status=SubmissionFile.Status.FAILED)
        assert (spool / old.stored_name).exists()
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 0}
        assert not SubmissionFile.objects.filter(pk=old.pk).exists()
        assert not (spool / old.stored_name).exists()
        assert SubmissionFile.objects.filter(pk=recent.pk, status=SubmissionFile.Status.FAILED).exists()
        assert (spool / recent.stored_name).exists()
        purge = Activity.objects.get(verb="recordings.submission.purge")
        assert purge.metadata == {"count": 1, "retention_days": 30}
        assert ObjectChangeLog.objects.filter(activity=purge, action=ObjectChangeLog.ACTION_DELETE).count() == 1

    def test_closed_pool_fails_the_submission(self, pool, spool):
        dataset, _contributor, _group = pool
        row = _spooled(pool, spool)
        Dataset.objects.filter(pk=dataset.pk).update(release_gated=False)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 1}
        row.refresh_from_db()
        assert row.status == SubmissionFile.Status.FAILED
        assert Recording.objects.count() == 0
        Dataset.objects.filter(pk=dataset.pk).update(release_gated=True, deleted_at=timezone.now())
        trashed = _spooled(pool, spool)
        assert ingest_pooled_submissions() == {"ingested": 0, "failed": 1}
        trashed.refresh_from_db()
        assert "no longer an open" in trashed.error

    def test_processing_the_ingested_recording(self, pool, spool, django_capture_on_commit_callbacks):
        _spooled(pool, spool)
        with django_capture_on_commit_callbacks(execute=True):
            ingest_pooled_submissions()
        recording = Recording.objects.get()
        assert recording.status == Recording.Status.READY, recording.processing_error
        assert recording.stored_hash


# ---------------------------------------------------------------------------
# The dataset field and the maintenance operation
# ---------------------------------------------------------------------------


class TestDatasetSubmissionGroup:
    def test_author_sets_and_clears_it(self, make_user):
        author = make_user()
        group = Group.objects.create(name="Centres")
        dataset = Dataset.objects.create(author=author, name="Pool", release_gated=True)
        client = _client(author)
        url = f"/api/v1/library/datasets/{dataset.object_hash}/"
        assert client.get(url).json()["submission_group_id"] is None
        response = patch_json(client, url, {"submission_group_id": group.pk})
        assert response.status_code == 200, response.content
        assert response.json()["submission_group_id"] == group.pk
        dataset.refresh_from_db()
        assert dataset.submission_group == group
        activity = Activity.objects.filter(verb="library.dataset.update").latest("pk")
        assert activity.metadata["fields_updated"] == ["submission_group"]
        assert patch_json(client, url, {"submission_group_id": None}).json()["submission_group_id"] is None
        assert patch_json(client, url, {"submission_group_id": 999999}).status_code == 400
        assert patch_json(client, url, {"name": "Renamed"}).status_code == 200
        dataset.refresh_from_db()
        assert dataset.submission_group is None

    def test_write_grantee_may_not(self, make_user):
        author = make_user()
        grantee = make_user()
        group = Group.objects.create(name="Centres")
        dataset = Dataset.objects.create(author=author, name="Pool", release_gated=True)
        AccessRight.objects.create(
            content_type=ContentType.objects.get_for_model(Dataset),
            object_id=str(dataset.pk),
            access_giver=author,
            access_target=grantee,
            can_read=True,
            can_write=True,
        )
        client = _client(grantee)
        url = f"/api/v1/library/datasets/{dataset.object_hash}/"
        assert patch_json(client, url, {"submission_group_id": group.pk}).status_code == 403
        assert patch_json(client, url, {"name": "Renamed"}).status_code == 200


class TestMaintenanceOperation:
    def test_release_dataset_is_registered(self):
        operation = get_operation("library.release_dataset")
        assert operation is not None
        assert operation.command == "release_dataset"
        assert operation.requires_step_up
        args = operation.args_schema(
            dataset="0" * 32, as_of="2026-11-01", dry_run=True, k=5, m=2, assessment_reference="Assessment v3"
        )
        assert operation.command_args(args) == [
            "0" * 32,
            "--format",
            "json",
            "--as-of",
            "2026-11-01",
            "--dry-run",
            "--k",
            "5",
            "--m",
            "2",
            "--assessment-reference",
            "Assessment v3",
        ]
        with pytest.raises(ValueError):
            operation.args_schema(dataset="not-a-hash")
