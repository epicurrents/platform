"""The post-ingest pipeline: reconstruction stages build a recording's default version, derive jobs read it.

This is ``compute``'s :class:`recordings.lifecycle.LifecycleProvider`, registered from ``ComputeConfig.ready``. It
implements the dispatch of ``signal-pipeline-plan.md`` §6 over the ledgers of ``pipeline-persistence-models.md``:

1. **Plan**, inside the ingest's final transaction: one :class:`~compute.models.RecordingJob` per registered
   reconstruction stage, in the registry's resolved order, ``QUEUED`` when enabled and ``SKIPPED`` when not; and one
   :class:`~compute.models.DeriveJob` per applicable derive job, against the version the enabled stages will produce.
   That version's id is known before anything runs — it is the hash of the manifest — so the number of jobs is too.
2. **Reconstruct**, in one Celery task: the enabled stages run in order over the source, each reading the previous
   one's output. One task rather than a chain of them, because the invariant that matters is that two stages never
   write concurrently, and a loop is the simplest thing that cannot break it. The source file is only read. The final
   bytes are the default version, materialised into the artifact cache and recorded once in the audit trail as the
   CREATE of an :class:`~compute.models.ArtifactCacheEntry` carrying its manifest — a new asset, never a MODIFY of the
   recording (``pipeline-persistence-models.md`` → "Recording a run into the audit chain is a CREATE").
3. **Derive**, one task per job, in parallel: each reads the default version and writes its own rows.

``recordings.lifecycle.settle`` re-derives the recording's status after every settled step: ``PROCESSING`` while a
stage is outstanding, ``AVAILABLE`` while a job is, ``READY`` after. A stage that fails ends the phase there — the
later stages and every derive job are skipped, since a number derived from a half-reconstructed signal would be
attributed to a version that was never built — and the recording still settles, with the failure on its jobs.

Ledger state changes are queryset updates, which emit no save signal, so the operational ledger stays out of the
permanent change log as the persistence note decides; what the jobs write is audited in the scope each task opens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from recordings.pipeline import SOURCE_VERSION_ID, Manifest, reconstruction_stages, stage_application
from recordings.pipeline.stages import Phase

logger = logging.getLogger(__name__)

#: File extensions the reconstruction stages operate on. A stage's contract is EDF bytes in, EDF bytes out.
EDF_EXTENSIONS = frozenset({".edf", ".bdf"})

#: Longest error text kept on a ledger row.
_ERROR_LIMIT = 1000


def artifact_cache_root() -> Path:
    """The artifact cache directory (``COMPUTE_ARTIFACT_CACHE_PATH``): rebuildable bytes, kept out of backups."""
    return Path(settings.COMPUTE_ARTIFACT_CACHE_PATH)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]


def _stages_by_name() -> dict:
    return {stage.name: stage for stage in reconstruction_stages()}


def default_manifest(recording, rows=None) -> Manifest:
    """The manifest the recording's enabled stages produce, in ledger order, from the registered stages.

    *rows* are the recording's ``RecordingJob`` rows; read from the database when not given. A row whose stage is no
    longer registered contributes nothing — :func:`plan` marks it skipped.
    """
    from compute.models import RecordingJob

    if rows is None:
        rows = RecordingJob.objects.filter(recording=recording).order_by("order")
    stages = _stages_by_name()
    manifest = Manifest(recording.stored_hash)
    for row in rows:
        stage = stages.get(row.stage_name)
        if row.enabled and stage is not None:
            manifest = manifest.with_stage(stage_application(stage))
    return manifest


class ComputeLifecycle:
    """The reconstruction and derive ledgers as a :class:`recordings.lifecycle.LifecycleProvider`."""

    def __repr__(self) -> str:
        return "ComputeLifecycle()"

    def plan(self, recording) -> None:
        """Create or requeue the recording's stage and job rows. Safe to call again: overrides are kept.

        A recording that is not EDF/BDF, or has no stored hash to name its versions by, gets no rows. A linked
        recording gets derive jobs against its source but no reconstruction stages: its file is the operator's, never
        de-identified by the platform, and a version built from it would be a new platform-held copy of that file.
        """
        from compute.derive import derive_jobs
        from compute.models import DeriveJob, RecordingJob
        from recordings.models import Recording

        if (recording.file_extension or "").lower() not in EDF_EXTENSIONS or not recording.stored_hash:
            return
        existing = {row.stage_name: row for row in RecordingJob.objects.filter(recording=recording)}
        registered_stages = [] if recording.storage == Recording.Storage.LINKED else reconstruction_stages()
        stages = _resolved_order(registered_stages, existing)
        registered = {stage.name for stage in stages}
        for order, stage in enumerate(stages):
            row = existing.get(stage.name)
            enabled = row.enabled if row is not None else stage.enabled_by_default
            values = {
                "order": order,
                "enabled": enabled,
                "state": RecordingJob.State.QUEUED if enabled else RecordingJob.State.SKIPPED,
                "input_digest": "",
                "output_version_id": "",
                "started_at": None,
                "finished_at": None,
                "error": "",
            }
            if row is None:
                RecordingJob.objects.bulk_create([RecordingJob(recording=recording, stage_name=stage.name, **values)])
            else:
                RecordingJob.objects.filter(pk=row.pk).update(**values)
        stale = [name for name in existing if name not in registered]
        if stale:
            RecordingJob.objects.filter(recording=recording, stage_name__in=stale).update(
                state=RecordingJob.State.SKIPPED, error="The stage is no longer registered."
            )

        version_id = default_manifest(recording).version_id
        # A job planned against another version was superseded by this plan before it ran.
        DeriveJob.objects.filter(
            recording=recording, state__in=[DeriveJob.State.QUEUED, DeriveJob.State.RUNNING]
        ).exclude(input_version_id=version_id).update(state=DeriveJob.State.SKIPPED, error="Superseded by a new plan.")
        for job in derive_jobs():
            if not job.applies_to(recording):
                continue
            row = DeriveJob.objects.filter(recording=recording, job_name=job.name, input_version_id=version_id).first()
            if row is None:
                DeriveJob.objects.bulk_create(
                    [
                        DeriveJob(
                            recording=recording,
                            job_name=job.name,
                            code_version=job.code_version,
                            input_version_id=version_id,
                        )
                    ]
                )
            elif row.state != DeriveJob.State.DONE or row.code_version != job.code_version:
                # Done against this exact version by this exact code is done: versions are content-addressed.
                DeriveJob.objects.filter(pk=row.pk).update(
                    state=DeriveJob.State.QUEUED,
                    code_version=job.code_version,
                    started_at=None,
                    finished_at=None,
                    error="",
                )

    def outstanding(self, recording) -> set[Phase]:
        from compute.models import DeriveJob, RecordingJob

        unsettled = [RecordingJob.State.QUEUED, RecordingJob.State.RUNNING]
        phases: set[Phase] = set()
        if RecordingJob.objects.filter(recording=recording, state__in=unsettled).exists():
            phases.add(Phase.RECONSTRUCT)
        if DeriveJob.objects.filter(recording=recording, state__in=unsettled).exists():
            phases.add(Phase.DERIVE)
        return phases

    def start(self, recording_pk: int) -> None:
        run_reconstruction.delay(recording_pk)

    def derived_files(self, recording) -> Iterable[Path]:
        from compute.models import ArtifactCacheEntry

        if recording.pk is None:
            return []

        paths = ArtifactCacheEntry.objects.filter(recording=recording).exclude(storage_path="")
        return [Path(path) for path in paths.values_list("storage_path", flat=True)]

    # ── Read and control surface (``recordings.lifecycle``'s optional calls) ──

    def jobs(self, recording) -> list[dict]:
        """The recording's stages in run order, then the derive jobs reading its current default version."""
        from compute.models import DeriveJob, RecordingJob

        stages = _stages_by_name()
        rows = list(RecordingJob.objects.filter(recording=recording).order_by("order"))
        listed = [
            {
                "name": row.stage_name,
                "phase": "reconstruct",
                "order": row.order,
                "enabled": row.enabled,
                "requires": list(getattr(stages.get(row.stage_name), "requires", ())),
                "state": row.state,
                "version_id": row.output_version_id,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "error": row.error,
            }
            for row in rows
        ]
        version_id = default_manifest(recording, rows).version_id
        for row in DeriveJob.objects.filter(recording=recording, input_version_id=version_id).order_by("job_name"):
            listed.append(
                {
                    "name": row.job_name,
                    "phase": "derive",
                    "order": None,
                    "enabled": True,
                    "requires": [],
                    "state": row.state,
                    "version_id": row.input_version_id,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "error": row.error,
                }
            )
        return listed

    def update_jobs(self, recording, *, enabled: dict[str, bool], order: list[str] | None) -> bool:
        """Apply per-recording stage overrides and replan. Returns whether a rerun is needed.

        Raises ``LookupError`` for a stage the recording does not have, ``ValueError`` for an order that is not a
        permutation of its stages or puts a stage before one it requires (``signal-pipeline-plan.md`` §4: the order
        is partial, not free), and ``RuntimeError`` while a stage is running, since changing the plan under a
        running phase would leave its ledger describing a run that never happened. An unchanged request reruns a
        recording with a failed stage, so it doubles as a retry.
        """
        from compute.models import RecordingJob

        release_stale_stages(recording)
        rows = {row.stage_name: row for row in RecordingJob.objects.filter(recording=recording)}
        unknown = sorted((set(enabled) | set(order or [])) - set(rows))
        if unknown:
            raise LookupError(f"Unknown stages: {', '.join(unknown)}.")
        if any(row.state == RecordingJob.State.RUNNING for row in rows.values()):
            raise RuntimeError("A reconstruction stage is running; change the plan once it has settled.")
        if order is not None:
            if sorted(order) != sorted(rows):
                raise ValueError("The order must list every stage of the recording exactly once.")
            stages = _stages_by_name()
            seen: set[str] = set()
            for name in order:
                missing = [dep for dep in getattr(stages.get(name), "requires", ()) if dep in rows and dep not in seen]
                if missing:
                    raise ValueError(f"{name} must run after {', '.join(missing)}.")
                seen.add(name)
        changed = False
        for name, value in enabled.items():
            if rows[name].enabled != value:
                RecordingJob.objects.filter(pk=rows[name].pk).update(enabled=value)
                changed = True
        if order is not None:
            for position, name in enumerate(order):
                if rows[name].order != position:
                    RecordingJob.objects.filter(pk=rows[name].pk).update(order=position)
                    changed = True
        # A request against a recording whose stage failed reruns it even unchanged: that is how an author retries a
        # failure, including one a stopped worker left (``release_stale_stages``).
        if not changed:
            changed = RecordingJob.objects.filter(recording=recording, state=RecordingJob.State.FAILED).exists()
        if changed:
            recording.refresh_from_db()
            self.plan(recording)
        return changed

    def versions(self, recording) -> dict | None:
        """The version listing of ``version-fetch-contract.md`` §2.1, without the recording handle.

        The source, every materialised version, and the planned default while it is being built or after its build
        failed. ``None`` for a recording this provider plans nothing for.
        """
        from compute.models import ArtifactCacheEntry, RecordingJob

        if (recording.file_extension or "").lower() not in EDF_EXTENSIONS or not recording.stored_hash:
            return None
        source_path = Path(recording.file_path)
        if not source_path.is_file():
            return None
        geometry, source_channels = _file_channels(source_path)
        listed = [
            {
                "version_id": SOURCE_VERSION_ID,
                "label": "Original",
                "state": "ready",
                "manifest": [],
                "channels": [dict(channel, modified_by=[]) for channel in source_channels],
            }
        ]
        materialised = ArtifactCacheEntry.objects.filter(
            recording=recording, disposition=ArtifactCacheEntry.Disposition.MATERIALISED
        ).exclude(storage_path="")
        for entry in materialised.order_by("created_at", "pk"):
            path = Path(entry.storage_path)
            record = _version_record(entry)
            if not path.is_file() or record is None:
                continue
            provenance = {channel["name"]: channel["modified_by"] for channel in record.get("channels", [])}
            _geometry, channels = _file_channels(path)
            listed.append(
                {
                    "version_id": entry.version_id,
                    "label": _version_label(record["version_manifest"]),
                    "state": "ready",
                    "manifest": _wire_manifest(record["version_manifest"]),
                    "channels": [dict(c, modified_by=provenance.get(c["name"], [])) for c in channels],
                }
            )

        rows = list(RecordingJob.objects.filter(recording=recording).order_by("order"))
        planned = default_manifest(recording, rows)
        known = {version["version_id"] for version in listed}
        default = planned.version_id if planned.version_id in known else SOURCE_VERSION_ID
        if planned.version_id not in known:
            enabled = [row for row in rows if row.enabled]
            if any(row.state in (RecordingJob.State.QUEUED, RecordingJob.State.RUNNING) for row in enabled):
                state = "building"
                default = planned.version_id
            elif any(row.state == RecordingJob.State.FAILED for row in enabled):
                state = "failed"
            else:
                state = "unbuilt"
            payload = json.loads(planned.canonical())
            listed.append(
                {
                    "version_id": planned.version_id,
                    "label": _version_label(payload),
                    "state": state,
                    "manifest": _wire_manifest(payload),
                    "channels": [],
                }
            )
        return {
            "source_hash": f"sha256:{recording.stored_hash}",
            "duration_seconds": geometry["duration_seconds"],
            "record_duration_sec": geometry["record_duration_sec"],
            "default_version": default,
            "versions": listed,
        }

    def version_file(self, recording, version_id: str) -> Path | None:
        return version_file(recording, version_id)


def _resolved_order(stages: list, existing: dict) -> list:
    """*stages* in the recording's own order where it has one, the registry's otherwise.

    A recording keeps the order a ``PATCH /jobs`` gave it; a stage registered since goes after the ones it already
    has, in registry order. An order that a newly registered dependency makes invalid falls back to the registry's,
    which is valid by construction, rather than run a stage before one it requires.
    """
    position = {stage.name: index for index, stage in enumerate(stages)}
    ordered = sorted(
        stages,
        key=lambda s: (0, existing[s.name].order) if s.name in existing else (1, position[s.name]),
    )
    seen: set[str] = set()
    for stage in ordered:
        if any(dep not in seen for dep in stage.requires):
            return stages
        seen.add(stage.name)
    return ordered


def _file_channels(path: Path) -> tuple[dict, list[dict]]:
    """The record geometry and the signal channels of the EDF/BDF at *path*, read from its header alone."""
    from recordings.processors.edf_window import read_header

    header, infos = read_header(path)
    duration = header.data_record_duration
    channels = [
        {
            "name": info.label.strip(),
            "fs": info.sample_count / duration if duration > 0 else 0.0,
            "sample_count": info.sample_count * header.data_record_count,
        }
        for info in infos
        if not info.is_annotation_channel
    ]
    geometry = {
        "duration_seconds": header.data_record_count * duration,
        "record_duration_sec": duration,
    }
    return geometry, channels


def _version_record(entry) -> dict | None:
    """The payload the version's CREATE row carries: its manifest and channel provenance."""
    from django.contrib.contenttypes.models import ContentType

    from activity.models import ObjectChangeLog

    row = (
        ObjectChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(entry),
            object_id=str(entry.pk),
            action=ObjectChangeLog.ACTION_CREATE,
            extra_payload__has_key="version_manifest",
        )
        .order_by("-pk")
        .first()
    )
    return row.extra_payload if row is not None else None


def _wire_manifest(payload: dict) -> list[dict]:
    return [
        {"stage": s["stage"], "code_version": s["code_version"], "params_hash": s["params_hash"]}
        for s in payload.get("stages", [])
    ]


def _version_label(payload: dict) -> str:
    """The label a stage registers, joined in manifest order; its name where it has none. Opaque to the viewer."""
    stages = _stages_by_name()
    names = [s["stage"] for s in payload.get("stages", [])]
    return " + ".join(getattr(stages.get(name), "label", name) for name in names) or "Original"


PROVIDER = ComputeLifecycle()


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------


@shared_task(name="compute.run_reconstruction")
def run_reconstruction(recording_pk: int) -> dict:
    """Run the recording's queued reconstruction stages, materialise the default version, then dispatch derive jobs."""
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from recordings.models import Recording

    recording = Recording.objects.filter(pk=recording_pk).first()
    if recording is None:
        return {"status": "gone"}
    with with_system_activity(
        "compute.reconstruction.run",
        interface=Activity.Interface.CELERY,
        target=recording,
        metadata={"recording_id": recording_pk},
    ):
        from recordings.lifecycle import settle

        result = _reconstruct(recording)
        if result["status"] == "busy":
            # The run holding the phase is still building the version the jobs read; it dispatches them itself.
            return result
        _dispatch_derive(recording)
        settle(recording_pk)
    return result


def _reconstruct(recording) -> dict:
    from compute.models import DeriveJob, RecordingJob

    release_stale_stages(recording)
    rows = list(RecordingJob.objects.filter(recording=recording).order_by("order"))
    if any(row.state == RecordingJob.State.RUNNING for row in rows):
        # Another run holds the phase. Two runs writing one recording's versions concurrently is the race the plan's
        # serialisation rule exists for, so this one yields.
        return {"status": "busy"}
    queued = [row for row in rows if row.state == RecordingJob.State.QUEUED]
    if not queued:
        return {"status": "nothing-queued"}

    stages = _stages_by_name()
    manifest = Manifest(recording.stored_hash)
    try:
        data = _read_source(recording)
        digests = channel_digests(data)
    except Exception as exc:
        _fail_phase(recording, queued, _error_text(exc))
        return {"status": "failed"}
    modified_by: dict[str, list[str]] = {label: [] for label in digests}

    for row in rows:
        if not row.enabled or row.state == RecordingJob.State.SKIPPED:
            continue
        stage = stages.get(row.stage_name)
        later = [r for r in rows if r.order > row.order and r.enabled]
        if stage is None:
            RecordingJob.objects.filter(pk=row.pk).update(
                state=RecordingJob.State.FAILED, finished_at=timezone.now(), error="The stage is no longer registered."
            )
            _fail_phase(recording, later, "An earlier stage failed.")
            return {"status": "failed", "stage": row.stage_name}
        claimed = RecordingJob.objects.filter(pk=row.pk, state=RecordingJob.State.QUEUED).update(
            state=RecordingJob.State.RUNNING, started_at=timezone.now(), finished_at=None, error=""
        )
        if not claimed:
            # A concurrent run claimed the stage between this run's read and now; it holds the phase.
            return {"status": "busy"}
        input_digest = hashlib.sha256(data).hexdigest()
        try:
            data = _apply_stage(stage, data)
            after = channel_digests(data)
        except Exception as exc:
            logger.exception("compute: stage %s failed on recording %d", row.stage_name, recording.pk)
            RecordingJob.objects.filter(pk=row.pk).update(
                state=RecordingJob.State.FAILED,
                input_digest=input_digest,
                finished_at=timezone.now(),
                error=_error_text(exc),
            )
            _fail_phase(recording, later, "An earlier stage failed.")
            return {"status": "failed", "stage": row.stage_name}
        # Per-channel provenance (``version-fetch-contract.md`` §2.1): a channel this stage added or whose samples it
        # changed is modified by it. Taken here, where both sides are in memory, so no reader ever diffs two files.
        modified_by = {
            label: modified_by.get(label, []) + ([stage.name] if digests.get(label) != digest else [])
            for label, digest in after.items()
        }
        digests = after
        manifest = manifest.with_stage(stage_application(stage))
        RecordingJob.objects.filter(pk=row.pk).update(
            state=RecordingJob.State.DONE,
            input_digest=input_digest,
            output_version_id=manifest.version_id,
            finished_at=timezone.now(),
        )

    if not manifest.is_source():
        reproducible = all(getattr(stages[name], "reproducible", True) for name in manifest.stage_names())
        channels = [{"name": label, "modified_by": stages_} for label, stages_ in modified_by.items()]
        materialise_version(recording, manifest, data, reproducible=reproducible, channels=channels)
    # A plan made against other stage settings than the ones that ran points its jobs at a version nobody built.
    DeriveJob.objects.filter(recording=recording, state=DeriveJob.State.QUEUED).exclude(
        input_version_id=manifest.version_id
    ).update(state=DeriveJob.State.SKIPPED, error="Planned against another version than the one built.")
    return {"status": "ok", "version_id": manifest.version_id}


def release_stale_stages(recording) -> int:
    """Fail stages left ``running`` longer than ``COMPUTE_STAGE_STALE_HOURS``; returns how many.

    A worker killed mid-stage — a whole recording is held in memory, so an out-of-memory kill is plausible — leaves
    its row ``running`` with nothing to settle it. The recording would stay ``processing`` for good, and a rerun is
    refused while a stage runs. Past the threshold the row is taken as abandoned: failed, which lets the recording
    settle and the author rerun it.
    """
    from datetime import timedelta

    from compute.models import RecordingJob

    hours = getattr(settings, "COMPUTE_STAGE_STALE_HOURS", 6)
    return RecordingJob.objects.filter(
        recording=recording,
        state=RecordingJob.State.RUNNING,
        started_at__lt=timezone.now() - timedelta(hours=hours),
    ).update(state=RecordingJob.State.FAILED, finished_at=timezone.now(), error="The run that held this stage stopped.")


def _read_source(recording) -> bytes:
    from recordings.linked import ensure_linked_file_unchanged
    from recordings.models import Recording

    if recording.storage == Recording.Storage.LINKED:
        ensure_linked_file_unchanged(recording)
    return Path(recording.file_path).read_bytes()


def channel_digests(data: bytes) -> dict[str, str]:
    """SHA-256 of each signal channel's samples across all data records, by label. Annotation channels are left out.

    A label that appears twice keeps its first occurrence, as the stored file's naming guarantees it does not.
    """
    from recordings.processors.edf import parse_edf_header, parse_signal_infos

    header = parse_edf_header(data)
    infos = parse_signal_infos(data, header)
    width = 3 if header.data_format.rstrip("+").lower() == "bdf" else 2
    sizes = [info.sample_count * width for info in infos]
    record_size = sum(sizes)
    hashers = [hashlib.sha256() for _ in infos]
    base = header.header_record_bytes
    for record in range(header.data_record_count):
        offset = base + record * record_size
        for index, size in enumerate(sizes):
            hashers[index].update(data[offset : offset + size])
            offset += size
    digests: dict[str, str] = {}
    for info, hasher in zip(infos, hashers, strict=True):
        if not info.is_annotation_channel:
            digests.setdefault(info.label.strip(), hasher.hexdigest())
    return digests


def _apply_stage(stage, data: bytes) -> bytes:
    from recordings.processors.edf import parse_edf_header

    header_size = parse_edf_header(data).header_record_bytes
    header, signals = stage.transform(data[:header_size], data[header_size:])
    return header + signals


def _fail_phase(recording, rows, reason: str) -> None:
    """Mark *rows* skipped with *reason*, and every unsettled derive job too: nothing downstream can run."""
    from compute.models import DeriveJob, RecordingJob

    now = timezone.now()
    RecordingJob.objects.filter(pk__in=[row.pk for row in rows]).exclude(state=RecordingJob.State.FAILED).update(
        state=RecordingJob.State.SKIPPED, finished_at=now, error=reason
    )
    DeriveJob.objects.filter(recording=recording, state__in=[DeriveJob.State.QUEUED, DeriveJob.State.RUNNING]).update(
        state=DeriveJob.State.SKIPPED, finished_at=now, error="Reconstruction did not complete."
    )


def version_path(recording, version_id: str) -> Path:
    """Where the artifact cache keeps *version_id* of *recording*: one directory per recording, by its hash prefix."""
    handle = recording.stored_name.split(".", 1)[0]
    extension = (recording.file_extension or ".edf").lower()
    return artifact_cache_root() / handle / f"{version_id}{extension}"


def materialise_version(recording, manifest: Manifest, data: bytes, *, reproducible: bool = True, channels=None):
    """Write *data* as *manifest*'s version of *recording* into the artifact cache and return its cache entry.

    The file is written beside its final name and moved into place, so a reader sees a whole version or none. The
    first materialisation of a version is its CREATE in the audit trail, carrying the manifest — the durable recipe
    the bytes can be rebuilt from once evicted. A rematerialisation of a known version writes no new record. Returns
    ``None``, having removed the file, when the recording was deleted while its stages ran.
    """
    from activity.audit import record_create_change
    from compute.models import ArtifactCacheEntry

    path = version_path(recording, manifest.version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    values = {
        "storage_path": str(path),
        "size_bytes": len(data),
        "reproducible": reproducible,
        "disposition": ArtifactCacheEntry.Disposition.MATERIALISED,
    }
    from recordings.models import Recording

    with transaction.atomic():
        # The recording may have been purged or erased while the stages ran. Every deletion path finds a version
        # through its cache row, so a file written for a recording that is gone would have no row and outlive the
        # erasure. Held under the row lock deletion takes, the check and the entry commit together; the file is
        # removed here when the recording is gone, and ``sweep_artifact_cache`` reaps anything a crash leaves.
        if not Recording.objects.select_for_update().filter(pk=recording.pk).exists():
            path.unlink(missing_ok=True)
            return None
        entry = ArtifactCacheEntry.objects.filter(recording=recording, version_id=manifest.version_id).first()
        if entry is not None:
            ArtifactCacheEntry.objects.filter(pk=entry.pk).update(**values)
            entry.refresh_from_db()
            return entry
        (entry,) = ArtifactCacheEntry.objects.bulk_create(
            [ArtifactCacheEntry(recording=recording, version_id=manifest.version_id, **values)]
        )
        if entry.pk is None:
            entry = ArtifactCacheEntry.objects.get(recording=recording, version_id=manifest.version_id)
        record_create_change(
            actor=None,
            obj=entry,
            extra_payload={
                "version_manifest": json.loads(manifest.canonical()),
                "version_id": manifest.version_id,
                "channels": channels or [],
            },
        )
    return entry


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def _dispatch_derive(recording) -> None:
    from compute.models import DeriveJob

    for pk in DeriveJob.objects.filter(recording=recording, state=DeriveJob.State.QUEUED).values_list("pk", flat=True):
        transaction.on_commit(lambda pk=pk: run_derive_job.delay(pk), robust=True)


def version_file(recording, version_id: str) -> Path | None:
    """The file holding *version_id* of *recording*: the stored file for the source, the cache entry otherwise.

    ``None`` when the version is not materialised — evicted, purged or never built.
    """
    from compute.models import ArtifactCacheEntry

    if version_id == SOURCE_VERSION_ID:
        return Path(recording.file_path)
    entry = ArtifactCacheEntry.objects.filter(
        recording=recording,
        version_id=version_id,
        disposition=ArtifactCacheEntry.Disposition.MATERIALISED,
    ).first()
    if entry is None or not entry.storage_path:
        return None
    path = Path(entry.storage_path)
    return path if path.exists() else None


@shared_task(name="compute.run_derive_job")
def run_derive_job(job_pk: int) -> dict:
    """Run one queued derive job against the version it was planned on, then settle the recording."""
    from activity.models import Activity
    from activity.system_activity import with_system_activity
    from compute.derive import get_derive_job
    from compute.models import DeriveJob
    from recordings.lifecycle import settle

    claimed = DeriveJob.objects.filter(pk=job_pk, state=DeriveJob.State.QUEUED).update(
        state=DeriveJob.State.RUNNING, started_at=timezone.now(), finished_at=None, error=""
    )
    if not claimed:
        return {"status": "not-queued"}
    row = DeriveJob.objects.select_related("recording").get(pk=job_pk)
    recording = row.recording
    with with_system_activity(
        "compute.derive.run",
        interface=Activity.Interface.CELERY,
        target=recording,
        metadata={"recording_id": recording.pk, "job": row.job_name},
    ):
        state, error = DeriveJob.State.DONE, ""
        spec = get_derive_job(row.job_name)
        path = version_file(recording, row.input_version_id)
        if spec is None:
            state, error = DeriveJob.State.FAILED, "The job is no longer registered."
        elif path is None:
            state, error = DeriveJob.State.FAILED, "The version the job reads is not materialised."
        else:
            try:
                with transaction.atomic():
                    spec.run(recording, path, row.input_version_id)
            except Exception as exc:
                logger.exception("compute: derive job %s failed on recording %d", row.job_name, recording.pk)
                state, error = DeriveJob.State.FAILED, _error_text(exc)
        DeriveJob.objects.filter(pk=job_pk).update(state=state, finished_at=timezone.now(), error=error)
        settle(recording.pk)
    return {"status": state}


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


@shared_task(name="compute.sweep_artifact_cache")
def sweep_artifact_cache() -> int:
    """Unlink artifact-cache files no ``ArtifactCacheEntry`` names; returns how many.

    Every deletion path finds a recording's versions through their cache rows, so a file without a row is beyond its
    reach: a ``.tmp`` a killed worker left mid-write, or a version whose row was created after a deletion listed the
    recording's files and then went with it in the cascade. Such a file is still a copy of a recording's signal. A
    regular file under the cache root older than ``COMPUTE_ARTIFACT_CACHE_SWEEP_GRACE_HOURS`` (default 24, which no
    write in progress outlasts) is unlinked when no row names it, and an emptied recording directory goes with it.
    Filesystem housekeeping only, so no audited scope; the count is logged, never a name.
    """
    from datetime import timedelta

    from compute.models import ArtifactCacheEntry

    root = artifact_cache_root()
    if not root.is_dir():
        return 0
    grace_hours = getattr(settings, "COMPUTE_ARTIFACT_CACHE_SWEEP_GRACE_HOURS", 24)
    threshold = (timezone.now() - timedelta(hours=grace_hours)).timestamp()
    candidates = []
    for entry in root.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime < threshold:
                candidates.append(entry)
        except OSError:
            continue
    if not candidates:
        return 0
    named = set(
        ArtifactCacheEntry.objects.filter(storage_path__in=[str(path) for path in candidates]).values_list(
            "storage_path", flat=True
        )
    )
    removed = 0
    for path in candidates:
        if str(path) in named:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
        try:
            path.parent.rmdir()
        except OSError:
            pass
    if removed:
        logger.info("sweep_artifact_cache: removed %d files no cache entry names.", removed)
    return removed
