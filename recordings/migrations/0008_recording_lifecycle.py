"""Add the AVAILABLE status and ``Recording.ingested_at``, stamping it on every recording whose ingest finished.

Before the lifecycle, READY and FAILED were the only states an ingest ended in, and PROCESSING always meant an ingest
still running. So the backfill stamps READY and FAILED rows and leaves PENDING and PROCESSING ones null, which keeps
the orphan reaper's reach over existing rows exactly what it was. ``created_at`` stands in for the time ingest
finished, which no column recorded; only whether the value is null is read.
"""

from django.db import migrations, models
from django.db.models import F


def stamp_finished_ingests(apps, schema_editor):
    Recording = apps.get_model("recordings", "Recording")
    Recording.objects.filter(status__in=["ready", "failed"], ingested_at__isnull=True).update(
        ingested_at=F("created_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0007_importjob_channel_map"),
    ]

    operations = [
        migrations.AddField(
            model_name="recording",
            name="ingested_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="recording",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("available", "Available"),
                    ("ready", "Ready"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
        migrations.RunPython(stamp_finished_ingests, migrations.RunPython.noop),
    ]
