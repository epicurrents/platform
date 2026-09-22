"""Flip the ``AccessRight.apply_middleware`` default to ``True``.

Default change only: no row is rewritten, because an existing row's stored value is its decision. A row created
without the flag from now on serves de-identified bytes, and an explicit ``False`` is the deliberate act of granting
the raw file. Every API and command path already defaulted the flag to ``True``; this brings the model into line so a
project's fixture or data migration cannot create a raw grant by omission.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("epicurrents", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accessright",
            name="apply_middleware",
            field=models.BooleanField(
                default=True,
                help_text="Pipe EDF/BDF file content through the configured middleware pipeline when serving this access right. An explicit False grants the raw file. Has no effect on non-EDF files or when the caller is the recording author or a superuser.",
            ),
        ),
    ]
