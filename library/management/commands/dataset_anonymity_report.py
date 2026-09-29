"""Report, per release of a release-gated dataset, the ¶ 41 record and the pool's equivalence-class sizes.

The anonymity report the multi-centre dataset design keeps beside its written assessment, re-run on
every release run and every profile change (EDPB Guidelines 02/2026 ¶ 35, 41). For each release it
prints the record the run stored (profile version, pass versions, k and m, sign-off count,
assessment reference, member count), how many of those members are still present, the pass
version now stamped on each with any member re-written since the run flagged, and the sizes of the
equivalence classes over the pool as released up to that run: the minimum k, the fraction of
members below the recorded k, the prosecutor risk and the entropy in bits.

Classes come from the function the project registers with ``library.release.register_equivalence_class``.
Without one the report prints the record and says that no class function is registered. The m
condition is repeated as recorded and never recomputed, because the pooled ingest keeps no
contributor per recording; journalist risk is not computed, because it needs population
frequencies the platform does not hold.

Reads only. The run is recorded as an ``Activity`` row so the trail shows when the record was
re-checked.

Usage::

    python manage.py dataset_anonymity_report <dataset-hash>                # every release
    python manage.py dataset_anonymity_report <dataset-hash> --release 7    # one run
    python manage.py dataset_anonymity_report <dataset-hash> --format json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity
from activity.system_activity import with_system_activity
from library.models import DatasetRelease
from library.release import resolve_dataset
from library.reports import anonymity_report


class Command(BaseCommand):
    """Print the release record and the class sizes of a release-gated dataset."""

    help = "Report the release record and equivalence-class sizes of a release-gated dataset"

    def add_arguments(self, parser):
        parser.add_argument("dataset", help="Dataset hash or primary key")
        parser.add_argument("--release", type=int, help="Report one release, by its id (default every release)")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        dataset = resolve_dataset(options["dataset"])
        if dataset is None:
            raise CommandError(f"No active dataset matches {options['dataset']!r}")
        if not dataset.release_gated:
            raise CommandError(f"Dataset {dataset.object_hash} is not release-gated")
        releases = DatasetRelease.objects.filter(dataset=dataset).order_by("released_on", "pk")
        if options.get("release") is not None:
            releases = releases.filter(pk=options["release"])
            if not releases.exists():
                raise CommandError(f"Dataset {dataset.object_hash} has no release {options['release']}")
        with with_system_activity(
            "library.dataset.anonymity_report",
            interface=Activity.Interface.COMMAND,
            target=dataset,
            metadata={"release_id": options.get("release"), "format": options["format"]},
        ) as activity:
            reports = [anonymity_report(dataset, release) for release in releases]
            if activity is not None:
                activity.metadata["release_count"] = len(reports)
                activity.save(update_fields=["metadata"])
        self._emit(dataset, reports, options["format"])

    def _emit(self, dataset, reports: list[dict], fmt: str) -> None:
        if fmt == "json":
            self.stdout.write(json.dumps({"dataset": dataset.object_hash, "releases": reports}, indent=2))
            return
        if not reports:
            self.stdout.write(f"Dataset {dataset.object_hash} has no releases")
            return
        for report in reports:
            self._emit_release(report)

    def _emit_release(self, report: dict) -> None:
        self.stdout.write(
            f"Release {report['release_id']} of dataset {report['dataset']} on {report['released_on']} "
            f"(month {report['release_month']}): {report['member_count']} released, {report['present_count']} present, "
            f"{report['withdrawn_count']} withdrawn, {report['changed_count']} re-written since; pool {report['pool_count']}"
        )
        self.stdout.write(
            f"  record: profile {report['profile_version'] or '-'}, passes {report['deidentification_versions'] or '-'}, "
            f"k {report['k'] if report['k'] is not None else '-'}, m {report['m'] if report['m'] is not None else '-'} "
            f"(as recorded), {report['sign_off_count']} sign-offs, assessment {report['assessment_reference'] or '-'}"
        )
        if not report["class_function_registered"]:
            self.stdout.write("  classes: no equivalence-class function is registered; nothing counted")
        else:
            below = report["below_k_fraction"]
            self.stdout.write(
                f"  classes: {len(report['classes'])} over the pool, {report['unclassified']} unclassified; "
                f"min k {report['min_k'] if report['min_k'] is not None else '-'}, "
                f"below k {f'{below:.0%}' if below is not None else '-'}, "
                f"prosecutor risk {f'{report['prosecutor_risk']:.3f}' if report['prosecutor_risk'] else '-'}, "
                f"entropy {f'{report['entropy_bits']:.2f} bits' if report['entropy_bits'] is not None else '-'}; "
                "journalist risk not computed (no population frequencies)"
            )
            for cls in report["classes"]:
                self.stdout.write(f"    {cls['key']}: {cls['size']}")
        for member in report["members"]:
            flag = "  re-written" if member["changed_since_release"] else ""
            version = member["deidentification_version"]
            self.stdout.write(f"  {member['member']}  pass {version if version is not None else '-'}{flag}")
