"""Django app configuration for the user app."""

from django.apps import AppConfig


def refuse_agreement_rollback(change, existing_obj) -> str:
    """The rollback guard for ``user.useragreement``: every rollback is refused."""
    return "A user agreement is published with publish_agreement, and a published version is never changed."


def refuse_acceptance_rollback(change, existing_obj) -> str:
    """The rollback guard for ``user.useragreementacceptance``: every rollback is refused."""
    return "An acceptance of the user agreement is made by the account holder, not by rollback."


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

        Affiliations register as dedicated groups, and their marker rows refuse
        every rollback (user/affiliations.py). Published agreement versions and
        acceptances refuse every rollback as well (user/agreement.py).
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

        # When an account accepted the agreement says what its holder did and when, as a seen marker does; the row
        # goes with the account, and the trail's copies are scrubbed with it.
        register_subject_pii(
            "user.useragreementacceptance",
            owner_field="user_id",
            pii_fields={"accepted_at"},
        )

        from activity.audit import register_rollback_guard
        from user.affiliations import AFFILIATION_KIND, refuse_affiliation_rollback, resolve_affiliations
        from user.dedicated_groups import register_dedicated_group_resolver

        register_dedicated_group_resolver(AFFILIATION_KIND, resolve_affiliations)
        register_rollback_guard("user.affiliation", refuse_affiliation_rollback)
        # A published version is the record of what was accepted, and an acceptance is the account holder's own act;
        # neither is restored or removed by rollback.
        register_rollback_guard("user.useragreement", refuse_agreement_rollback)
        register_rollback_guard("user.useragreementacceptance", refuse_acceptance_rollback)

        from user.audit_digests import (
            GROUP_MEMBERSHIP_DIGEST_KEY,
            compute_group_membership_digest,
        )

        register_derived_state_digester(
            target_model=self.get_model("User"),
            key=GROUP_MEMBERSHIP_DIGEST_KEY,
            digester=compute_group_membership_digest,
        )
