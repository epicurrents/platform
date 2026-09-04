"""Django app configuration for the recordings app."""

from django.apps import AppConfig


class RecordingsConfig(AppConfig):
    """Django app configuration for recordings domain."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "recordings"

    def ready(self) -> None:
        """Validate preservation-tier settings + register conversion-hook handlers.

        Settings validation delegates to
        ``recordings.preservation.validate_settings`` so the check is
        testable in isolation. Fails loudly via ``ImproperlyConfigured``
        if ``RECORDINGS_PRESERVE_MODE`` is set to a non-default value
        without a corresponding ``RECORDINGS_ORIGINALS_PATH``.

        The conversion-hook handlers close the Phase 3 ``"failed"``
        preservation gap for converter-bound formats — see
        ``recordings/preservation.py`` for the stash mechanism. The Nicolet
        sidecar handler is registered the same way and is the worked
        example for plugin authors writing format-specific post_convert
        handlers.
        """
        from activity.derived_state import register_derived_state_digester
        from epicurrents.permissions import register_read_visibility_gate
        from recordings.audit_digests import (
            SIGNAL_INFO_DIGEST_KEY,
            compute_signal_info_digest,
        )
        from recordings.converters.sidecar import handle_post_convert
        from recordings.models import Recording
        from recordings.pipelines import (
            register_convert_failed,
            register_post_convert,
            register_pre_convert,
        )
        from recordings.preservation import (
            _on_convert_failed,
            _on_pre_convert,
            validate_settings,
        )

        validate_settings()
        # Patient-side PHI must not persist in audit payloads: the uploaded
        # filename and the processing-error text (paths, traces) are masked
        # at write time. The live row keeps both for the author-only API
        # surfaces; file_path stays unmasked because it derives from the
        # random stored_name and rollback restore needs it.
        from activity.audit import register_masked_fields

        register_masked_fields("recordings.recording", {"original_name", "processing_error"})
        # The bulk-import rows carry the same class of data under different
        # names. `relative_path` is the source filename inside the import tree —
        # `original_name` before a Recording exists — and `source_path` is the
        # operator-supplied directory it came from, which in practice is named
        # after whatever the export was of. `error` is `processing_error`'s
        # counterpart. All three land in the change log on the status write that
        # follows each file, so masking them is what keeps a bulk import from
        # writing to the permanent trail what a single upload does not.
        register_masked_fields("recordings.importjob", {"source_path"})
        register_masked_fields("recordings.importjobfile", {"relative_path", "error"})
        # The read-visibility gate is what makes FAILED / trashed hiding hold
        # on surfaces that resolve recordings through the generic permission
        # resolver rather than the recordings API — see recordings/permissions.py.
        from recordings.permissions import recording_hidden_from_reader

        register_read_visibility_gate("recordings.recording", recording_hidden_from_reader)
        register_pre_convert(_on_pre_convert)
        register_convert_failed(_on_convert_failed)
        register_post_convert(handle_post_convert)
        register_derived_state_digester(
            target_model=Recording,
            key=SIGNAL_INFO_DIGEST_KEY,
            digester=compute_signal_info_digest,
        )
