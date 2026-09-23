"""Add the per-recording de-identification record to ``RecordingMeta``.

Two columns stamped at ingest from then on: ``deidentification_version``, the version of the
header and channel-block pass that wrote the stored file, and ``annotation_text_preserved``,
whether the file keeps its annotation text. Existing rows take the defaults, ``0`` and ``False``,
and ``0`` is what marks a recording as processed before the record existed; the report command
reads the flag on such a row as unknown rather than as a decision.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0002_recording_stored_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="recordingmeta",
            name="annotation_text_preserved",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="recordingmeta",
            name="deidentification_version",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
