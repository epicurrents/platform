"""Periodic audit-trail integrity check.

Consumes ``verify_chain`` and ``verify_derived_state`` from a single
entry point — ``run_integrity_check`` — that the
``verify_audit_integrity`` Celery task wraps for the scheduled
invocation. Designed so an operator can also call it ad-hoc from a
shell when investigating a suspected tamper.

Two layers of checks:

1. **Per-content_type chain verification.** Walks every shard that has
   v3 rows and calls ``verify_chain``. Detects content-hash tampering,
   chain-link breaks, missing ``sequence_no`` values (gaps), and
   genesis-sentinel mismatches.

2. **Per-row derived-state recompute.** Walks recent rows that carry a
   non-empty ``extra_payload`` and runs ``verify_derived_state`` on
   each. Detects tampering with the dependent rows the digest covers
   (e.g. ``SignalInfo`` for a ``Recording``). Scoped to a sliding
   window (``ACTIVITY_DERIVED_CHECK_WINDOW_DAYS``, default 7) because
   the per-row cost is dominated by the digester function; chain
   verification covers older rows comprehensively at a lower cost.

Each anomaly emits a structured security event via
``epicurrents.security_log.log_security_event``. Clean runs log a
single INFO summary line.

Limitations:

- v1 / v2 legacy rows are not walked by this check. They are
  individually verifiable via ``verify_change_hash`` and any tamper is
  detected when the rollback path touches them. A future sweep over
  legacy rows could be added if forensic completeness becomes
  mandatory.
- A missing HMAC key for the row's ``hash_key_version`` raises
  ``ActivityHashKeyMissing`` inside the verification path; the check
  catches it per content_type, emits an event, and continues with the
  next shard so a single missing key does not silently skip the rest.
"""

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from epicurrents.security_log import log_security_event

from .audit import ActivityHashKeyMissing, verify_chain
from .derived_state import verify_derived_state
from .models import ObjectChangeLog

logger = logging.getLogger(__name__)


def run_integrity_check(*, derived_window_days: int | None = None) -> dict[str, Any]:
    """Run chain + derived-state integrity checks. Emit security events
    on every anomaly and return a summary dict for the caller.

    Pass ``derived_window_days`` to override the
    ``ACTIVITY_DERIVED_CHECK_WINDOW_DAYS`` setting (default 7). Set to
    ``0`` to skip the derived-state phase entirely.
    """

    if derived_window_days is None:
        derived_window_days = getattr(settings, "ACTIVITY_DERIVED_CHECK_WINDOW_DAYS", 7)

    chain_summary = _run_chain_phase()
    derived_summary = _run_derived_phase(derived_window_days)

    summary = {
        "chains_checked": chain_summary["chains_checked"],
        "chain_rows_checked": chain_summary["rows_checked"],
        "chain_breaks": chain_summary["breaks"],
        "chain_gaps": chain_summary["gaps"],
        "genesis_invalid": chain_summary["genesis_invalid"],
        "key_missing": chain_summary["key_missing"],
        "derived_rows_checked": derived_summary["rows_checked"],
        "derived_mismatches": derived_summary["mismatches"],
        "derived_no_digester": derived_summary["no_digester"],
    }
    anomaly_count = (
        summary["chain_breaks"]
        + len(summary["chain_gaps"])
        + summary["genesis_invalid"]
        + summary["key_missing"]
        + summary["derived_mismatches"]
    )
    if anomaly_count == 0:
        logger.info(
            "verify_audit_integrity: clean chains=%d chain_rows=%d derived_rows=%d (window_days=%d)",
            summary["chains_checked"],
            summary["chain_rows_checked"],
            summary["derived_rows_checked"],
            derived_window_days,
        )
    else:
        logger.warning(
            "verify_audit_integrity: anomalies=%d chains=%d chain_breaks=%d "
            "gaps=%d genesis_invalid=%d key_missing=%d "
            "derived_mismatches=%d (window_days=%d)",
            anomaly_count,
            summary["chains_checked"],
            summary["chain_breaks"],
            len(summary["chain_gaps"]),
            summary["genesis_invalid"],
            summary["key_missing"],
            summary["derived_mismatches"],
            derived_window_days,
        )
    return summary


def _run_chain_phase() -> dict[str, Any]:
    """Walk every v3 shard's chain. Emit one event per anomaly class."""

    shard_ct_ids = list(
        ObjectChangeLog.objects.filter(hash_algorithm="v3").values_list("content_type_id", flat=True).distinct()
    )
    chains_checked = 0
    rows_checked = 0
    breaks = 0
    gaps: list[int] = []
    genesis_invalid = 0
    key_missing = 0

    for ct_id in shard_ct_ids:
        try:
            ct = ContentType.objects.get_for_id(ct_id)
        except ContentType.DoesNotExist:
            continue
        try:
            result = verify_chain(ct)
        except ActivityHashKeyMissing as exc:
            key_missing += 1
            log_security_event(
                "audit.hash_key_missing",
                content_type=f"{ct.app_label}.{ct.model}",
                reason=str(exc),
            )
            continue
        chains_checked += 1
        rows_checked += result.rows_checked

        if result.first_break_sequence_no is not None:
            breaks += 1 + result.downstream_break_count
            log_security_event(
                "audit.chain_break",
                content_type=f"{ct.app_label}.{ct.model}",
                first_break_sequence_no=result.first_break_sequence_no,
                downstream_break_count=result.downstream_break_count,
            )
        if result.gap_sequence_nos:
            gaps.extend(result.gap_sequence_nos)
            log_security_event(
                "audit.chain_gap",
                content_type=f"{ct.app_label}.{ct.model}",
                missing_sequence_nos=result.gap_sequence_nos,
            )
        if not result.genesis_ok:
            genesis_invalid += 1
            log_security_event(
                "audit.genesis_invalid",
                content_type=f"{ct.app_label}.{ct.model}",
            )

    return {
        "chains_checked": chains_checked,
        "rows_checked": rows_checked,
        "breaks": breaks,
        "gaps": gaps,
        "genesis_invalid": genesis_invalid,
        "key_missing": key_missing,
    }


def _run_derived_phase(window_days: int) -> dict[str, Any]:
    """Walk recent rows with extra_payload; emit events on mismatches."""

    if window_days <= 0:
        return {"rows_checked": 0, "mismatches": 0, "no_digester": 0}

    cutoff = timezone.now() - timedelta(days=window_days)
    queryset = ObjectChangeLog.objects.filter(created_at__gte=cutoff)

    rows_checked = 0
    mismatches = 0
    no_digester = 0
    for row in queryset.iterator():
        if not row.extra_payload:
            continue
        rows_checked += 1
        result = verify_derived_state(row)
        for key, verdict in result.digests.items():
            if verdict == "mismatch":
                mismatches += 1
                log_security_event(
                    "audit.derived_state_mismatch",
                    change_id=row.pk,
                    content_type=f"{row.content_type.app_label}.{row.content_type.model}",
                    object_id=row.object_id,
                    digest_key=key,
                )
            elif verdict == "no_digester":
                no_digester += 1
                log_security_event(
                    "audit.derived_state_no_digester",
                    change_id=row.pk,
                    content_type=f"{row.content_type.app_label}.{row.content_type.model}",
                    digest_key=key,
                )

    return {
        "rows_checked": rows_checked,
        "mismatches": mismatches,
        "no_digester": no_digester,
    }
