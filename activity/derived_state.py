"""Derived-state digest registry and verifier.

When a single audit row carries digests of dependent rows it doesn't
record individually (e.g. ``SignalInfo`` under ``Recording``), the digest
is stored in ``ObjectChangeLog.extra_payload`` and mixed into the row's
``after_hash``. Tampering with the row's stored digest breaks chain
verification; tampering with the dependent rows themselves is caught by
``verify_derived_state``, which re-derives the digest from current DB
state and compares.

Apps register a digester per ``(target model, payload key)`` pair at
``AppConfig.ready`` time. The verifier looks up the registration via the
audit row's ``content_type`` and each key in ``extra_payload``, calls the
digester on the live target object, and compares to the stored value.

Recompute is on-demand: nothing here runs on a schedule. The future
periodic-integrity Celery task can call ``verify_derived_state`` on a
sliding window of recent rows; that's tracked in ROADMAP rather than
landing here, since the cadence + alerting shape is a separate decision.
"""

from collections.abc import Callable

from .models import ObjectChangeLog

_DIGESTERS: dict[tuple[type, str], Callable[[object], str]] = {}


def register_derived_state_digester(
    *,
    target_model: type,
    key: str,
    digester: Callable[[object], str],
) -> None:
    """Register a callable that recomputes a derived-state digest.

    ``target_model`` is the model class whose audit rows carry the digest
    in their ``extra_payload``. ``key`` is the dict key under which the
    writer stored the hex digest. ``digester`` takes the target instance
    and returns a hex string equal to what the writer hashed.

    Registration is idempotent — re-registering the same ``(target_model,
    key)`` pair overwrites the previous entry (so app ``ready()`` re-runs
    during test bootstrap don't accumulate stale callables).
    """
    _DIGESTERS[(target_model, key)] = digester


class DerivedStateVerificationResult:
    """Outcome of recomputing an audit row's derived-state digests.

    Attributes:

    - ``change_id`` — primary key of the audit row that was verified.
    - ``target_loaded`` — ``True`` when the row's ``content_object`` could be
      loaded. ``False`` when the target was deleted after the row was
      written; in that case ``digests`` is empty and ``ok`` is ``False``.
    - ``digests`` — mapping from each ``extra_payload`` key to a verdict:
      ``"ok"`` (recomputed value matches stored), ``"mismatch"`` (recomputed
      differs — tamper signal), or ``"no_digester"`` (no callable registered
      for this ``(target_model, key)`` pair; the row carries a digest the
      app code doesn't know how to recompute).
    """

    def __init__(
        self,
        change_id: int,
        target_loaded: bool,
        digests: dict[str, str],
    ):
        self.change_id = change_id
        self.target_loaded = target_loaded
        self.digests = digests

    @property
    def ok(self) -> bool:
        return self.target_loaded and all(verdict == "ok" for verdict in self.digests.values())

    def __repr__(self) -> str:
        return (
            f"DerivedStateVerificationResult(change_id={self.change_id}, "
            f"target_loaded={self.target_loaded}, digests={self.digests})"
        )


def verify_derived_state(change: ObjectChangeLog) -> DerivedStateVerificationResult:
    """Recompute each stored derived-state digest against current DB state.

    Returns a result with one verdict per key in ``change.extra_payload``.
    An empty ``extra_payload`` (the common case for signal-driven rows)
    yields an empty digest map and ``ok = target_loaded``.

    No side effects. Callers decide what a non-ok verdict means in their
    context: a periodic integrity scan would emit a security-log event;
    an ad-hoc operator query just reports the verdict.
    """
    target = change.content_object
    if target is None:
        return DerivedStateVerificationResult(
            change_id=change.pk,
            target_loaded=False,
            digests={},
        )

    target_model = type(target)
    digests: dict[str, str] = {}
    for key, stored in (change.extra_payload or {}).items():
        digester = _DIGESTERS.get((target_model, key))
        if digester is None:
            digests[key] = "no_digester"
            continue
        recomputed = digester(target)
        digests[key] = "ok" if recomputed == stored else "mismatch"

    return DerivedStateVerificationResult(
        change_id=change.pk,
        target_loaded=True,
        digests=digests,
    )
