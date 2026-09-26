"""Rename ``SubmissionBatch`` to ``SubmissionLedger``: one per contributor per pool, its profile the pool's."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0006_dataset_submission_pool"),
        ("recordings", "0006_merge_submission_batches"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel(old_name="SubmissionBatch", new_name="SubmissionLedger"),
        migrations.RenameField(model_name="submissionfile", old_name="batch", new_name="ledger"),
        migrations.AlterField(
            model_name="submissionledger",
            name="contributor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="submission_ledgers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="submissionledger",
            name="dataset",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="submission_ledgers",
                to="library.dataset",
            ),
        ),
        migrations.RemoveField(model_name="submissionledger", name="profile_key"),
        migrations.AddConstraint(
            model_name="submissionledger",
            constraint=models.UniqueConstraint(
                condition=models.Q(contributor__isnull=False),
                fields=("dataset", "contributor"),
                name="submission_ledger_one_per_contributor",
            ),
        ),
    ]
