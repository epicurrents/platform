"""System checks for the *dicom* plugin."""

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security)
def check_uid_key(app_configs, **kwargs):
    """Warn when de-identification is on and ``DICOM_UID_KEY`` is empty, which refuses every upload.

    A warning rather than an error: a deployment that enables the plugin on an
    update has no shell to add the key with before boot, and refusing the boot
    would take the rest of the platform down with the plugin.
    """
    if not getattr(settings, "DICOM_DEIDENTIFY_ON_INGEST", True) or getattr(settings, "DICOM_UID_KEY", ""):
        return []
    return [
        Warning(
            "DICOM de-identification is on and DICOM_UID_KEY is empty, so every DICOM upload and import is refused.",
            hint="Run `python manage.py init_env` or `scripts/enable_plugin.sh dicom` to generate it, then restart.",
            id="dicom.W001",
        )
    ]
