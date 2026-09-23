"""Release gating: the dataset flag, the release-run record and the item's pointer at the run that published it."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0002_tag_curated"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="dataset",
            name="release_gated",
            field=models.BooleanField(
                default=False,
                help_text="Hide members until a release run publishes them, refuse share-token callers and serve the release month in place of the upload time.",
            ),
        ),
        migrations.CreateModel(
            name="DatasetRelease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("released_on", models.DateField()),
                ("release_month", models.DateTimeField(editable=False)),
                ("profile_version", models.CharField(blank=True, default="", max_length=64)),
                ("deidentification_versions", models.JSONField(blank=True, default=list)),
                ("k", models.PositiveIntegerField(blank=True, null=True)),
                ("m", models.PositiveIntegerField(blank=True, null=True)),
                ("sign_off_user_ids", models.JSONField(blank=True, default=list)),
                ("assessment_reference", models.CharField(blank=True, default="", max_length=512)),
                ("member_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="dataset_releases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "dataset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="releases", to="library.dataset"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="datasetitem",
            name="release",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="items",
                to="library.datasetrelease",
            ),
        ),
        migrations.AddIndex(
            model_name="datasetrelease",
            index=models.Index(fields=["dataset", "released_on"], name="library_dat_dataset_882260_idx"),
        ),
    ]
