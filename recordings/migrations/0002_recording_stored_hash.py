"""Add ``Recording.stored_hash``, the digest of the file as stored and the one hash the API serves."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recordings", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="recording",
            name="stored_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
