"""Contract tests for ingest-time channel-block de-identification.

The stored file is the artifact every serving path reads — raw-file grants, the proxy offload, and
federated serving all assume its header carries no site fingerprint. These tests pin the cleaning
transform itself, its placement in ``process_edf_file`` (the written bytes), the author-private
``source_*`` capture through persistence and the API, and the preservation of that capture across
``refresh_signal_metadata`` (which cannot re-derive it from the cleaned file). Design rationale in
docs/engineering-notes/channel-deidentification-plan.md.
"""

import json
import tempfile
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType

from epicurrents.models import AccessRight
from recordings.models import Recording, SignalInfo
from recordings.processors.edf import (
    deidentify_signal_infos,
    format_prefiltering,
    parse_edf_header,
    parse_prefiltering,
    parse_signal_infos,
    process_edf_file,
)
from recordings.tests.test_edf_processor import _make_edf_data, _make_edf_header

DETAIL_URL = "/recordings/api/v1/{hash}"

# A channel set exercising each cleaning branch: a resolvable EEG label with a
# reference, an unresolvable vendor label, and a vendor-formatted prefiltering
# string the parser does not recognise.
FIXTURE_SIGNALS = [
    {
        "label": "EEG Fp1-Cz",
        "transducer": "AgAgCl sintered VendorCorp",
        "prefiltering": "HP:0.1Hz LP:75Hz",
        "sample_count": 16,
    },
    {
        "label": "XYZ99",
        "transducer": "VendorCorp DC box",
        "prefiltering": "0.53-70 Hz",
        "sample_count": 16,
    },
]


def _parse_fixture(signals=None):
    """Build fixture header bytes and return the parsed signal info list."""
    header_bytes = _make_edf_header(signals=signals or FIXTURE_SIGNALS)
    header = parse_edf_header(header_bytes)
    return parse_signal_infos(header_bytes, header)


class TestFormatPrefiltering:
    def test_round_trips_through_parse(self):
        assert parse_prefiltering(format_prefiltering(0.5, 70.0, 50.0)) == (0.5, 70.0, 50.0)

    def test_unset_components_omitted(self):
        assert format_prefiltering(0.1, 0.0, 0.0) == "HP:0.1Hz"

    def test_all_unset_yields_empty(self):
        assert format_prefiltering(0.0, 0.0, 0.0) == ""

    def test_integral_values_have_no_trailing_zeros(self):
        assert format_prefiltering(0.0, 75.0, 50.0) == "LP:75Hz N:50Hz"


class TestDeidentifySignalInfos:
    def test_resolved_label_replaced_with_canonical(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert infos[0].label == "Fp1"

    def test_unresolved_label_replaced_with_misc(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert infos[1].label == "MISC_1"

    def test_misc_numbering_is_positional(self):
        signals = [
            {"label": "XYZ99", "sample_count": 16},
            {"label": "Fp1", "sample_count": 16},
            {"label": "QQQ7", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert [s.label for s in infos] == ["MISC_1", "Fp1", "MISC_2"]

    def test_annotation_channel_label_untouched(self):
        signals = [
            {"label": "Fp1", "sample_count": 16},
            {"label": "EDF Annotations", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert infos[1].label == "EDF Annotations"

    def test_transducer_blanked_on_every_channel(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert all(s.transducer_type == "" for s in infos)

    def test_recognised_prefiltering_reconstructed(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert infos[0].prefiltering == "HP:0.1Hz LP:75Hz"

    def test_unrecognised_prefiltering_dropped(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert infos[1].prefiltering == ""

    def test_source_fields_capture_originals(self):
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        assert infos[0].source_label == "EEG Fp1-Cz"
        assert infos[0].source_transducer_type == "AgAgCl sintered VendorCorp"
        assert infos[0].source_prefiltering == "HP:0.1Hz LP:75Hz"
        assert infos[1].source_label == "XYZ99"
        assert infos[1].source_prefiltering == "0.53-70 Hz"

    def test_mixed_reference_collision_keeps_references(self):
        """Fp1-A1 + Fp1-A2 must not both become Fp1 — the file would carry duplicate labels."""
        signals = [
            {"label": "EEG Fp1-A1", "sample_count": 16},
            {"label": "EEG Fp1-A2", "sample_count": 16},
            {"label": "EEG Fp2-A2", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert [s.label for s in infos] == ["Fp1-A1", "Fp1-A2", "Fp2"]

    def test_true_duplicate_channels_demoted_to_misc(self):
        signals = [
            {"label": "EEG Fp1-A1", "sample_count": 16},
            {"label": "EEG Fp1-A1", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert infos[0].label == "Fp1-A1"
        assert infos[1].label == "MISC_1"

    def test_no_duplicate_eeg_labels_survive_cleaning(self):
        signals = [
            {"label": "EEG Fp1-A1", "sample_count": 16},
            {"label": "EEG Fp1-A2", "sample_count": 16},
            {"label": "EEG Fp1-Cz", "sample_count": 16},
            {"label": "Fp1", "sample_count": 16},
            {"label": "XYZ99", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        labels = [s.label for s in infos]
        assert len(labels) == len(set(labels))

    def test_primed_labels_normalise_to_the_base_electrode(self):
        """A montage whose only quirk is prime notation must not collapse to MISC_<n>."""
        signals = [
            {"label": "EEG C3'", "sample_count": 16},
            {"label": "EEG C4'", "sample_count": 16},
            {"label": "Fp1'", "sample_count": 16},
            {"label": "EEG O1'-A1'", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert [s.label for s in infos] == ["C3", "C4", "Fp1", "O1"]
        assert all(s.signal_type == "eeg" for s in infos)
        # The primed originals survive as author-private provenance.
        assert [s.source_label for s in infos] == ["EEG C3'", "EEG C4'", "Fp1'", "EEG O1'-A1'"]

    def test_prime_and_unprimed_coexistence_falls_into_duplicate_handling(self):
        """C3 + C3' in one file collide after normalisation; the standard demotion applies."""
        signals = [
            {"label": "C3", "sample_count": 16},
            {"label": "C3'", "sample_count": 16},
        ]
        infos = _parse_fixture(signals)
        deidentify_signal_infos(infos)
        assert [s.label for s in infos] == ["C3", "MISC_1"]

    def test_misc_names_embed_no_electrode_tokens(self):
        """MISC3 contained the substring C3; the underscored form must never do that."""
        from recordings.processors.channel_labels import _TEN_TEN_ELECTRODES

        for n in range(1, 41):
            name = f"MISC_{n}".upper()
            hits = [e for e in _TEN_TEN_ELECTRODES if e.upper() in name]
            assert not hits, f"MISC_{n} embeds {hits}"

    def test_second_pass_is_stable(self):
        """Re-cleaning already-clean infos reproduces the same public values."""
        infos = _parse_fixture()
        deidentify_signal_infos(infos)
        first = [(s.label, s.transducer_type, s.prefiltering) for s in infos]
        deidentify_signal_infos(infos)
        assert [(s.label, s.transducer_type, s.prefiltering) for s in infos] == first


class TestProcessFileChannelCleaning:
    """The written header bytes — the contract every raw-file serving path relies on."""

    def _process_fixture(self, signals=None):
        header_bytes = _make_edf_header(signals=signals or FIXTURE_SIGNALS)
        data_bytes = _make_edf_data(signals or FIXTURE_SIGNALS)
        with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as tmp:
            tmp.write(header_bytes + data_bytes)
            path = Path(tmp.name)
        try:
            result = process_edf_file(path)
            on_disk = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        header = parse_edf_header(on_disk)
        return result, parse_signal_infos(on_disk, header)

    def test_labels_cleaned_on_disk(self):
        _, disk_infos = self._process_fixture()
        assert [s.label for s in disk_infos] == ["Fp1", "MISC_1"]

    def test_transducer_blank_on_disk(self):
        _, disk_infos = self._process_fixture()
        assert all(s.transducer_type == "" for s in disk_infos)

    def test_prefiltering_canonical_on_disk(self):
        _, disk_infos = self._process_fixture()
        assert disk_infos[0].prefiltering == "HP:0.1Hz LP:75Hz"
        assert disk_infos[1].prefiltering == ""

    def test_raw_values_absent_from_written_file(self):
        header_bytes = _make_edf_header(signals=FIXTURE_SIGNALS)
        data_bytes = _make_edf_data(FIXTURE_SIGNALS)
        with tempfile.NamedTemporaryFile(suffix=".edf", delete=False) as tmp:
            tmp.write(header_bytes + data_bytes)
            path = Path(tmp.name)
        try:
            process_edf_file(path)
            on_disk = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        assert b"VendorCorp" not in on_disk
        assert b"XYZ99" not in on_disk
        assert b"0.53-70" not in on_disk

    def test_returned_infos_describe_the_disk(self):
        """process_edf_file's documented postcondition holds for the cleaned fields."""
        result, disk_infos = self._process_fixture()
        assert [s.label for s in result.signal_infos] == [s.label for s in disk_infos]
        assert [s.prefiltering for s in result.signal_infos] == [s.prefiltering for s in disk_infos]

    def test_annotation_channel_survives_cleaning(self):
        signals = [
            {"label": "Fp1", "sample_count": 16},
            {"label": "EDF Annotations", "sample_count": 16},
        ]
        _, disk_infos = self._process_fixture(signals)
        assert disk_infos[1].label == "EDF Annotations"
        assert disk_infos[1].is_annotation_channel


def _ingest_fixture(user, tmp_path):
    """Process the fixture file and persist its results, as ingest would."""
    from recordings.tasks import _save_edf_results

    header_bytes = _make_edf_header(signals=FIXTURE_SIGNALS)
    data_bytes = _make_edf_data(FIXTURE_SIGNALS)
    path = tmp_path / "rec.edf"
    path.write_bytes(header_bytes + data_bytes)
    result = process_edf_file(path)
    recording = Recording.objects.create(
        author=user,
        original_name="rec.edf",
        stored_name="CAFE0000CAFE0000CAFE0000CAFE0000.edf",
        file_path=str(path),
        file_extension=".edf",
        file_size=path.stat().st_size,
        status=Recording.Status.READY,
    )
    _save_edf_results(recording, result)
    return recording, path


@pytest.mark.django_db
class TestSourceFieldPersistence:
    def _ingest(self, user, tmp_path):
        return _ingest_fixture(user, tmp_path)

    def test_rows_hold_cleaned_and_source_values(self, user, tmp_path):
        self._ingest(user, tmp_path)
        rows = list(SignalInfo.objects.order_by("index"))
        assert [r.label for r in rows] == ["Fp1", "MISC_1"]
        assert [r.source_label for r in rows] == ["EEG Fp1-Cz", "XYZ99"]
        assert rows[0].source_transducer_type == "AgAgCl sintered VendorCorp"
        assert rows[1].source_prefiltering == "0.53-70 Hz"
        assert all(r.transducer_type == "" for r in rows)

    def test_rows_agree_with_the_file_on_disk(self, user, tmp_path):
        _, path = self._ingest(user, tmp_path)
        on_disk = path.read_bytes()
        disk_infos = parse_signal_infos(on_disk, parse_edf_header(on_disk))
        rows = list(SignalInfo.objects.order_by("index"))
        assert [r.label for r in rows] == [s.label for s in disk_infos]
        assert [r.prefiltering for r in rows] == [s.prefiltering for s in disk_infos]

    def test_refresh_preserves_source_fields(self, user, tmp_path):
        from recordings.metadata import refresh_signal_metadata
        from recordings.models import RecordingMeta

        recording, _ = self._ingest(user, tmp_path)
        # Force the replacement path: claim a signal count the file contradicts.
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        meta = RecordingMeta.objects.get(content_type=ct, object_id=str(recording.pk))
        meta.signal_count = 99
        meta.save()

        result = refresh_signal_metadata(recording)
        assert result.changed
        rows = list(SignalInfo.objects.order_by("index"))
        assert [r.source_label for r in rows] == ["EEG Fp1-Cz", "XYZ99"]
        assert rows[0].source_transducer_type == "AgAgCl sintered VendorCorp"

    def test_refresh_drops_source_fields_when_channels_shift(self, user, tmp_path):
        """Index alone is not channel identity — a shifted rewrite must not misattribute provenance."""
        from recordings.metadata import refresh_signal_metadata

        recording, path = self._ingest(user, tmp_path)
        swapped = [FIXTURE_SIGNALS[1], FIXTURE_SIGNALS[0]]
        path.write_bytes(_make_edf_header(signals=swapped) + _make_edf_data(swapped))

        result = refresh_signal_metadata(recording)
        assert result.changed
        rows = list(SignalInfo.objects.order_by("index"))
        assert all(r.source_label == "" for r in rows)
        assert all(r.source_transducer_type == "" for r in rows)


@pytest.mark.django_db
class TestSourceFieldApiGating:
    """source_* mirrors original_name: the author sees it, every other caller gets null."""

    def _recording_with_source_rows(self, author):
        from recordings.models import RecordingMeta

        recording = Recording.objects.create(
            author=author,
            original_name="rec.edf",
            stored_name="BEEF0000BEEF0000BEEF0000BEEF0000.edf",
            file_path="/tmp/none.edf",
            file_extension=".edf",
            file_size=1024,
            status=Recording.Status.READY,
        )
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        meta = RecordingMeta.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            format="edf",
            duration=1.0,
            data_record_count=1,
            data_record_duration=1.0,
            signal_count=1,
            discontinuous=False,
        )
        SignalInfo.objects.create(
            meta=meta,
            index=0,
            label="Fp1",
            source_label="EEG Fp1-Cz",
            source_transducer_type="AgAgCl sintered VendorCorp",
            source_prefiltering="0.53-70 Hz",
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-32768,
            digital_max=32767,
            units_per_bit=0.003,
            digital_offset=0.0,
            sample_count=16,
            sampling_rate=16.0,
        )
        return recording

    def _get_detail(self, client, recording):
        hash_part = recording.stored_name.split(".")[0]
        resp = client.get(DETAIL_URL.format(hash=hash_part))
        assert resp.status_code == 200
        return json.loads(resp.content)

    def test_author_sees_source_fields(self, auth_client):
        client, user = auth_client
        recording = self._recording_with_source_rows(user)
        body = self._get_detail(client, recording)
        signal = body["meta"]["signals"][0]
        assert signal["label"] == "Fp1"
        assert signal["source_label"] == "EEG Fp1-Cz"
        assert signal["source_transducer_type"] == "AgAgCl sintered VendorCorp"
        assert signal["source_prefiltering"] == "0.53-70 Hz"

    def test_grantee_gets_null_source_fields(self, client, user, make_user):
        reader = make_user()
        recording = self._recording_with_source_rows(user)
        ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
        AccessRight.objects.create(
            content_type=ct,
            object_id=str(recording.pk),
            access_giver=user,
            access_target=reader,
            can_read=True,
        )
        client.force_login(reader)
        body = self._get_detail(client, recording)
        signal = body["meta"]["signals"][0]
        assert signal["label"] == "Fp1"
        assert signal["source_label"] is None
        assert signal["source_transducer_type"] is None
        assert signal["source_prefiltering"] is None
        assert "VendorCorp" not in resp_text(body)


def resp_text(body) -> str:
    """Flatten a JSON body for substring absence checks."""
    return json.dumps(body)
