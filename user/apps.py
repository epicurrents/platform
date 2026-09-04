"""Django app configuration for the user app."""

from django.apps import AppConfig


class UserConfig(AppConfig):
    """Django app configuration for the custom User model."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "user"

    def ready(self):
        """Register audit-trail handling for the app's personal data.

        The password hash is masked out of audit payloads at write time;
        identity fields stay recorded for audit reconstruction but are
        registered for scrubbing on a GDPR Art. 17 erasure request. The
        preference blob is registered for the same reason.

        Group membership additionally gets a derived-state digester, because it
        lives in an M2M table the audit signals never see.
        """
        # Imported for the @register side effect, as activity.checks is: the
        # subject-export registry is validated against the real schema at
        # manage.py check, because every way of getting it wrong is silent.
        from activity.audit import register_masked_fields
        from activity.derived_state import register_derived_state_digester
        from activity.erasure import register_subject_pii
        from user import checks  # noqa: F401

        register_masked_fields("user.user", {"password"})
        # Enrolment and every 2FA login write this row from inside an audited
        # request, so without the mask the shared secret and the recovery-code
        # hashes would be copied into a change log that is deliberately
        # permanent. Masked at write time; registered for erasure as well, so
        # rows written before a mask was in place are still reachable.
        register_masked_fields("user.twofactorcredential", {"secret", "backup_codes"})
        register_subject_pii(
            "user.user",
            owner_field=None,
            pii_fields={
                "username",
                "first_name",
                "last_name",
                "email",
                "password",
            },
        )
        register_subject_pii(
            "user.externalidentity",
            owner_field="user_id",
            pii_fields={"subject", "email"},
        )
        # The preference blob is meant to hold client settings only, and the write endpoint
        # enforces that shape, but a badly named client setting would otherwise sit in the
        # permanent audit trail with no way to erase it.
        register_subject_pii(
            "user.twofactorcredential",
            owner_field="user_id",
            pii_fields={"secret", "backup_codes"},
        )
        register_subject_pii(
            "user.userpreference",
            owner_field="user_id",
            pii_fields={"values"},
        )

        from user.audit_digests import (
            GROUP_MEMBERSHIP_DIGEST_KEY,
            compute_group_membership_digest,
        )

        register_derived_state_digester(
            target_model=self.get_model("User"),
            key=GROUP_MEMBERSHIP_DIGEST_KEY,
            digester=compute_group_membership_digest,
        )
