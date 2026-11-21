"""PluginConfig for the *dicom* plugin.

Registers the app and wires up extension points once Django is ready. The
app ``label`` stays ``dicom`` (unchanged from when DICOM was a project), so no
database table or migration-history rename is needed by the plugin migration —
only the Python module ``name`` moved from ``projects.dicom`` to
``plugins.dicom``.
"""

from epicurrents.plugins import PluginConfig


class DicomConfig(PluginConfig):
    # Required on every concrete plugin config: the PluginConfig import above
    # makes this module ambiguous to Django's app-config auto-detection, and
    # without an explicit default Django silently falls back to the bare
    # AppConfig — ready() never runs. See epicurrents/plugins.py.
    default = True
    default_auto_field = "django.db.models.BigAutoField"
    name = "plugins.dicom"
    label = "dicom"
    requires_platform = ">=0.2,<0.3"

    # DICOM composes with any active project; it depends only on core apps that
    # are always loaded (recordings' access model, activity's audit trail), so
    # no plugin-to-plugin requirement is declared.
    requires: list[str] = []

    def ready(self):
        """Wire the plugin's extension points.

        - The ``DICOM_UID_KEY`` system check (``checks.py``).
        - File-unlink ``pre_delete`` receiver, so every hard-delete path
          (purge, study delete, account-erasure cascade) cleans the
          filesystem.
        - ``can_read_via_attachment`` read-permission extension, so a study
          attached to a recording inherits the recording's read access.
        - Audit masking for patient demographics: the patient is not a
          platform ``User``, so the subject-erasure registry cannot key on
          them — write-time masking is the only lever that keeps their
          identity out of the permanent audit trail, whether or not the
          study was de-identified at ingest. The as-uploaded digest is
          withheld outright.
        """
        import plugins.dicom.checks
        import plugins.dicom.signals  # noqa: F401
        from activity.audit import register_masked_fields
        from epicurrents.permissions import register_read_permission_extension
        from plugins.dicom.permissions import can_read_via_attachment

        register_read_permission_extension(can_read_via_attachment)

        register_masked_fields(
            "dicom.dicomstudy",
            {
                "patient_name",
                "patient_id",
                "patient_birth_date",
                "patient_sex",
                "patient_age",
                "accession_number",
            },
        )

        # The as-uploaded digest links a stored file to the original held by
        # whoever acquired it; a masking digest of it could be recomputed from
        # that original, so it is withheld.
        register_masked_fields("dicom.dicominstance", {"source_hash"}, withhold=True)

        # Account erasure lists a DICOM author's preserved originals for the
        # operator. Only a rewritten file was preserved; a file the pass refused
        # has no row, and its manifest names the author_id instead.
        from recordings.preservation import register_plugin_originals

        register_plugin_originals("dicom", _preserved_original_keys)

        # Subject-access export. AGENTS.md's rule says projects *and plugins*
        # register their own; the plugin half was missed when the rule landed, and
        # manage.py check refuses to boot a dicom deployment until it is here.
        from user.export import register_export_relation

        register_export_relation(
            "dicom.dicomstudy",
            "author",
            fields=("created_at",),
            title="DICOM studies you imported",
        )


def _preserved_original_keys(user) -> list[str]:
    """The originals-volume directory keys of *user*'s rewritten DICOM files: each stored name's prefix."""
    from plugins.dicom.models import DicomInstance

    names = DicomInstance.objects.filter(
        series__study__author=user, deidentification_version__isnull=False
    ).values_list("stored_name", flat=True)
    return [name.split(".", 1)[0] for name in names]
