"""Import a published dataset's subject table onto a dataset's subject fields, through a mapping file.

The table's rows are paired with the dataset author's own recordings by the original file name, or with
``"on": "content"`` by the bytes of the file each row names under ``--files``, and each declared field is read
through the mapping (``library.subject_import`` has the format). Columns the mapping does not name are
dropped, and cells it does not cover are counted, never stored. Output is counts and the table's line numbers for
rows that matched nothing or matched ambiguously; it never names a recording, a file or a value.

Audited as ``library.dataset.subject.import`` against the dataset with the counts in the metadata, never the
table's path, the mapping or a value; each member's change is recorded under it with its values withheld.
``--dry-run`` reports what would be written and writes nothing.

Usage::

    python manage.py import_subject_table <dataset-hash> participants.tsv --mapping mapping.json --dry-run
    python manage.py import_subject_table <dataset-hash> participants.tsv --mapping mapping.json
    python manage.py import_subject_table <dataset-hash> subjects.csv --mapping mapping.json --files /data/source
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from activity.models import Activity
from activity.system_activity import with_system_activity
from library.models import Dataset
from library.pools import is_pool
from library.release import resolve_dataset
from library.subject_import import ImportRefused, apply_plan, hash_files, load_mapping, plan_import, read_table
from library.subjects import schema_of


class Command(BaseCommand):
    """Map a published subject table onto a dataset's subject fields."""

    help = "Import a published dataset's subject table onto a dataset's subject fields through a mapping file"

    def add_arguments(self, parser):
        parser.add_argument("dataset", help="Dataset hash or primary key")
        parser.add_argument("table", help="The subject table, CSV or (.tsv, .tab) tab-separated, UTF-8")
        parser.add_argument("--mapping", required=True, help="The JSON mapping file")
        parser.add_argument(
            "--files",
            help='The folder the table\'s file names are read under; required by, and only for, "on": "content"',
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would be written and write nothing")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        dataset = resolve_dataset(options["dataset"])
        if dataset is None:
            raise CommandError(f"No active dataset matches {options['dataset']!r}")
        if is_pool(dataset):
            raise CommandError(
                f"Dataset {dataset.object_hash} is a submission pool; its values arrive with submissions"
            )
        try:
            raw_mapping = json.loads(Path(options["mapping"]).read_text(encoding="utf-8"))
        except OSError as exc:
            raise CommandError(f"Cannot read the mapping file: {exc.strerror}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommandError("The mapping file is not UTF-8 JSON") from exc
        try:
            header, rows = read_table(Path(options["table"]))
        except OSError as exc:
            raise CommandError(f"Cannot read the table: {exc.strerror}") from exc
        except ImportRefused as exc:
            raise CommandError(str(exc)) from exc

        # Checked before the audited scope opens, so a refused mapping leaves no activity row; checked again under
        # the lock in case the schema changed in between.
        mapping = self._mapping(dataset, raw_mapping, header)
        digests = self._digests(mapping, rows, options["files"])
        plan = self._plan(dataset, raw_mapping, header, rows, digests)
        if options["dry_run"]:
            self._emit(dataset, plan.report(), options["format"], dry_run=True)
            return

        with (
            with_system_activity(
                "library.dataset.subject.import", interface=Activity.Interface.COMMAND, target=dataset
            ) as activity,
            transaction.atomic(),
        ):
            # Under the schema write's lock, so the values are checked against the schema they are stored under.
            locked = Dataset.objects.select_for_update().get(pk=dataset.pk)
            plan = self._plan(locked, raw_mapping, header, rows, digests)
            apply_plan(plan)
            if activity is not None:
                activity.metadata = plan.audit_metadata()
                activity.save(update_fields=["metadata"])
        self._emit(dataset, plan.report(), options["format"], dry_run=False)

    @staticmethod
    def _mapping(dataset, raw_mapping, header):
        schema = schema_of(dataset)
        if not schema:
            raise CommandError(f"Dataset {dataset.object_hash} declares no subject fields")
        try:
            return load_mapping(raw_mapping, schema, header)
        except ImportRefused as exc:
            raise CommandError(str(exc)) from exc

    @staticmethod
    def _digests(mapping, rows, files):
        """The digest of each file the table names, read once and outside the lock; ``None`` unless on content."""
        if mapping.match_on != "content":
            if files is not None:
                raise CommandError('--files is read only when the mapping matches "on": "content"')
            return None
        if files is None:
            raise CommandError('A mapping matching "on": "content" needs --files')
        root = Path(files)
        if not root.is_dir():
            raise CommandError("--files is not a folder")
        return hash_files(rows, mapping.match_column, root)

    def _plan(self, dataset, raw_mapping, header, rows, digests):
        mapping = self._mapping(dataset, raw_mapping, header)
        return plan_import(dataset, schema_of(dataset), mapping, rows, digests)

    def _emit(self, dataset, report: dict, fmt: str, *, dry_run: bool) -> None:
        if fmt == "json":
            self.stdout.write(json.dumps({"dataset": dataset.object_hash, **report, "dry_run": dry_run}, indent=2))
            return
        verb = "Would change" if dry_run else "Changed"
        self.stdout.write(
            f"{verb} the values of {report['members_changed']} of {report['members']} recording members of dataset "
            f"{dataset.object_hash}"
        )
        self.stdout.write(
            f"  rows: {report['rows']}, matched {report['matched_rows']}, unmatched {report['unmatched_rows']}, "
            f"ambiguous {report['ambiguous_rows']}"
        )
        self.stdout.write(
            f"  members matched: {report['members_matched']}, of which released and left as they are: "
            f"{report['members_released']}"
        )
        if report["files_not_read"]:
            self.stdout.write(f"  rows naming no readable file under --files, or none: {report['files_not_read']}")
        if report["members_not_controlled"]:
            self.stdout.write(f"  recording members by another author, not paired: {report['members_not_controlled']}")
        for key, counts in report["fields"].items():
            self.stdout.write(
                f"  {key}: mapped {counts['mapped']}, unmapped {counts['unmapped']}, missing {counts['missing']}"
            )
        if report["unmatched_lines"]:
            self.stdout.write(f"  unmatched lines: {', '.join(map(str, report['unmatched_lines']))}")
        if report["ambiguous_lines"]:
            self.stdout.write(f"  ambiguous lines: {', '.join(map(str, report['ambiguous_lines']))}")
