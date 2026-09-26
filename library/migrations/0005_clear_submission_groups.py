"""Clear every dataset's submission group ahead of the one-to-one pool group.

The group a dataset named before this was any group an author picked, not one created for the pool, so no dataset
keeps it: the reference is cleared, the group itself is left alone, and a pool is configured afresh from the dataset
page. A migration of its own, because PostgreSQL refuses to alter a table with the update's trigger events pending.
"""

from django.db import migrations


def clear_submission_groups(apps, schema_editor):
    Dataset = apps.get_model("library", "Dataset")
    Dataset.objects.filter(submission_group__isnull=False).update(submission_group=None)


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0004_dataset_submission_group"),
    ]

    operations = [
        migrations.RunPython(clear_submission_groups, migrations.RunPython.noop),
    ]
