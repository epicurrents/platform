"""Keep the channel map an import job was started with, so a resumed run applies the same one."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0006_recording_pending_scoring"),
    ]

    operations = [
        migrations.AddField(
            model_name="importjob",
            name="channel_map",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
