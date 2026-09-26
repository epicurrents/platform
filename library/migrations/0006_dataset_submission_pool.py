"""Make a dataset a submission pool: a registered profile, a dedicated one-to-one group and an intake switch."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("library", "0005_clear_submission_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="dataset",
            name="submission_profile",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Key of the registered ingest profile submissions are checked against; empty when not a pool.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="dataset",
            name="submissions_open",
            field=models.BooleanField(
                default=False,
                help_text="Whether the pool accepts new submissions; closing it leaves the spool and the ledgers alone.",
            ),
        ),
        migrations.AlterField(
            model_name="dataset",
            name="submission_group",
            field=models.OneToOneField(
                blank=True,
                help_text="The pool's dedicated group, whose members may submit prepared recordings.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submission_pool",
                to="auth.group",
            ),
        ),
    ]
