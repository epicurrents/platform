"""The subject-table import: the mapping's validation, how a cell maps, how rows pair with recordings, and the command.

Covers the refusals of a mapping, values before bands and the half-open bands, the four match modes, ambiguous
and unmatched rows, byte-identical files under content matching, released members left alone, a pool refused, and
what the run leaves behind: values written and withheld from the trail, an activity row carrying counts only, and
output naming no file and no value.
"""

from __future__ import annotations

import hashlib
import json
import math
from io import StringIO

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command

from activity.models import Activity, ObjectChangeLog
from library.models import Dataset, DatasetItemSubject, DatasetMeta
from library.subject_import import FieldMap, ImportRefused, _matches, load_mapping
from library.tests.test_release import _add, _gated_dataset, _recording, _release

pytestmark = pytest.mark.django_db

# Distinctive, so a search of the output or the trail for them cannot match anything else.
MARKER = "band-seventy-plus"
SUBJECT = "sub-zq7"
DROPPED = "sinistral-qx"
AGE = {"key": "age", "label": "Age", "choices": ["0-17", "18-64", MARKER]}
SEX = {"key": "sex", "label": "Sex", "choices": ["female", "male"]}
MAPPING = {
    "match": {"column": "participant_id", "on": "prefix"},
    "fields": {
        "age": {
            "column": "age",
            "bands": [{"below": 18, "choice": "0-17"}, {"from": 18, "below": 65, "choice": "18-64"}],
            "values": {"89+": MARKER},
        },
        "sex": {"column": "sex", "values": {"F": "female", "M": "male"}},
    },
}
COLUMNS = ["participant_id", "age", "sex", "handedness"]


def _dataset(author, *recordings, schema=(AGE, SEX)):
    dataset = Dataset.objects.create(author=author, name="published")
    DatasetMeta.objects.create(dataset=dataset, subject_schema=list(schema))
    items = [_add(dataset, recording) for recording in recordings]
    return dataset, items


def _named(author, index, original_name, **kwargs):
    return _recording(author, index=index, original_name=original_name, **kwargs)


def _files(tmp_path, rows, mapping=MAPPING, columns=COLUMNS):
    table = tmp_path / "participants.tsv"
    table.write_text("\n".join("\t".join(row) for row in [columns, *rows]) + "\n", encoding="utf-8")
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    return table, mapping_path


def _run(dataset, table, mapping_path, *extra):
    out = StringIO()
    call_command(
        "import_subject_table", dataset.object_hash, str(table), "--mapping", str(mapping_path), *extra, stdout=out
    )
    return out.getvalue()


def _values(item):
    row = DatasetItemSubject.objects.filter(item=item).first()
    return row.values if row is not None else None


class TestMapping:
    @pytest.mark.parametrize(
        "mapping",
        [
            {"match": {"column": "participant_id"}},
            {"match": {"column": "nowhere"}, "fields": MAPPING["fields"]},
            {"match": {"column": "participant_id", "on": "position"}, "fields": MAPPING["fields"]},
            {"match": MAPPING["match"], "fields": {}},
            {"match": MAPPING["match"], "fields": {"diagnosis": {"column": "age", "values": {}}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "nowhere", "values": {}}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "age"}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "age", "values": {"3": "toddler"}}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "age", "bands": [{"below": 3, "choice": "x"}]}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "age", "bands": [{"choice": "0-17"}]}}},
            {"match": MAPPING["match"], "fields": {"age": {"column": "age", "bands": {"below": 3}}}},
            {
                "match": MAPPING["match"],
                "fields": {"age": {"column": "age", "bands": [{"from": 5, "below": 5, "choice": "0-17"}]}},
            },
            {
                "match": MAPPING["match"],
                "fields": {"age": {"column": "age", "bands": [{"from": True, "below": 5, "choice": "0-17"}]}},
            },
            {
                "match": MAPPING["match"],
                "fields": {
                    "age": {
                        "column": "age",
                        "bands": [{"below": 20, "choice": "0-17"}, {"from": 18, "choice": "18-64"}],
                    }
                },
            },
            {"match": MAPPING["match"], "fields": {"age": {"column": "age", "values": {}, "unit": "years"}}},
        ],
    )
    def test_a_faulty_mapping_is_refused(self, mapping):
        with pytest.raises(ImportRefused):
            load_mapping(mapping, [AGE, SEX], COLUMNS)

    def test_a_valid_mapping_loads(self):
        mapping = load_mapping(MAPPING, [AGE, SEX], COLUMNS)
        assert mapping.match_on == "prefix"
        assert [field.key for field in mapping.fields] == ["age", "sex"]

    def test_values_come_before_bands_and_bands_are_half_open(self):
        field = load_mapping(MAPPING, [AGE, SEX], COLUMNS).fields[0]
        assert field.choice_for("89+") == MARKER
        assert field.choice_for("17.9") == "0-17"
        assert field.choice_for("18") == "18-64"
        assert field.choice_for("65") is None
        assert field.choice_for("forty") is None
        assert field.choice_for("nan") is None
        assert field.choice_for("inf") is None

    def test_a_values_only_field_maps_nothing_else(self):
        field = FieldMap(key="sex", column="sex", values={"F": "female"}, bands=[])
        assert field.choice_for("F") == "female"
        assert field.choice_for("f") is None
        assert field.choice_for("1") is None

    def test_an_open_band_reaches_infinity_and_no_further(self):
        field = FieldMap(key="age", column="age", values={}, bands=[(65.0, math.inf, MARKER)])
        assert field.choice_for("1e300") == MARKER
        assert field.choice_for("inf") is None


class TestMatching:
    @pytest.mark.parametrize(
        ("original_name", "cell", "on", "expected"),
        [
            ("sub-1_task-rest_eeg.edf", "sub-1", "prefix", True),
            ("sub-10_task-rest_eeg.edf", "sub-1", "prefix", False),
            ("sub-1.edf", "sub-1", "prefix", True),
            ("sub-1", "sub-1", "prefix", True),
            ("sub-1_eeg.edf", "", "prefix", False),
            ("sub-1_eeg.edf", "sub-1_eeg.edf", "name", True),
            ("sub-1_eeg.edf", "sub-1/eeg/sub-1_eeg.edf", "name", True),
            ("sub-1_eeg.edf", "sub-1_eeg.bdf", "name", False),
            ("sub-1_eeg.edf", "sub-1_eeg.vhdr", "stem", True),
            ("sub-1_eeg.edf", "sub-1_eeg", "stem", True),
            ("sub-1_eeg.edf", "sub-1", "stem", False),
            ("sub-1.edf", "sub-1.2", "stem", False),
            ("sub-1.2.edf", "sub-1.2", "stem", True),
        ],
    )
    def test_match_modes(self, original_name, cell, on, expected):
        assert _matches(original_name, cell, on) is expected


class TestCommand:
    def test_the_import_writes_the_mapped_values(self, user, tmp_path):
        first = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        second = _named(user, 1, f"{SUBJECT}_task-sleep_eeg.edf")
        other = _named(user, 2, "sub-2_task-rest_eeg.edf")
        dataset, items = _dataset(user, first, second, other)
        table, mapping = _files(tmp_path, [[SUBJECT, "89+", "F", DROPPED], ["sub-2", "30", "M", DROPPED]])
        _run(dataset, table, mapping)
        assert _values(items[0]) == {"age": MARKER, "sex": "female"}
        assert _values(items[1]) == {"age": MARKER, "sex": "female"}
        assert _values(items[2]) == {"age": "18-64", "sex": "male"}

    def test_the_output_and_the_activity_carry_counts_only(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, _items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [[SUBJECT, "89+", "F", DROPPED], ["sub-gone", "40", "M", DROPPED]])
        output = _run(dataset, table, mapping)
        assert "unmatched lines: 3" in output
        for secret in (SUBJECT, MARKER, "female", "sub-gone", DROPPED, str(tmp_path)):
            assert secret not in output
        activity = Activity.objects.get(verb="library.dataset.subject.import")
        assert activity.target_object_id == str(dataset.pk)
        assert activity.metadata["members_changed"] == 1
        assert activity.metadata["unmatched_rows"] == 1
        assert "unmatched_lines" not in activity.metadata
        recorded = json.dumps(activity.metadata)
        for secret in (SUBJECT, MARKER, "female", "sub-gone", str(tmp_path), "participant_id"):
            assert secret not in recorded

    def test_the_trail_withholds_the_imported_values(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, _items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [[SUBJECT, "89+", "F", DROPPED]])
        _run(dataset, table, mapping)
        changes = ObjectChangeLog.objects.filter(content_type=ContentType.objects.get_for_model(DatasetItemSubject))
        assert changes.count() == 1
        trail = json.dumps([[change.before_state, change.changes] for change in changes])
        assert MARKER not in trail and "female" not in trail

    def test_unmapped_and_missing_cells_are_counted_and_not_stored(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [[SUBJECT, "70", "n/a", DROPPED]])
        output = _run(dataset, table, mapping, "--format", "json")
        report = json.loads(output)
        assert report["fields"]["age"] == {"mapped": 0, "unmapped": 1, "missing": 0}
        assert report["fields"]["sex"] == {"mapped": 0, "unmapped": 0, "missing": 1}
        assert _values(items[0]) is None

    def test_an_import_merges_with_the_values_a_member_has(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, items = _dataset(user, recording)
        DatasetItemSubject.objects.create(item=items[0], values={"sex": "male", "age": "0-17"})
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "", DROPPED]])
        _run(dataset, table, mapping)
        assert _values(items[0]) == {"sex": "male", "age": "18-64"}

    def test_a_member_two_rows_match_takes_neither(self, user, tmp_path):
        recording = _named(user, 0, "sub-1_ses-2_eeg.edf")
        dataset, items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [["sub-1", "30", "F", ""], ["sub-1_ses-2", "10", "M", ""]])
        report = json.loads(_run(dataset, table, mapping, "--format", "json"))
        assert report["ambiguous_lines"] == [2, 3]
        assert _values(items[0]) is None

    def test_a_name_two_members_share_is_ambiguous(self, user, tmp_path):
        first = _named(user, 0, "rec.edf")
        second = _named(user, 1, "rec.edf")
        dataset, items = _dataset(user, first, second)
        mapping = {"match": {"column": "participant_id", "on": "name"}, "fields": MAPPING["fields"]}
        table, mapping_path = _files(tmp_path, [["rec.edf", "30", "F", ""]], mapping=mapping)
        report = json.loads(_run(dataset, table, mapping_path, "--format", "json"))
        assert report["ambiguous_rows"] == 1
        assert _values(items[0]) is None and _values(items[1]) is None

    def test_a_released_member_keeps_its_values(self, user, tmp_path):
        released = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        unreleased = _named(user, 1, f"{SUBJECT}_task-sleep_eeg.edf")
        dataset, items = _gated_dataset(user, released, unreleased)
        DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE, SEX])
        _release(dataset, items[0])
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        report = json.loads(_run(dataset, table, mapping, "--format", "json"))
        assert report["members_released"] == 1
        assert _values(items[0]) is None
        assert _values(items[1]) == {"age": "18-64", "sex": "female"}

    def test_a_trashed_recording_is_not_matched(self, user, tmp_path):
        from django.utils import timezone

        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf", deleted_at=timezone.now())
        dataset, items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        report = json.loads(_run(dataset, table, mapping, "--format", "json"))
        assert report["members"] == 0 and report["unmatched_rows"] == 1
        assert _values(items[0]) is None

    def test_another_authors_recording_is_not_paired(self, user, make_user, tmp_path):
        mine = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        theirs = _named(make_user(), 1, f"{SUBJECT}_task-sleep_eeg.edf")
        dataset, items = _dataset(user, mine, theirs)
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", DROPPED]])
        report = json.loads(_run(dataset, table, mapping, "--format", "json"))
        assert report["members"] == 1 and report["members_not_controlled"] == 1
        assert _values(items[0]) == {"age": "18-64", "sex": "female"}
        assert _values(items[1]) is None

    def test_a_dry_run_writes_nothing(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, items = _dataset(user, recording)
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        report = json.loads(_run(dataset, table, mapping, "--dry-run", "--format", "json"))
        assert report["members_changed"] == 1 and report["dry_run"] is True
        assert _values(items[0]) is None
        assert not Activity.objects.filter(verb="library.dataset.subject.import").exists()

    def test_a_refused_mapping_writes_nothing(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, items = _dataset(user, recording)
        broken = {"match": MAPPING["match"], "fields": {"age": {"column": "age", "values": {"1": "toddler"}}}}
        table, mapping = _files(tmp_path, [[SUBJECT, "1", "F", ""]], mapping=broken)
        with pytest.raises(CommandError):
            _run(dataset, table, mapping)
        assert _values(items[0]) is None
        assert not Activity.objects.filter(verb="library.dataset.subject.import").exists()

    def test_a_pool_is_refused(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, _items = _dataset(user, recording)
        Dataset.objects.filter(pk=dataset.pk).update(submission_profile="any")
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        with pytest.raises(CommandError, match="submission pool"):
            _run(dataset, table, mapping)

    def test_a_dataset_without_a_schema_is_refused(self, user, tmp_path):
        recording = _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf")
        dataset, _items = _dataset(user, recording, schema=())
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        with pytest.raises(CommandError, match="declares no subject fields"):
            _run(dataset, table, mapping)

    def test_a_comma_separated_table_reads(self, user, tmp_path):
        recording = _named(user, 0, "rec-a.edf")
        dataset, items = _dataset(user, recording)
        table = tmp_path / "subjects.csv"
        table.write_text("file,age,sex\nrec-a.edf,12,M\n", encoding="utf-8")
        mapping = tmp_path / "mapping.json"
        mapping.write_text(
            json.dumps({"match": {"column": "file"}, "fields": {"age": MAPPING["fields"]["age"]}}), encoding="utf-8"
        )
        _run(dataset, table, mapping)
        assert _values(items[0]) == {"age": "0-17"}

    def test_a_row_longer_than_the_header_is_refused(self, user, tmp_path):
        dataset, _items = _dataset(user, _named(user, 0, "rec-a.edf"))
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", DROPPED, "extra"]])
        with pytest.raises(CommandError, match="more cells"):
            _run(dataset, table, mapping)


CONTENT = {"match": {"column": "participant_id", "on": "content"}, "fields": MAPPING["fields"]}


def _source(tmp_path, files):
    """A source folder holding *files*, a mapping of name to bytes, and the digest of each."""
    root = tmp_path / "source"
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root, {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _discarded(author, index, file_hash):
    return _named(author, index, f"upload-20260101T00000{index}Z.edf", file_hash=file_hash)


class TestContentMatching:
    def test_rows_pair_with_recordings_by_the_bytes_of_the_file_they_name(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"alpha", "B2.edf": b"beta"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["B2.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "89+", "F", ""], ["B2.edf", "30", "M", ""]], mapping=CONTENT)
        output = _run(dataset, table, mapping, "--files", str(root))
        assert _values(items[0]) == {"age": MARKER, "sex": "female"}
        assert _values(items[1]) == {"age": "18-64", "sex": "male"}
        for secret in ("A1.edf", digests["A1.edf"], str(root)):
            assert secret not in output
        recorded = json.dumps(Activity.objects.get(verb="library.dataset.subject.import").metadata)
        assert digests["A1.edf"] not in recorded and str(root) not in recorded

    def test_identical_files_given_the_same_cells_are_one_row(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"twin", "B2.edf": b"twin"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["B2.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", "x"], ["B2.edf", "30", "M", "y"]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["matched_rows"] == 2 and report["ambiguous_rows"] == 0
        assert report["members_matched"] == 2 and report["fields"]["sex"]["mapped"] == 2
        assert _values(items[0]) == _values(items[1]) == {"age": "18-64", "sex": "male"}

    def test_identical_files_given_different_cells_are_ambiguous(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"twin", "B2.edf": b"twin", "C3.edf": b"twin"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["B2.edf"]))
        rows = [["A1.edf", "30", "M", ""], ["B2.edf", "10", "F", ""], ["C3.edf", "10", "F", ""]]
        table, mapping = _files(tmp_path, rows, mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["ambiguous_lines"] == [2, 3, 4]
        assert _values(items[0]) is None and _values(items[1]) is None

    @pytest.mark.parametrize("cell", ["../outside.edf", "missing.edf", ""])
    def test_a_cell_naming_no_file_in_the_folder_is_unmatched(self, user, tmp_path, cell):
        root, _digests = _source(tmp_path, {"A1.edf": b"alpha"})
        (tmp_path / "outside.edf").write_bytes(b"outside")
        dataset, items = _dataset(user, _discarded(user, 0, hashlib.sha256(b"outside").hexdigest()))
        table, mapping = _files(tmp_path, [[cell, "30", "M", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["unmatched_lines"] == [2]
        assert _values(items[0]) is None

    def test_an_absolute_path_is_not_read(self, user, tmp_path):
        root, _digests = _source(tmp_path, {"A1.edf": b"alpha"})
        outside = tmp_path / "outside.edf"
        outside.write_bytes(b"outside")
        dataset, items = _dataset(user, _discarded(user, 0, hashlib.sha256(b"outside").hexdigest()))
        table, mapping = _files(tmp_path, [[str(outside), "30", "M", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["unmatched_lines"] == [2]
        assert _values(items[0]) is None

    def test_another_authors_recording_is_not_paired_by_content(self, user, make_user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"alpha"})
        dataset, items = _dataset(user, _discarded(make_user(), 0, digests["A1.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["members_not_controlled"] == 1 and report["unmatched_rows"] == 1
        assert _values(items[0]) is None

    def test_content_matching_needs_the_folder(self, user, tmp_path):
        dataset, _items = _dataset(user, _discarded(user, 0, "0" * 64))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""]], mapping=CONTENT)
        with pytest.raises(CommandError, match="needs --files"):
            _run(dataset, table, mapping)
        with pytest.raises(CommandError, match="not a folder"):
            _run(dataset, table, mapping, "--files", str(tmp_path / "nowhere"))

    def test_the_folder_is_refused_for_a_name_match(self, user, tmp_path):
        dataset, _items = _dataset(user, _named(user, 0, f"{SUBJECT}_task-rest_eeg.edf"))
        table, mapping = _files(tmp_path, [[SUBJECT, "30", "F", ""]])
        with pytest.raises(CommandError, match="only when"):
            _run(dataset, table, mapping, "--files", str(tmp_path))

    def test_one_row_reaches_every_member_with_the_same_bytes(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"twin"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["A1.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["matched_rows"] == 1 and report["members_matched"] == 2
        assert _values(items[0]) == _values(items[1]) == {"age": "18-64", "sex": "male"}

    def test_identical_files_writing_the_same_values_agree_whatever_the_spelling(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"twin", "B2.edf": b"twin"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["B2.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "n/a", ""], ["B2.edf", "30.0", "", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["ambiguous_rows"] == 0
        assert _values(items[0]) == _values(items[1]) == {"age": "18-64"}

    def test_a_duplicate_of_an_unmatched_row_is_unmatched(self, user, tmp_path):
        root, _digests = _source(tmp_path, {"A1.edf": b"twin", "B2.edf": b"twin"})
        dataset, _items = _dataset(user, _discarded(user, 0, "0" * 64))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""], ["B2.edf", "30", "M", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["unmatched_lines"] == [2, 3] and report["files_not_read"] == 0

    def test_a_link_in_the_folder_is_followed_wherever_it_points(self, user, tmp_path):
        root, _digests = _source(tmp_path, {})
        annex = tmp_path / "annex"
        annex.mkdir()
        (annex / "object").write_bytes(b"annexed")
        (root / "A1.edf").symlink_to(annex / "object")
        dataset, items = _dataset(user, _discarded(user, 0, hashlib.sha256(b"annexed").hexdigest()))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""]], mapping=CONTENT)
        _run(dataset, table, mapping, "--files", str(root))
        assert _values(items[0]) == {"age": "18-64", "sex": "male"}

    def test_a_backslash_separator_reads_as_a_folder(self, user, tmp_path):
        root, _digests = _source(tmp_path, {})
        (root / "sub-1").mkdir()
        (root / "sub-1" / "A1.edf").write_bytes(b"nested")
        dataset, items = _dataset(user, _discarded(user, 0, hashlib.sha256(b"nested").hexdigest()))
        table, mapping = _files(tmp_path, [["sub-1\\A1.edf", "30", "M", ""]], mapping=CONTENT)
        _run(dataset, table, mapping, "--files", str(root))
        assert _values(items[0]) == {"age": "18-64", "sex": "male"}

    @pytest.mark.parametrize("cell", ["sub-1", "A1.edf", "bad\x00name.edf"])
    def test_a_cell_naming_nothing_readable_is_counted_and_unmatched(self, user, tmp_path, cell):
        root, digests = _source(tmp_path, {"A1.edf": b"alpha"})
        (root / "sub-1").mkdir()
        (root / "A1.edf").chmod(0)
        dataset, _items = _dataset(user, _discarded(user, 0, digests["A1.edf"]))
        table, mapping = _files(tmp_path, [[cell, "30", "M", ""]], mapping=CONTENT)
        try:
            output = _run(dataset, table, mapping, "--files", str(root), "--format", "json")
        finally:
            (root / "A1.edf").chmod(0o600)
        report = json.loads(output)
        if cell == "A1.edf" and report["matched_rows"] == 1:
            pytest.skip("running with privileges that read a file with no permissions")
        assert report["unmatched_lines"] == [2] and report["files_not_read"] == 1
        assert str(root) not in output

    def test_a_released_member_keeps_its_values_under_content_matching(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"alpha", "B2.edf": b"beta"})
        released = _discarded(user, 0, digests["A1.edf"])
        unreleased = _discarded(user, 1, digests["B2.edf"])
        dataset, items = _gated_dataset(user, released, unreleased)
        DatasetMeta.objects.create(dataset=dataset, subject_schema=[AGE, SEX])
        _release(dataset, items[0])
        table, mapping = _files(tmp_path, [["A1.edf", "30", "F", ""], ["B2.edf", "30", "F", ""]], mapping=CONTENT)
        report = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert report["members_released"] == 1
        assert _values(items[0]) is None and _values(items[1]) == {"age": "18-64", "sex": "female"}

    def test_a_dry_run_reports_what_the_run_writes(self, user, tmp_path):
        root, digests = _source(tmp_path, {"A1.edf": b"alpha"})
        dataset, items = _dataset(user, _discarded(user, 0, digests["A1.edf"]))
        table, mapping = _files(tmp_path, [["A1.edf", "30", "M", ""], ["gone.edf", "30", "M", ""]], mapping=CONTENT)
        dry = json.loads(_run(dataset, table, mapping, "--files", str(root), "--dry-run", "--format", "json"))
        assert _values(items[0]) is None
        real = json.loads(_run(dataset, table, mapping, "--files", str(root), "--format", "json"))
        assert {**dry, "dry_run": False} == real
        assert real["members_changed"] == 1 and real["files_not_read"] == 1

    def test_a_missing_cell_disagrees_with_a_choice_spelled_missing(self, user, tmp_path):
        status = {"key": "status", "label": "Status", "choices": ["missing", "present"]}
        root, digests = _source(tmp_path, {"A1.edf": b"twin", "B2.edf": b"twin"})
        recordings = (_discarded(user, 0, digests["A1.edf"]), _discarded(user, 1, digests["B2.edf"]))
        dataset, items = _dataset(user, *recordings, schema=(status,))
        mapping = {**CONTENT, "fields": {"status": {"column": "sex", "values": {"unknown": "missing"}}}}
        table, mapping_path = _files(
            tmp_path, [["A1.edf", "", "n/a", ""], ["B2.edf", "", "unknown", ""]], mapping=mapping
        )
        report = json.loads(_run(dataset, table, mapping_path, "--files", str(root), "--format", "json"))
        assert report["ambiguous_lines"] == [2, 3]
        assert _values(items[0]) is None and _values(items[1]) is None
