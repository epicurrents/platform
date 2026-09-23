"""Purge members of release-gated datasets by the hash of the bytes they were submitted as.

The withdrawal path for a dataset pool: a contributor asks for a recording to go, naming it by the
SHA-256 of the file it submitted, and the operator runs this command with those hashes. For each
hash it finds the matching members of release-gated datasets (``Recording.file_hash``, the digest of
the upload as received), unlinks each file, deletes each row so the audit signal records the
deletion, and reports per hash whether it was ``purged`` or ``not found``. That report, handed back
to the contributor, is the only confirmation of withdrawal the platform gives and its only answer to
whether a hash exists; no endpoint answers that question to any role. It is also the dataset's
Art. 17 path, since a pool's recordings belong to no platform user and account erasure never
reaches them.

Scope is members of release-gated datasets only, trashed ones included, so a centre's withdrawal
cannot remove a platform user's own recording that happens to share the bytes; a match outside the
scope is reported as not found. The originals preservation volume is never touched, as nowhere in
the platform reads it back; a release-gated deployment refuses to configure one.

Audited as ``recordings.purge_dataset`` with counts in the metadata; each deletion is recorded under
it.

Usage::

    python manage.py purge_dataset_recordings <sha256> [<sha256> ...]
    python manage.py purge_dataset_recordings --hashes-file withdrawal.txt --dry-run
    python manage.py purge_dataset_recordings --dataset <dataset-hash> <sha256> --format json
"""

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from library.models import Dataset, DatasetItem
from recordings.models import Recording

STATUS_PURGED = "purged"
STATUS_NOT_FOUND = "not found"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"

_HEX = set("0123456789abcdef")


def _normalise(value: str) -> str | None:
    """A lower-case 64-character hex digest, or None when the value is not one."""
    candidate = value.strip().lower()
    if len(candidate) != 64 or any(c not in _HEX for c in candidate):
        return None
    return candidate


def _member_pks(dataset: Dataset | None) -> set[int]:
    """Primary keys of every recording that is a member of a release-gated dataset, or of *dataset*."""
    recording_ct = ContentType.objects.get_for_model(Recording, for_concrete_model=False)
    items = DatasetItem.objects.filter(content_type=recording_ct, dataset__release_gated=True)
    if dataset is not None:
        items = items.filter(dataset=dataset)
    return {int(pk) for pk in items.values_list("object_id", flat=True) if str(pk).isdigit()}


class Command(BaseCommand):
    """Purge release-gated dataset members by submitted-file hash and report per hash."""

    help = "Purge members of release-gated datasets by the SHA-256 of the submitted file"

    def add_arguments(self, parser):
        parser.add_argument("hashes", nargs="*", help="SHA-256 digests of the submitted files")
        parser.add_argument("--hashes-file", help="File with one digest per line; blank lines ignored")
        parser.add_argument("--dataset", help="Restrict to members of this dataset (hash or primary key)")
        parser.add_argument("--dry-run", action="store_true", help="Report matches without deleting anything")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        submitted = list(options["hashes"])
        if options.get("hashes_file"):
            path = Path(options["hashes_file"])
            if not path.is_file():
                raise CommandError(f"No such file: {path}")
            submitted += [line for line in path.read_text().splitlines() if line.strip()]
        if not submitted:
            raise CommandError("Give at least one hash, or --hashes-file")

        dataset = None
        if options.get("dataset"):
            from library.management.commands.release_dataset import _resolve_dataset

            dataset = _resolve_dataset(options["dataset"])
            if not dataset.release_gated:
                raise CommandError(f"Dataset {dataset.object_hash} is not release-gated")

        scope = _member_pks(dataset)
        rows: list[dict] = []
        if options["dry_run"]:
            for value in submitted:
                rows.append(self._examine(value, scope))
        else:
            with with_system_activity(
                "recordings.purge_dataset",
                interface=Activity.Interface.COMMAND,
                target=dataset,
                metadata={"submitted_count": len(submitted)},
            ):
                for value in submitted:
                    rows.append(self._purge(value, scope))

        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        if options["format"] == "json":
            self.stdout.write(json.dumps({"dry_run": options["dry_run"], "counts": counts, "hashes": rows}, indent=2))
        else:
            for row in rows:
                suffix = f" ({row['count']})" if row.get("count", 0) > 1 else ""
                self.stdout.write(f"{row['status']:<10} {row['hash']}{suffix}")
            summary = ", ".join(f"{status}: {n}" for status, n in sorted(counts.items()))
            self.stdout.write(("Dry run. " if options["dry_run"] else "") + summary)
        if counts.get(STATUS_ERROR):
            raise CommandError(f"{counts[STATUS_ERROR]} hash(es) could not be purged; see above")

    @staticmethod
    def _matches(digest: str, scope: set[int]) -> list[Recording]:
        return list(Recording.objects.filter(file_hash__iexact=digest, pk__in=scope).order_by("pk"))

    def _examine(self, value: str, scope: set[int]) -> dict:
        digest = _normalise(value)
        if digest is None:
            return {"hash": value.strip(), "status": STATUS_INVALID, "count": 0}
        matches = self._matches(digest, scope)
        status = STATUS_PURGED if matches else STATUS_NOT_FOUND
        return {"hash": digest, "status": status, "count": len(matches)}

    def _purge(self, value: str, scope: set[int]) -> dict:
        digest = _normalise(value)
        if digest is None:
            return {"hash": value.strip(), "status": STATUS_INVALID, "count": 0}
        matches = self._matches(digest, scope)
        if not matches:
            return {"hash": digest, "status": STATUS_NOT_FOUND, "count": 0}
        purged = 0
        for recording in matches:
            file_path = Path(recording.file_path) if recording.file_path else None
            try:
                if file_path is not None and file_path.exists():
                    file_path.unlink()
            except OSError as exc:
                # The row stays so the next run can retry: deleting it now would
                # leave the file on disk with nothing remembering it.
                self.stderr.write(f"Could not unlink the file of recording {recording.pk}: {exc}")
                continue
            recording.delete()
            purged += 1
        if purged < len(matches):
            return {"hash": digest, "status": STATUS_ERROR, "count": purged}
        return {"hash": digest, "status": STATUS_PURGED, "count": purged}
