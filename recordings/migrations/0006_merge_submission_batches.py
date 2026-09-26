"""Keep one batch per contributor per pool ahead of the rename to ``SubmissionLedger``.

A contributor who opened several batches on one pool keeps the earliest, holding every file and the summed ingest
count of the others, which are deleted. A migration of its own, because PostgreSQL refuses to alter a table with the
update's trigger events pending.
"""

from django.db import migrations


def merge_batches(apps, schema_editor):
    SubmissionBatch = apps.get_model("recordings", "SubmissionBatch")
    SubmissionFile = apps.get_model("recordings", "SubmissionFile")
    keep: dict[tuple[int, int], object] = {}
    for batch in SubmissionBatch.objects.filter(contributor__isnull=False).order_by("created_at", "pk"):
        key = (batch.dataset_id, batch.contributor_id)
        first = keep.get(key)
        if first is None:
            keep[key] = batch
            continue
        SubmissionFile.objects.filter(batch=batch).update(batch=first)
        first.ingested_count += batch.ingested_count
        first.save(update_fields=["ingested_count"])
        batch.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0005_recording_public_source"),
    ]

    operations = [
        migrations.RunPython(merge_batches, migrations.RunPython.noop),
    ]
