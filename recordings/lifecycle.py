"""The post-ingest lifecycle: which status a recording holds once ingest has finished.

``recording-lifecycle-design.md`` draws the state boundary at file mutation. Ingest ends with a final stored file;
after it, reconstruction stages build the recording's default version and derive jobs read it. The status follows from
that work rather than being set by whoever finishes a step:

* reconstruction outstanding → ``PROCESSING`` (the version a rater should open is not built yet);
* reconstruction settled, derive jobs outstanding → ``AVAILABLE`` (openable, background work still running);
* everything settled → ``READY``.

"Settled" includes failed and skipped: a failed job is reported on the recording's jobs, never as ``FAILED``, which
stays reserved for an ingest that left no usable file.

``recordings`` owns the status and knows nothing about stages or jobs. The work is tracked by *providers*, registered
from an ``AppConfig.ready()`` — ``compute`` registers the reconstruction and derive ledgers — so this module never
imports the app that runs the work, which may depend on ``recordings`` but not the other way round. A deployment with
no provider, or whose providers find nothing to do, takes a recording straight to ``READY`` at the end of ingest.

A provider implements four calls:

``plan(recording)``
    Create the rows for the work this recording needs. Called inside the ingest's final transaction, so the rows and
    the status they imply commit together or not at all.
``outstanding(recording) -> set[Phase]``
    The phases in which it still has unsettled work.
``start(recording_pk)``
    Dispatch that work. Called once the ingest transaction has committed; it must not run work inline.
``derived_files(recording) -> Iterable[Path]``
    Files it keeps on the recording's behalf, which :func:`recordings.linked.unlink_recording_file` removes with the
    recording's own. A version built from a recording is the recording's data, so every deletion path — purge,
    account erasure, a dataset purge — has to reach it.

and may implement four more, which back the recording's ``/jobs`` and ``/versions`` endpoints. A provider without them
contributes nothing there:

``jobs(recording) -> list[dict]``
    Its stages and jobs for the recording, in run order.
``update_jobs(recording, *, enabled, order) -> bool``
    Apply per-recording overrides and replan; whether a rerun is needed. Raises ``LookupError`` for an unknown name,
    ``ValueError`` for an invalid order and ``RuntimeError`` while the work is running.
``versions(recording) -> dict | None``
    The version listing of ``version-fetch-contract.md`` §2.1, without the recording handle.
``version_file(recording, version_id) -> Path | None``
    The file holding a version, or ``None`` when it is not materialised.

The routes stay in ``recordings`` — and so behind its access rules, its FAILED-hidden gate and its sanitising serve
pipeline — while the data comes from whichever app runs the work.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from django.db import transaction

from recordings.pipeline.stages import Phase

logger = logging.getLogger(__name__)


@runtime_checkable
class LifecycleProvider(Protocol):
    """One source of post-ingest work. See the module docstring for each call's contract."""

    def plan(self, recording) -> None: ...

    def outstanding(self, recording) -> set[Phase]: ...

    def start(self, recording_pk: int) -> None: ...

    def derived_files(self, recording) -> Iterable[Path]: ...


_PROVIDERS: list[LifecycleProvider] = []


def register_lifecycle_provider(provider: LifecycleProvider) -> None:
    """Register *provider*, from an ``AppConfig.ready()``. Registering the same object again is a no-op."""
    if not isinstance(provider, LifecycleProvider):
        raise TypeError(f"{provider!r} does not implement plan, outstanding, start and derived_files")
    if any(existing is provider for existing in _PROVIDERS):
        return
    _PROVIDERS.append(provider)


def lifecycle_providers() -> tuple[LifecycleProvider, ...]:
    """The registered providers, in registration order."""
    return tuple(_PROVIDERS)


def status_for(recording):
    """The status *recording*'s outstanding post-ingest work implies."""
    from recordings.models import Recording

    outstanding: set[Phase] = set()
    for provider in _PROVIDERS:
        outstanding |= provider.outstanding(recording)
    if Phase.RECONSTRUCT in outstanding:
        return Recording.Status.PROCESSING
    if Phase.DERIVE in outstanding:
        return Recording.Status.AVAILABLE
    return Recording.Status.READY


def begin(recording):
    """Plan the post-ingest work for *recording* and return the status it starts in.

    Called by the ingest's final transition, inside its transaction, for a recording whose ingest succeeded. Dispatch
    is deferred to the commit, so a rolled-back ingest starts nothing.

    Nothing here may raise into the ingest: its failure handler unlinks the stored file and marks the recording
    ``FAILED``, which would discard a good upload over a fault in post-ingest work. A provider whose planning fails is
    logged and contributes no work, so the recording settles without it — visibly, since its jobs never appear — and
    dispatch runs in robust mode, where a failure is logged rather than raised to the committing caller.
    """
    for provider in _PROVIDERS:
        try:
            with transaction.atomic():
                provider.plan(recording)
        except Exception:
            logger.exception(
                "lifecycle: %r could not plan recording %d; it gets no post-ingest work.", provider, recording.pk
            )
    status = status_for(recording)
    if status != recording.Status.READY:
        pk = recording.pk
        transaction.on_commit(lambda: start(pk), robust=True)
    return status


def start(recording_pk: int) -> None:
    """Dispatch every provider's work for the recording, as after ingest or after its jobs were changed."""
    for provider in _PROVIDERS:
        provider.start(recording_pk)


def settle(recording_pk: int):
    """Re-derive the recording's status from its outstanding work and persist it if it moved. Returns the status.

    Called by a provider whenever a job settles or is requeued, so the status can move backwards too: rerunning a
    reconstruction stage takes a ``READY`` recording back to ``PROCESSING``. A recording that is gone, or whose
    ingest has not finished or failed, is left alone. The change is audited in the caller's scope, as the ingest's own
    transition is, and a move to ``READY`` tells the author their recording is done.
    """
    from activity.audit import record_modify_change, serialize_instance
    from recordings.models import Recording

    with transaction.atomic():
        recording = Recording.objects.select_for_update().filter(pk=recording_pk).first()
        if recording is None or recording.ingested_at is None or recording.status == Recording.Status.FAILED:
            return None
        status = status_for(recording)
        if status == recording.status:
            return status
        before_state = serialize_instance(recording)
        Recording.objects.filter(pk=recording_pk).update(status=status)
        recording.refresh_from_db()
        write_pending_scoring(recording, strict=False)
        record_modify_change(actor=None, obj=recording, before_state=before_state)
        if status == Recording.Status.READY:
            transaction.on_commit(lambda: notify_ready(recording), robust=True)
    return status


def write_pending_scoring(recording, *, strict: bool = True) -> None:
    """Write a study's waiting hypnogram stages as events once it can be opened, and clear them.

    The stages wait on ``Recording.pending_scoring`` (``recordings.hypnogram``) until the study is ``AVAILABLE`` or
    ``READY``, so a reader holding a grant finds no stage before the study itself can be read. Called inside the
    transaction that moves the status, with *recording* fresh from the database.

    At the end of ingest (*strict*) a failure raises, and the ingest fails with it rather than leave a study openable
    without the scoring it was given. Later, from :func:`settle`, the study's file is already final and a raise would
    hold it unopenable on every attempt, so the scoring is kept for repair and the status moves.
    """
    from recordings.hypnogram import write_hypnogram
    from recordings.models import Recording

    openable = (recording.Status.AVAILABLE, recording.Status.READY)
    if not recording.pending_scoring or recording.status not in openable:
        return
    try:
        with transaction.atomic():
            write_hypnogram(recording, recording.pending_scoring)
    except Exception:
        if strict:
            raise
        logger.exception("lifecycle: hypnogram stages of recording %d not written; kept for repair.", recording.pk)
        return
    Recording.objects.filter(pk=recording.pk).update(pending_scoring=None)
    recording.refresh_from_db()


def notify_ready(recording) -> None:
    """Tell the author *recording* has finished processing. A notification that cannot be queued is only logged."""
    from notifications.tasks import send_push_to_user
    from recordings.tasks import _push_display_name

    try:
        send_push_to_user.delay(
            user_id=recording.author_id,
            title="Recording ready",
            body=f'"{_push_display_name(recording)}" has been processed and is ready.',
            data={"type": "recording_ready", "recording_id": recording.pk},
        )
    except Exception:
        logger.warning("lifecycle: ready notification for recording %d not queued.", recording.pk, exc_info=True)


def derived_files(recording) -> list[Path]:
    """Every file a provider keeps on *recording*'s behalf."""
    paths: list[Path] = []
    for provider in _PROVIDERS:
        paths.extend(Path(path) for path in provider.derived_files(recording))
    return paths


def clear_lifecycle_providers() -> None:
    """Empty the registry. Test helper."""
    _PROVIDERS.clear()


# ---------------------------------------------------------------------------
# The optional read and control surface
# ---------------------------------------------------------------------------


def jobs(recording) -> list[dict]:
    """Every provider's stages and jobs for *recording*, in provider order."""
    listed: list[dict] = []
    for provider in _PROVIDERS:
        call = getattr(provider, "jobs", None)
        if call is not None:
            listed.extend(call(recording))
    return listed


def update_jobs(recording, *, enabled: dict[str, bool], order: list[str] | None) -> bool:
    """Apply job overrides through the provider that has jobs; whether a rerun is needed.

    Raises ``LookupError`` when no provider has jobs for the recording, and whatever the provider raises otherwise.
    """
    for provider in _PROVIDERS:
        call = getattr(provider, "update_jobs", None)
        if call is not None and getattr(provider, "jobs", None) is not None and provider.jobs(recording):
            return call(recording, enabled=enabled, order=order)
    raise LookupError("The recording has no jobs to change.")


def versions(recording) -> dict | None:
    """The version listing for *recording*, from the first provider that has one; ``None`` when none does."""
    for provider in _PROVIDERS:
        call = getattr(provider, "versions", None)
        listing = call(recording) if call is not None else None
        if listing is not None:
            return listing
    return None


def version_file(recording, version_id: str) -> Path | None:
    """The file holding *version_id* of *recording*, or ``None`` when no provider has it materialised."""
    for provider in _PROVIDERS:
        call = getattr(provider, "version_file", None)
        path = call(recording, version_id) if call is not None else None
        if path is not None:
            return Path(path)
    return None
