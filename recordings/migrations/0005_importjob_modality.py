"""Keep a declared modality on the import job, so a resumed run does not return to the channel vote."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0004_submission_not_before"),
    ]

    operations = [
        migrations.AddField(
            model_name="importjob",
            name="modality",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
