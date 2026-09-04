"""Serialization, hashing, recording, permission, and rollback helpers
for the activity audit trail.

⚠️ LOAD-BEARING — audit-trail integrity hash.
``compute_audit_hash`` is the versioned dispatcher that produces
``ObjectChangeLog`` row integrity hashes. ``_compute_audit_hash_v1`` is
the legacy SHA-256 fingerprint kept for already-written rows;
``_compute_audit_hash_v2`` is HMAC-SHA256 against a server-side key from
``settings.ACTIVITY_HASH_KEYS[hash_key_version]``;
``_compute_audit_hash_v3`` adds a per-content_type chain pointer
(``prev_hash``) into the HMAC payload. ``create_chained_change_log`` is
the single write entry point — every signal handler and explicit
recorder calls it. ``compute_erased_hash`` is the subject-erasure
counterpart: it re-seals a scrubbed row's payload while the original
``after_hash`` stays in place to keep chain links verifiable
(see ``activity/erasure.py``).

Changing any algorithm, the chain link payload shape, the erased-hash
payload shape, or the genesis-sentinel format invalidates every row
written under it — old rows will compute mismatching hashes on next
read and silently appear tampered. See AGENTS.md → *Load-bearing files*
before modifying; the contract tests are
``activity/tests/test_audit.py`` (``TestComputeAuditHash``,
``TestHashTamperDetection``, ``TestVerifyChangeHash``,
``TestChainWrites``, ``TestChainVerification``) and
``activity/tests/test_erasure.py`` for the erasure tombstones.

The module hosts four concerns (serialization, recording, permission,
rollback) in one file; the split is tracked in ROADMAP under *Activity —
split `audit.py` into focused submodules*.
"""

import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from .models import Activity, ObjectChangeLog
from .request_context import (
    get_current_activity,
    reset_change_logging_suppressed,
    set_change_logging_suppressed,
)


def _json_safe(value):
    """Convert Python values into JSON-serializable forms.

    Unlike ``DjangoJSONEncoder`` (which only applies at ``json.dumps`` time),
    this returns a canonical Python dict so diffing and hashing stay stable
    across the JSONField write/read round-trip.

    ``bytes`` / ``memoryview`` from ``BinaryField`` columns are captured as a
    length sentinel string (``"<bytes:len=N>"``) rather than encoded inline.
    The actual blob can be megabytes (e.g. ``compute.LeadFieldCache.lead_field``),
    and storing it verbatim in every audit row would bloat the log without
    meaningful benefit — the cache is regenerable, rollback of binary payloads
    isn't a supported use case, and the length sentinel still detects size
    changes for tamper detection and diff display.
    """

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<bytes:len={len(bytes(value))}>"
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


# Credential fields that must never reach the audit trail in the clear.
# Keyed by "app_label.model_name"; each set holds field attnames whose
# values are replaced by a masked digest at serialization time. Owning
# apps register their own fields from AppConfig.ready() — see
# user/apps.py and notifications/apps.py for the core registrations.
_MASKED_FIELDS: dict[str, frozenset[str]] = {}

MASK_PREFIX = "<masked:"


def register_masked_fields(model_label: str, fields) -> None:
    """Register credential fields of *model_label* for write-time masking.

    ``model_label`` is the lowercase ``app_label.model_name`` pair. Masking
    replaces the value with an opaque digest sentinel before the state
    reaches ``before_state`` / ``changes`` / the integrity hash, so secrets
    (password hashes, push-encryption keys) never persist in the audit trail
    while unequal secrets still produce a visible diff.
    """
    _MASKED_FIELDS[model_label] = frozenset(fields)


def registered_masked_fields(model_label: str) -> frozenset[str]:
    """The credential fields registered for *model_label*, or an empty set.

    Public because the registry answers a question beyond the audit trail: these
    are the fields of this model that must never leave the system in the clear,
    whichever surface is doing the leaving. ``user.export`` reads it to keep
    credentials out of an Art. 15 subject access export, so one declaration
    covers both — a project registering a new credential field is excluded from
    the export without having to know the export exists.

    Read-only; registration stays with :func:`register_masked_fields`.
    """
    return _MASKED_FIELDS.get(model_label, frozenset())


def _mask_value(value) -> str:
    """Return the masked sentinel for a credential value.

    The digest is over the stored value itself (for a password that is the
    salted hash, so the digest cannot be brute-forced without the salt).
    Equal values mask to equal sentinels, keeping ``diff_states`` quiet when
    a secret did not change and producing a visible — but opaque — diff when
    it did.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"{MASK_PREFIX}{digest}>"


def serialize_instance(instance) -> dict:
    """Serialize concrete model fields to a dict suitable for audit storage.

    Fields registered via ``register_masked_fields`` are replaced by a masked
    digest sentinel; empty values (``None``, ``""``) pass through unmasked
    because they carry no secret material.
    """

    label = f"{instance._meta.app_label}.{instance._meta.model_name}"
    masked = _MASKED_FIELDS.get(label, frozenset())
    data = {}
    for field in instance._meta.concrete_fields:
        value = getattr(instance, field.attname)
        if field.attname in masked and value:
            data[field.attname] = _mask_value(value)
        else:
            data[field.attname] = _json_safe(value)
    return data


class ActivityHashKeyMissing(ImproperlyConfigured):
    """Raised when a v2+ row's ``hash_key_version`` is not present in
    ``settings.ACTIVITY_HASH_KEYS``.

    Happens when the operator removed a key from ``.env`` while rows under
    that key version still exist in the audit trail, or when production
    booted with no HMAC key at all and is now reading a v2 row written
    against a now-absent key. Distinct from ``ChangeHashMismatch``: this is
    a configuration problem, not a tamper signal — surfacing it as a
    different exception lets the rollback path emit a different security
    event and lets the operator notice the misconfig.
    """


def compute_audit_hash(
    after_state: dict,
    *,
    performed_by_id,
    content_type_id,
    object_id: str,
    action: str,
    timestamp: str,
    algorithm: str = "v1",
    hash_key_version: int | None = None,
    prev_hash: str | None = None,
    extra_payload: dict | None = None,
) -> str:
    """Versioned audit-hash dispatcher — the single entry point for computing
    ``ObjectChangeLog`` row integrity hashes.

    The ``algorithm`` kwarg selects the version:

    - ``"v1"`` — plain ``sha256(payload)[:32]`` with no server-side secret.
      Kept for rows written under it.
    - ``"v2"`` — HMAC-SHA256 against a key looked up by ``hash_key_version``
      in ``settings.ACTIVITY_HASH_KEYS``.
    - ``"v3"`` — v2 plus a per-content_type chain pointer. ``prev_hash``
      mixes into the payload; tampering with row N invalidates row N+1
      because N+1's hash was computed against N's now-stale value.

    ``extra_payload`` carries caller-supplied derived-state digests that are
    mixed into the payload only when non-empty. Empty / ``None`` produces
    the same hash a row written before this field existed — existing rows
    keep verifying. Used for high-fanout derived rows (e.g. SignalInfo
    under Recording) that the writer hashes via ``verify_derived_state``
    rather than per-row chain entries.

    For recomputation against an existing row, pass the ``algorithm``,
    ``hash_key_version``, (for v3) ``prev_hash``, and the row's stored
    ``extra_payload``. The write-time helper ``current_write_hash_version``
    reads ``settings.ACTIVITY_HASH_KEY_CURRENT`` to decide which version a
    new row is written under.
    """
    if algorithm == "v1":
        return _compute_audit_hash_v1(
            after_state,
            performed_by_id=performed_by_id,
            content_type_id=content_type_id,
            object_id=object_id,
            action=action,
            timestamp=timestamp,
            extra_payload=extra_payload,
        )
    if algorithm == "v2":
        if hash_key_version is None:
            raise ValueError("hash_key_version is required for algorithm='v2' (the v2 algorithm is HMAC-keyed).")
        return _compute_audit_hash_v2(
            after_state,
            performed_by_id=performed_by_id,
            content_type_id=content_type_id,
            object_id=object_id,
            action=action,
            timestamp=timestamp,
            hash_key_version=hash_key_version,
            extra_payload=extra_payload,
        )
    if algorithm == "v3":
        if hash_key_version is None:
            raise ValueError("hash_key_version is required for algorithm='v3' (the v3 algorithm is HMAC-keyed).")
        if prev_hash is None:
            raise ValueError(
                "prev_hash is required for algorithm='v3' (the v3 algorithm chains each row to the previous one)."
            )
        return _compute_audit_hash_v3(
            after_state,
            performed_by_id=performed_by_id,
            content_type_id=content_type_id,
            object_id=object_id,
            action=action,
            timestamp=timestamp,
            hash_key_version=hash_key_version,
            prev_hash=prev_hash,
            extra_payload=extra_payload,
        )
    raise ValueError(f"Unknown audit-hash algorithm: {algorithm!r}")


def _augment_payload(payload: dict, extra_payload: dict | None) -> dict:
    """Return *payload* unchanged when *extra_payload* is empty / None;
    otherwise add an ``"extra_payload"`` key carrying the JSON-safe copy.

    Conditional inclusion keeps the hash of rows written without
    ``extra_payload`` byte-identical to their pre-Phase-2 form, so the
    schema addition doesn't invalidate any existing audit row.
    """
    if not extra_payload:
        return payload
    return {**payload, "extra_payload": _json_safe(extra_payload)}


def _load_activity_hash_key(hash_key_version: int) -> bytes:
    """Return ``settings.ACTIVITY_HASH_KEYS[hash_key_version]`` or raise."""
    from django.conf import settings

    keys = getattr(settings, "ACTIVITY_HASH_KEYS", {})
    key = keys.get(hash_key_version)
    if key is None:
        raise ActivityHashKeyMissing(
            f"ACTIVITY_HASH_KEY_V{hash_key_version} is not configured. "
            "An audit row references this key version; restore the key "
            "to .env before the row can be verified."
        )
    return key


def current_write_hash_version() -> tuple[str, int | None]:
    """Return ``(algorithm, hash_key_version)`` for new audit-trail rows.

    Reads ``settings.ACTIVITY_HASH_KEYS`` and ``ACTIVITY_HASH_KEY_CURRENT``.
    When a current key is present, returns ``("v3", current_version)`` —
    HMAC + per-content_type chain. When no keys are configured (dev mode
    without ``init_env``, or production misconfig — the ``apps.py`` boot
    guard catches the latter), falls back to ``("v1", None)`` so the audit
    trail keeps recording rather than blocking writes.

    Tests and callsites that need a specific version override the result
    with explicit ``algorithm`` / ``hash_key_version`` kwargs on the
    ``compute_audit_hash`` call. Older versions (v1, v2) remain available
    for recomputation against existing rows.
    """
    from django.conf import settings

    keys = getattr(settings, "ACTIVITY_HASH_KEYS", {})
    current = getattr(settings, "ACTIVITY_HASH_KEY_CURRENT", None)
    if not keys or current is None or current not in keys:
        return "v1", None
    return "v3", current


def _compute_audit_hash_v1(
    after_state: dict,
    *,
    performed_by_id,
    content_type_id,
    object_id: str,
    action: str,
    timestamp: str,
    extra_payload: dict | None = None,
) -> str:
    """v1 algorithm: ``sha256(canonical_json(payload))[:32]``.

    No server-side secret, no chain to the previous row. Detects accidental
    corruption and naive tampering; does **not** withstand an attacker with
    code-level access who re-runs this function against an edited payload.
    See ``activity/README.md`` → *Threat model* for the attacker matrix.

    Kept indefinitely so legacy rows written under this algorithm can still
    verify after later phases (v2, v3) become the default for new writes.
    """
    payload = _augment_payload(
        {
            "after_state": _json_safe(after_state),
            "performed_by_id": performed_by_id,
            "content_type_id": content_type_id,
            "object_id": object_id,
            "action": action,
            "timestamp": timestamp,
        },
        extra_payload,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _compute_audit_hash_v2(
    after_state: dict,
    *,
    performed_by_id,
    content_type_id,
    object_id: str,
    action: str,
    timestamp: str,
    hash_key_version: int,
    extra_payload: dict | None = None,
) -> str:
    """v2 algorithm: ``hmac_sha256(key, canonical_json(payload))[:32]``.

    Loads the HMAC key from ``settings.ACTIVITY_HASH_KEYS[hash_key_version]``;
    raises ``ActivityHashKeyMissing`` when the version is absent. The payload
    shape is identical to v1 so the canonical-JSON bytes are stable across
    algorithm changes — only the keyed hash differs.

    Forgery resistance: an attacker without the key cannot produce a matching
    hash, even with full source-code visibility. The key is the only piece
    not in the repo.
    """
    key = _load_activity_hash_key(hash_key_version)
    payload = _augment_payload(
        {
            "after_state": _json_safe(after_state),
            "performed_by_id": performed_by_id,
            "content_type_id": content_type_id,
            "object_id": object_id,
            "action": action,
            "timestamp": timestamp,
        },
        extra_payload,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()[:32]


def _compute_audit_hash_v3(
    after_state: dict,
    *,
    performed_by_id,
    content_type_id,
    object_id: str,
    action: str,
    timestamp: str,
    hash_key_version: int,
    prev_hash: str,
    extra_payload: dict | None = None,
) -> str:
    """v3 algorithm: HMAC over (v2 payload + previous row's after_hash).

    Same key lookup and HMAC primitive as v2, but the payload additionally
    carries ``prev_hash`` — the ``after_hash`` of the previous row in the
    same per-content_type shard, or the per-shard genesis sentinel for the
    first row. Tampering with row N's contents changes its ``after_hash``,
    which means row N+1's verification recomputes against the new (wrong)
    value and the chain break is detected at N+1.

    Ordering / replay resistance: reordering, deleting middle rows, or
    replaying an old row's contents with a new sequence position all
    invalidate the chain because the prev_hash relationship no longer
    holds.
    """
    key = _load_activity_hash_key(hash_key_version)
    payload = _augment_payload(
        {
            "after_state": _json_safe(after_state),
            "performed_by_id": performed_by_id,
            "content_type_id": content_type_id,
            "object_id": object_id,
            "action": action,
            "timestamp": timestamp,
            "prev_hash": prev_hash,
        },
        extra_payload,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()[:32]


def current_erased_hash_version() -> tuple[str, int | None]:
    """Return ``(algorithm, hash_key_version)`` for sealing erased rows.

    The erased hash has no chain semantics of its own (the original
    ``after_hash`` keeps the row's chain position), so the version space is
    ``"v1"`` (unkeyed SHA-256) or ``"v2"`` (HMAC-SHA256). Follows the
    deployment's current write configuration: a keyed deployment seals
    erasures under HMAC, an unkeyed one falls back to the fingerprint.
    """
    algorithm, key_version = current_write_hash_version()
    if algorithm == "v1":
        return "v1", None
    return "v2", key_version


def compute_erased_hash(
    *,
    before_state: dict,
    changes: dict | None,
    after_hash: str,
    content_type_id,
    object_id: str,
    action: str,
    erased_at: str,
    algorithm: str,
    hash_key_version: int | None,
) -> str:
    """Seal a subject-erased row's scrubbed payload.

    Computed at erasure time over the post-scrub ``before_state`` /
    ``changes`` plus the row's original ``after_hash``, which binds the seal
    to the row's chain position — a scrubbed payload lifted onto another row
    fails verification. ``erased_at`` must be the ISO timestamp stored on
    the row. Verification recomputes under the stored
    ``erased_hash_algorithm`` / ``erased_hash_key_version``.
    """
    payload = {
        "before_state": _json_safe(before_state),
        "changes": _json_safe(changes),
        "after_hash": after_hash,
        "content_type_id": content_type_id,
        "object_id": object_id,
        "action": action,
        "erased_at": erased_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    if algorithm == "v1":
        return hashlib.sha256(encoded).hexdigest()[:32]
    if algorithm == "v2":
        if hash_key_version is None:
            raise ValueError("hash_key_version is required for erased-hash algorithm='v2'.")
        key = _load_activity_hash_key(hash_key_version)
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()[:32]
    raise ValueError(f"Unknown erased-hash algorithm: {algorithm!r}")


def genesis_sentinel(content_type_id: int) -> str:
    """Return the per-shard ``prev_hash`` sentinel for the first row in a
    content_type's chain.

    Encodes the shard so a v3 row lifted from one chain to another fails
    verification — the moved row's stored ``prev_hash`` references the
    source shard's sentinel, which doesn't match the target shard's. 32
    characters wide to match the ``prev_hash`` field length.
    """
    return f"genesis:{content_type_id}".ljust(32, "0")[:32]


# Advisory-lock namespace for the per-content_type chain lock. Arbitrary
# 32-bit integer; just needs to be stable across processes so an unrelated
# advisory-lock user in the same database doesn't collide with the audit
# chain. PG advisory locks are namespaced by (key1, key2) — content_type_id
# is the second key, this constant is the first.
_CHAIN_LOCK_NAMESPACE = 47119


def acquire_chain_lock(content_type_id: int) -> None:
    """Acquire the per-content_type chain lock for the current transaction.

    Postgres uses ``pg_advisory_xact_lock`` so the lock is automatically
    released at transaction commit or rollback. SQLite is a no-op — SQLite
    serializes all writes at the database level, so the chain's read-tail-
    then-insert sequence is already atomic within a transaction without
    additional locking.

    The caller MUST already be inside ``transaction.atomic()``; the lock
    has no effect outside one, and the chain-write sequence (read tail,
    compute prev_hash + sequence_no, insert row) must commit or rollback
    atomically to avoid skipped sequence slots.
    """
    from django.db import connection

    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_CHAIN_LOCK_NAMESPACE, content_type_id],
        )


def chain_tail(content_type) -> "ObjectChangeLog | None":
    """Return the most recent chained row (highest ``sequence_no``) in the
    content_type's shard, or ``None`` when the chain is empty.

    Skips pre-chain rows (v1/v2 with ``sequence_no IS NULL``) because they
    are not part of the linked sequence even though they share the same
    content_type.
    """
    return (
        ObjectChangeLog.objects.filter(
            content_type=content_type,
            sequence_no__isnull=False,
        )
        .order_by("-sequence_no")
        .first()
    )


class ChangeHashMismatch(Exception):
    """Raised when a stored ``ObjectChangeLog.after_hash`` does not match the
    hash recomputed from the row's contents.

    Within the threat model in activity/README.md, this either means accidental
    corruption (partial write, bit flip, schema-migration drift) or naive
    tampering by an actor who edited the row without recomputing the hash. The
    rollback path refuses to apply ``before_state`` from a row that fails
    verification — a tampered row that successfully rolls back would silently
    restore the attacker's chosen state.
    """


def hash_payload_state(action: str, before_state: dict | None, changes: dict | None) -> dict:
    """Return the state dict the audit hash is computed over for a given action.

    The hash payload differs by action — see ``activity/signals.py`` and
    ``rollback_change`` for the write-side shapes:

    - CREATE: hash is over the initial object state, which equals the row's
      stored ``before_state`` (created_state and before_state are the same
      dict at write time).
    - ERASE: hash is over the stored ``before_state``, which carries the
      erasure summary (row counts per model) rather than object state.
    - DELETE: hash is over an empty dict ``{}`` (the action carries no state
      worth hashing beyond the surrounding metadata).
    - MODIFY / ROLLBACK: hash is over the post-change state, which the row
      stores implicitly as ``before_state`` plus the ``changes`` diff. Apply
      each ``{"from": …, "to": new}`` entry to recover the post-state.

    Single source of truth for both write-time hash computation and
    read-time verification. Test helpers building synthetic change-log rows
    should also call this so their stored ``after_hash`` matches what
    ``verify_change_hash`` will recompute.
    """
    if action in (ObjectChangeLog.ACTION_CREATE, ObjectChangeLog.ACTION_ERASE):
        return before_state or {}
    if action == ObjectChangeLog.ACTION_DELETE:
        return {}
    after = dict(before_state or {})
    for field, delta in (changes or {}).items():
        after[field] = delta.get("to")
    return after


def verify_change_hash(change) -> bool:
    """Recompute the integrity hash for *change* and compare to the stored value.

    Returns ``True`` when the row's stored ``after_hash`` matches a fresh
    recomputation under the row's algorithm; ``False`` otherwise. Pure: no
    side effects, no logging. Callers decide whether a mismatch is an alert
    (rollback path: yes, raise + emit ``log_security_event``) or a soft
    annotation (changelog read API: surface a per-row ``verified`` flag).

    Reconstruction of the hash payload is delegated to ``hash_payload_state``.
    The row's stored ``hash_algorithm``, ``hash_key_version``, and (for v3)
    ``prev_hash`` are passed through so each row verifies under the
    algorithm it was written with. ``ActivityHashKeyMissing`` propagates
    when a v2+ row references a key version no longer in settings — the
    caller decides whether that's a tamper signal or a configuration error.

    Subject-erased rows (non-null ``erased_at``) verify against
    ``erased_hash`` instead: the original ``after_hash`` no longer matches
    the scrubbed payload by design, but the erasure routine sealed the
    scrubbed content, so post-erasure tampering is still detectable. The
    ``after_hash`` itself stays authoritative for chain-link checks in
    ``verify_chain``.
    """
    if change.erased_at is not None:
        expected = compute_erased_hash(
            before_state=change.before_state,
            changes=change.changes,
            after_hash=change.after_hash,
            content_type_id=change.content_type_id,
            object_id=change.object_id,
            action=change.action,
            erased_at=change.erased_at.isoformat(),
            algorithm=change.erased_hash_algorithm,
            hash_key_version=change.erased_hash_key_version,
        )
        return expected == change.erased_hash
    expected = compute_audit_hash(
        hash_payload_state(change.action, change.before_state, change.changes),
        performed_by_id=change.performed_by_id,
        content_type_id=change.content_type_id,
        object_id=change.object_id,
        action=change.action,
        timestamp=change.created_at.isoformat(),
        algorithm=change.hash_algorithm,
        hash_key_version=change.hash_key_version,
        prev_hash=change.prev_hash if change.hash_algorithm == "v3" else None,
        extra_payload=change.extra_payload or None,
    )
    return expected == change.after_hash


class ChainVerificationResult:
    """Outcome of a per-content_type chain walk.

    Attributes:

    - ``content_type_id`` — the shard walked.
    - ``rows_checked`` — number of chained (v3) rows verified.
    - ``first_break_sequence_no`` — ``sequence_no`` of the first row whose
      hash recomputation failed, or ``None`` when the whole chain verified.
      The verification continues past the break to count downstream
      failures, since every row N+k after a break has a stale
      ``prev_hash`` reference too.
    - ``downstream_break_count`` — number of rows after the first break that
      also fail recomputation. Useful for SIEM alerting: a chain with a
      single bad row + healthy tail looks very different from a chain that
      was retroactively rewritten end-to-end.
    - ``gap_sequence_nos`` — list of missing ``sequence_no`` values between
      the first row and the last row in the chain. A non-empty list means
      rows were deleted; the chain itself may still verify for the rows
      that remain.
    - ``genesis_ok`` — ``True`` when the first row's ``prev_hash`` matches
      ``genesis_sentinel(content_type_id)``. A False here means either the
      chain was lifted from another shard or the first row's prev_hash was
      tampered with.

    ``ok`` is the headline bool — True only when there is no break, no
    gap, and the genesis sentinel matches.
    """

    def __init__(
        self,
        content_type_id: int,
        rows_checked: int,
        first_break_sequence_no: int | None,
        downstream_break_count: int,
        gap_sequence_nos: list[int],
        genesis_ok: bool,
    ):
        self.content_type_id = content_type_id
        self.rows_checked = rows_checked
        self.first_break_sequence_no = first_break_sequence_no
        self.downstream_break_count = downstream_break_count
        self.gap_sequence_nos = gap_sequence_nos
        self.genesis_ok = genesis_ok

    @property
    def ok(self) -> bool:
        return self.first_break_sequence_no is None and not self.gap_sequence_nos and self.genesis_ok

    def __repr__(self) -> str:
        return (
            f"ChainVerificationResult(content_type_id={self.content_type_id}, "
            f"rows_checked={self.rows_checked}, "
            f"first_break_sequence_no={self.first_break_sequence_no}, "
            f"downstream_break_count={self.downstream_break_count}, "
            f"gap_sequence_nos={self.gap_sequence_nos}, "
            f"genesis_ok={self.genesis_ok}, ok={self.ok})"
        )


def verify_chain(content_type) -> ChainVerificationResult:
    """Walk the chained (v3) rows for *content_type* in ``sequence_no``
    order and report on chain integrity.

    Pre-chain rows (``sequence_no IS NULL``) are excluded — they verify
    individually via ``verify_change_hash`` under their own algorithm but
    are not part of the linked sequence.

    The walk does not stop at the first break — every row past a break
    has a stale ``prev_hash`` reference, so reporting the downstream count
    distinguishes "one tampered row in an otherwise healthy chain" from
    "retroactive end-to-end rewrite." The caller (typically the periodic
    integrity-check task) decides what to do with each finding.
    """
    rows = list(
        ObjectChangeLog.objects.filter(
            content_type=content_type,
            sequence_no__isnull=False,
        ).order_by("sequence_no")
    )

    if not rows:
        return ChainVerificationResult(
            content_type_id=content_type.pk,
            rows_checked=0,
            first_break_sequence_no=None,
            downstream_break_count=0,
            gap_sequence_nos=[],
            genesis_ok=True,
        )

    # Genesis: the first row's prev_hash must be the per-shard sentinel.
    genesis_ok = rows[0].prev_hash == genesis_sentinel(content_type.pk)

    # Gap detection: enumerate missing sequence_no values within the
    # observed range.
    observed_seq = {row.sequence_no for row in rows}
    expected_seq = set(range(rows[0].sequence_no, rows[-1].sequence_no + 1))
    gaps = sorted(expected_seq - observed_seq)

    # Two break classes:
    # - Content: the row's own ``after_hash`` does not recompute to the
    #   stored value (verify_change_hash returns False).
    # - Link: ``row[N+1].prev_hash`` does not match ``row[N].after_hash``.
    #   Catches the "tamper + recompute that row's hash to cover the
    #   single-row check" attack — the attacker would have to also
    #   recompute every subsequent row's hash to keep the links intact,
    #   which forces full-tail rewriting.
    first_break = None
    additional_breaks = 0
    prev_row = None
    for row in rows:
        broken = not verify_change_hash(row)
        if prev_row is not None and row.prev_hash != prev_row.after_hash:
            broken = True
        if broken:
            if first_break is None:
                first_break = row.sequence_no
            else:
                additional_breaks += 1
        prev_row = row

    return ChainVerificationResult(
        content_type_id=content_type.pk,
        rows_checked=len(rows),
        first_break_sequence_no=first_break,
        downstream_break_count=additional_breaks,
        gap_sequence_nos=gaps,
        genesis_ok=genesis_ok,
    )


def diff_states(before_state: dict, after_state: dict) -> dict:
    """Return field-level delta between two serialized states."""

    all_keys = set(before_state.keys()) | set(after_state.keys())
    changes = {}
    for key in all_keys:
        before_value = before_state.get(key)
        after_value = after_state.get(key)
        if before_value != after_value:
            changes[key] = {
                "from": before_value,
                "to": after_value,
            }
    return changes


def record_api_activity(
    request,
    verb: str,
    *,
    status_code: int | None = None,
    target_obj=None,
    target_identifier: str | None = None,
    metadata: dict | None = None,
):
    """Create an Activity row for a request-level event."""

    # Only log actor for logged-in users.
    actor = getattr(request, "user", None)
    if actor and not getattr(actor, "is_authenticated", False):
        actor = None

    method = getattr(request, "method", "") or ""
    path = getattr(request, "path", "") or ""

    target_content_type = None
    target_object_id = ""
    if target_obj is not None:
        target_content_type = ContentType.objects.get_for_model(target_obj, for_concrete_model=False)
        target_object_id = str(getattr(target_obj, "pk", ""))

    activity = Activity.objects.create(
        actor=actor,
        verb=verb,
        method=method,
        path=path,
        status_code=status_code,
        target_content_type=target_content_type,
        target_object_id=target_object_id,
        target_identifier=target_identifier or path,
        metadata=metadata or {},
    )
    return activity


def log_activity(
    verb: str,
    *,
    target=None,
    metadata: dict | None = None,
) -> None:
    """Annotate the current request's ``Activity`` row with semantic context.

    Intended call site: after the endpoint resolves its work but before
    returning the response.

    The middleware created the ``Activity`` row at request entry with the
    default verb (lowercased HTTP method) and no target. This helper
    overrides those defaults with the operation-specific semantics:

    - ``verb`` — required. Which verb to use is settled by
      ``activity/README.md`` → *Verb taxonomy*, which names the base
      actions and the distinctions the platform draws between them; the
      verbs already in use are listed per app in the *Verb registry*
      after it, which a new verb joins in the commit that introduces it.
    - ``target`` — optional model instance the request operated on. When
      passed, ``target_content_type`` and ``target_object_id`` are filled
      in from the instance. Listing endpoints and bulk actions without a
      single target row pass ``None``.
    - ``metadata`` — optional dict merged into ``activity.metadata``.
      Existing keys on the live activity are preserved; new keys override
      colliding entries. Used only for context that is NOT already
      recoverable from ``target`` or its linked ``ObjectChangeLog`` row:

      - counts, filter parameters, queries (list/search/probe endpoints
        that produce no ``ObjectChangeLog`` row);
      - derived insights — ``fields_updated``, ``key_changed``,
        ``rolled_back_count``, comparison results;
      - bulk-operation identifiers when ``target_*`` only fits one row
        (e.g. ``change_ids`` for ``activity.rollback.bulk``).

      Do NOT duplicate fields already present in the target's snapshot
      (``peer_url`` on a peer create / delete, ``endpoint`` on a
      subscription create) — the linked CL row is joinable via
      ``activity_id`` and is the source of truth for the snapshot.

    No-op when there is no request-scoped Activity (signals fired outside
    an API request — Celery tasks, management commands, the shell).

    Only the fields actually mutated by this call are passed to
    ``save(update_fields=...)`` so concurrent writes to other Activity
    fields (e.g. the middleware's exit-time ``status_code``) are
    preserved.

    Example::

        log_activity(
            verb="notifications.subscription.create",
            target=subscription,
            metadata={"upserted": not created},
        )

    For destructive operations, call ``log_activity`` BEFORE the delete
    inside a ``transaction.atomic()`` block so ``target.pk`` is still
    populated when the audit row is updated and the two writes commit
    or roll back together::

        with transaction.atomic():
            log_activity(verb="federation.peer.delete", target=peer)
            peer.delete()
    """
    activity = get_current_activity()
    if activity is None:
        return

    update_fields = ["verb"]
    activity.verb = verb

    if target is not None:
        # ``target.pk`` may be None for an in-memory instance that was
        # never persisted (e.g. a model created in a failed transaction)
        # — in that case we still set the content type so the row's app /
        # model is queryable, but skip the object_id. This is rare;
        # typical callers pass a saved instance.
        pk = getattr(target, "pk", None)
        activity.target_content_type = ContentType.objects.get_for_model(target, for_concrete_model=False)
        update_fields.append("target_content_type")
        if pk is not None:
            activity.target_object_id = str(pk)
            update_fields.append("target_object_id")

    if metadata:
        activity.metadata = {**(activity.metadata or {}), **metadata}
        update_fields.append("metadata")

    activity.save(update_fields=update_fields)


def create_chained_change_log(
    *,
    content_type,
    object_id: str,
    action: str,
    performed_by,
    before_state: dict,
    changes: dict | None,
    hash_payload: dict,
    timestamp,
    activity=None,
    project: str = "",
    extra_payload: dict | None = None,
):
    """Create an ``ObjectChangeLog`` row, chaining it under v3 when a key is
    configured and writing standalone (v1/v2) otherwise.

    Single source of truth for ``ObjectChangeLog`` writes from production
    code — every signal handler and explicit recorder funnels through here
    so the chain-lock + tail-read + sequence-no allocation logic lives in
    exactly one place. Callers pass the pre-computed ``hash_payload`` (what
    the hash is computed over for the given action; see
    ``hash_payload_state``) so the function does not have to re-derive it
    from before_state / changes / action again.

    ``extra_payload`` is stored verbatim on the row and mixed into the
    audit hash. Used for derived-state digests (see ``verify_derived_state``
    and AGENTS.md → *Bulk ORM operations bypass the audit signal*). Pass
    ``None`` or empty dict for ordinary writes so the hash stays
    byte-identical to its pre-extra_payload form.

    When ``activity`` is omitted (or explicitly ``None``), falls back to
    ``get_current_activity()`` so callers inside an audited scope attach
    to the surrounding parent without threading it through every call
    site. The signal-driven path always passes its resolved activity
    value explicitly; the fallback only triggers for callers that don't
    bother (or for tests that want default behaviour).

    Wraps the chain-write sequence in ``transaction.atomic()`` so the
    advisory lock is held until the row is durably written and the
    sequence_no cannot be skipped by a concurrent rollback.
    """
    if activity is None:
        activity = get_current_activity()
    algorithm, key_version = current_write_hash_version()
    stored_extra = dict(extra_payload) if extra_payload else {}

    if algorithm != "v3":
        return ObjectChangeLog.objects.create(
            activity=activity,
            project=project,
            content_type=content_type,
            object_id=object_id,
            action=action,
            performed_by=performed_by,
            before_state=before_state,
            changes=changes,
            extra_payload=stored_extra,
            hash_algorithm=algorithm,
            hash_key_version=key_version,
            after_hash=compute_audit_hash(
                hash_payload,
                performed_by_id=getattr(performed_by, "pk", None),
                content_type_id=content_type.pk,
                object_id=object_id,
                action=action,
                timestamp=timestamp.isoformat(),
                algorithm=algorithm,
                hash_key_version=key_version,
                extra_payload=extra_payload or None,
            ),
            created_at=timestamp,
        )

    # Chain path — acquire the per-content_type advisory lock so the
    # read-tail-then-insert sequence is serialised against concurrent
    # writers on the same shard.
    with transaction.atomic():
        acquire_chain_lock(content_type.pk)
        tail = chain_tail(content_type)
        if tail is None:
            prev_hash = genesis_sentinel(content_type.pk)
            sequence_no = 1
        else:
            prev_hash = tail.after_hash
            sequence_no = tail.sequence_no + 1

        return ObjectChangeLog.objects.create(
            activity=activity,
            project=project,
            content_type=content_type,
            object_id=object_id,
            action=action,
            performed_by=performed_by,
            before_state=before_state,
            changes=changes,
            extra_payload=stored_extra,
            hash_algorithm=algorithm,
            hash_key_version=key_version,
            prev_hash=prev_hash,
            sequence_no=sequence_no,
            after_hash=compute_audit_hash(
                hash_payload,
                performed_by_id=getattr(performed_by, "pk", None),
                content_type_id=content_type.pk,
                object_id=object_id,
                action=action,
                timestamp=timestamp.isoformat(),
                algorithm=algorithm,
                hash_key_version=key_version,
                prev_hash=prev_hash,
                extra_payload=extra_payload or None,
            ),
            created_at=timestamp,
        )


def record_create_change(
    *,
    actor,
    obj,
    activity: Activity | None = None,
    project: str = "",
    extra_payload: dict | None = None,
):
    """Create a create change-log entry for a newly created object.

    ``before_state`` stores the initial field values of the created object
    (analogous to how ``record_delete_change`` stores the final state before
    deletion). Rolling back a create entry deletes the object; the rollback
    log entry captures the deleted state so the deletion is itself undoable.

    The ``activity`` parameter is forwarded to ``create_chained_change_log``;
    leaving it ``None`` defers to that helper's current-activity fallback.
    """
    created_state = serialize_instance(obj)
    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    now = timezone.now()
    return create_chained_change_log(
        content_type=content_type,
        object_id=str(obj.pk),
        action=ObjectChangeLog.ACTION_CREATE,
        performed_by=actor,
        before_state=created_state,
        changes=None,
        hash_payload=created_state,
        timestamp=now,
        activity=activity,
        project=project,
        extra_payload=extra_payload,
    )


def record_modify_change(
    *,
    actor,
    obj,
    before_state: dict,
    activity: Activity | None = None,
    project: str = "",
    extra_payload: dict | None = None,
):
    """Create a modify change-log entry for an updated object.

    ``extra_payload`` rides on the audit hash and gets stored on the row.
    Used to cover derived state that bulk-creates / bulk-updates wouldn't
    otherwise audit — pass a dict whose keys match digester registrations
    in ``activity.derived_state.register_derived_state_digester``.
    """

    after_state = serialize_instance(obj)
    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    now = timezone.now()
    return create_chained_change_log(
        content_type=content_type,
        object_id=str(obj.pk),
        action=ObjectChangeLog.ACTION_MODIFY,
        performed_by=actor,
        before_state=before_state,
        changes=diff_states(before_state, after_state),
        hash_payload=after_state,
        timestamp=now,
        activity=activity,
        project=project,
        extra_payload=extra_payload,
    )


def record_delete_change(
    *,
    actor,
    obj,
    before_state: dict,
    activity: Activity | None = None,
    project: str = "",
    extra_payload: dict | None = None,
):
    """Create a delete change-log entry for a deleted object."""

    content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
    now = timezone.now()
    return create_chained_change_log(
        content_type=content_type,
        object_id=str(obj.pk),
        action=ObjectChangeLog.ACTION_DELETE,
        performed_by=actor,
        before_state=before_state,
        changes=None,
        hash_payload={},
        timestamp=now,
        activity=activity,
        project=project,
        extra_payload=extra_payload,
    )


def _has_write_access_for_ref(user, change: ObjectChangeLog) -> bool:
    """Check AccessRight write permission using content_type + object_id reference."""

    from epicurrents.models import AccessRight

    return AccessRight.objects.has_permission_for_ref(
        user=user,
        content_type=change.content_type,
        object_id=change.object_id,
        permission_field="can_write",
    )


def can_rollback_change(user, change: ObjectChangeLog, existing_obj=None) -> bool:
    """Return True when user may rollback the given change log entry.

    Checks (in order):
    1. Superuser — always allowed.
    2. ACTION_CREATE entries — authorship only (``can_modify_object``).
       Rolling back a creation destroys the object, and a ``can_write``
       grant is a collaboration permission, not a deletion permission: a
       grantee who could roll back the CREATE entry of a shared object
       would hold a destruction path its owner never delegated. The
       object is fetched here when ``existing_obj`` is not supplied, so
       API pre-flight checks resolve authorship the same way the
       execution path does.
    3. Object authorship — if ``existing_obj`` is provided and the user is its
       author (via ``can_modify_object``).
    4. Write access right — an active ``AccessRight`` with ``can_write=True``
       on the target object.

    ``existing_obj`` is optional so this function can be used both from within
    ``rollback_change`` (where the object is already fetched) and from API
    pre-flight checks (where ``None`` is passed). When ``existing_obj`` is
    ``None``, the authorship check (step 3) is skipped for non-CREATE
    entries — only the stored ``AccessRight`` rows for the object are
    consulted.
    """

    from epicurrents.permissions import can_modify_object

    if user and getattr(user, "is_superuser", False):
        return True

    if change.action == ObjectChangeLog.ACTION_CREATE:
        if existing_obj is None:
            model_class = change.content_type.model_class()
            if model_class is None:
                return False
            existing_obj = model_class._default_manager.filter(pk=change.object_id).first()
        return existing_obj is not None and can_modify_object(user=user, obj=existing_obj)

    if existing_obj is not None and can_modify_object(user=user, obj=existing_obj):
        return True

    return _has_write_access_for_ref(user=user, change=change)


def _restore_object_state(model_class, state: dict, *, suppress_auto_change_log: bool = False):
    """Restore persisted object state into the primary database table.

    Masked credential sentinels (see ``register_masked_fields``) are skipped —
    the audit trail deliberately never stored the real value, so restoring
    the sentinel would clobber a live secret with an opaque digest string.
    The field keeps whatever value the live row (or the model default) has.
    """

    pk_field = model_class._meta.pk.attname
    object_pk = state.get(pk_field)
    instance = model_class._default_manager.filter(pk=object_pk).first()
    if instance is None:
        instance = model_class()

    concrete_fields_by_name = {field.attname: field for field in model_class._meta.concrete_fields}
    for key, value in state.items():
        field = concrete_fields_by_name.get(key)
        if field is None:
            continue
        if isinstance(field, models.BinaryField):
            continue
        if isinstance(value, str) and value.startswith(MASK_PREFIX):
            continue
        setattr(instance, key, value)

    token = None
    if suppress_auto_change_log:
        token = set_change_logging_suppressed(True)

    try:
        with transaction.atomic():
            instance.save()
    finally:
        if token is not None:
            reset_change_logging_suppressed(token)

    return instance


def _create_rollback_activity(*, user, change: ObjectChangeLog):
    """Create a dedicated Activity record describing rollback execution."""

    current_activity = get_current_activity()
    path = current_activity.path if current_activity is not None else "rollback"
    method = current_activity.method if current_activity is not None else "INTERNAL"

    return Activity.objects.create(
        actor=user if user and getattr(user, "is_authenticated", False) else None,
        verb="rollback",
        method=method,
        path=path,
        status_code=200,
        target_content_type=change.content_type,
        target_object_id=change.object_id,
        target_identifier=f"{change.content_type.app_label}.{change.content_type.model}:{change.object_id}",
        metadata={"source_change_id": change.pk},
    )


def rollback_change(*, user, change_id: int):
    """Rollback an object to the state saved in a change log entry.

    For ACTION_CREATE entries the rollback is a deletion — soft (via
    ``deleted_at``, preserving the model's trash / retention pipeline) when
    the model supports it, hard otherwise. Returns None in that case
    (rather than a restored instance). The rollback is itself logged as
    ACTION_ROLLBACK with ``before_state`` capturing the deleted object's
    fields, so the deletion can be undone by rolling back the rollback
    entry.

    For all other actions the object is restored to its pre-change state and
    the restored instance is returned.
    """
    change = ObjectChangeLog.objects.select_related("content_type").get(pk=change_id)
    if change.action == ObjectChangeLog.ACTION_ERASE:
        raise ValueError("Erasure records cannot be rolled back — they carry no object state.")
    if change.erased_at is not None:
        raise ValueError(
            "This change log entry was subject-erased under GDPR Art. 17; "
            "its state payload no longer exists and cannot be restored."
        )
    model_class = change.content_type.model_class()
    if model_class is None:
        raise ValueError("Target model for change log no longer exists")

    existing_obj = model_class._default_manager.filter(pk=change.object_id).first()
    if not can_rollback_change(user=user, change=change, existing_obj=existing_obj):
        raise PermissionError("You do not have permission to rollback this object state")

    # Refuse to apply before_state from a row whose integrity hash no longer
    # matches its contents. A tampered row that silently rolls back would
    # restore the attacker's chosen state; refusing here turns that into a
    # visible, alertable failure.
    if not verify_change_hash(change):
        from epicurrents.security_log import log_security_event

        log_security_event(
            "audit.hash_verification_failed",
            change_id=change.pk,
            content_type=f"{change.content_type.app_label}.{change.content_type.model}",
            object_id=change.object_id,
            action=change.action,
            actor_id=getattr(user, "pk", None),
        )
        raise ChangeHashMismatch(f"Integrity hash mismatch on change log entry {change.pk}; refusing to rollback.")

    # Capture state before we apply the rollback — this is stored on the
    # ACTION_ROLLBACK log entry so the rollback itself is undoable.
    rollback_before_state = serialize_instance(existing_obj) if existing_obj is not None else {}

    if change.action == ObjectChangeLog.ACTION_CREATE:
        # Rolling back a create = deleting the object. This is destructive:
        # the ACTION_ROLLBACK entry records the deleted state so it can be
        # re-created by rolling back the rollback entry.
        if existing_obj is None:
            raise ValueError("Created object no longer exists; the creation cannot be rolled back.")
        token = set_change_logging_suppressed(True)
        try:
            with transaction.atomic():
                if hasattr(existing_obj, "deleted_at"):
                    # Soft-deletable models go through their own trash /
                    # retention pipeline instead of being hard-deleted —
                    # a rollback must not bypass the purge window that
                    # the normal delete endpoints enforce.
                    existing_obj.deleted_at = timezone.now()
                    existing_obj.save(update_fields=["deleted_at"])
                else:
                    existing_obj.delete()
        finally:
            reset_change_logging_suppressed(token)
        restored = None
        restored_state = serialize_instance(existing_obj) if hasattr(existing_obj, "deleted_at") else {}
    else:
        restored = _restore_object_state(
            model_class=model_class,
            state=change.before_state,
            suppress_auto_change_log=True,
        )
        restored_state = serialize_instance(restored)

    rollback_activity = _create_rollback_activity(user=user, change=change)
    performed_by = user if user and getattr(user, "is_authenticated", False) else None
    now = timezone.now()

    create_chained_change_log(
        content_type=change.content_type,
        object_id=change.object_id,
        action=ObjectChangeLog.ACTION_ROLLBACK,
        performed_by=performed_by,
        before_state=rollback_before_state,
        changes=diff_states(rollback_before_state, restored_state),
        hash_payload=restored_state,
        timestamp=now,
        activity=rollback_activity,
        project=change.project,  # inherit project scope from the original change
    )

    return restored


def snapshot_instance(instance):
    """Return a model snapshot including primary key for ad-hoc auditing."""

    return model_to_dict(
        instance,
        fields=[f.name for f in instance._meta.concrete_fields if f.name != "id"],
    ) | {instance._meta.pk.attname: getattr(instance, instance._meta.pk.attname)}
