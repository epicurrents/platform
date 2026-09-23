"""Add the sharer's contextual-assessment record to ``AccessRight``.

Two nullable-by-default columns, so no existing row changes: a reference to the sharer's documented
assessment and the date it was made, constrained to be set together. See ``epicurrents.assessment``.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("epicurrents", "0002_alter_accessright_apply_middleware"),
        ("federation", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="accessright",
            name="assessment_date",
            field=models.DateField(
                blank=True,
                help_text="Date the assessment named by assessment_reference was made or last re-run.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="accessright",
            name="assessment_reference",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Reference to the sharer's documented contextual assessment for this grant (an identifier or a URL), where one exists. Set together with assessment_date.",
                max_length=512,
            ),
        ),
        migrations.AddConstraint(
            model_name="accessright",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("assessment_date__isnull", True), ("assessment_reference", "")),
                    models.Q(models.Q(("assessment_reference", ""), _negated=True), ("assessment_date__isnull", False)),
                    _connector="OR",
                ),
                name="access_right_assessment_dated",
            ),
        ),
    ]
