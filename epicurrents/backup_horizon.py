"""The backup horizon: how long after a moment every copy of the database taken before it is gone.

A file handed over into a submission pool is only unjoinable from the copy it was made from once
every backup holding that copy has been destroyed. The hand-over therefore dates the file's
``not_before`` the horizon after the hand-over, and the horizon is derived from what the deployment
declares rather than set on its own, so changing the retention moves the delay with it.

Three kinds of copy are covered:

- **Borg archives.** The retention (``BACKUP_KEEP_DAILY``, ``_WEEKLY``, ``_MONTHLY``) and the gap
  between runs (``BACKUP_INTERVAL`` plus ``RUN_ALLOWANCE`` for the run itself) bound the age at
  which a prune removes an archive. The local repository compacts in the same run.
- **The append-only remote.** The host's prune only marks an archive as deleted there, and the data
  stays until the remote's administrator compacts the repository, so the interval the operator
  attests in ``BACKUP_REMOTE_COMPACTION_DAYS`` is added to the archive bound.
- **Snapshots under backups/.** update.sh and the host agent keep them by count, which is no age at
  all; ``BACKUP_SNAPSHOT_MAX_AGE_DAYS`` gives them one, applied by update.sh and daily by the agent.

The horizon is the longest of the three. When a term cannot be bounded — a remote with no attested
compaction, snapshots with no age limit, a value that does not parse — ``backup_horizon`` raises
``BackupHorizonUnknown`` with every reason, rather than returning a figure that is wrong in the
direction that matters. ``BACKUP_HORIZON_REQUIRED``, which a project feeding a pool from held
copies sets, turns that into a ``manage.py check`` error (``epicurrents/checks.py``).

What the bound cannot see: a backup container that has stopped, a remote that cannot be reached
and a run longer than ``RUN_ALLOWANCE`` all delay the prune past it, and copies outside the
platform are the operator's to attest. The monitor in docs/operations.md is what notices the first
two.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings

# The defaults the borg service applies when .env leaves a value empty. They are restated in
# borgmatic/emit_runtime_config.py, docker-compose.yml and borgmatic/entrypoint.sh, and
# epicurrents/tests/test_backup_horizon.py keeps the copies in step: a horizon computed from
# other numbers than the ones borg prunes by is wrong without anything failing.
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 3
DEFAULT_KEEP_MONTHLY = 5
DEFAULT_INTERVAL_SECONDS = 86400

# The entrypoint sleeps BACKUP_INTERVAL after each run ends, so the gap between two prunes is the
# interval plus however long a run takes. Six hours covers a slow remote; a run longer than that
# delays destruction past the bound.
RUN_ALLOWANCE = timedelta(hours=6)

# The host agent prunes snapshots by age once a day.
SNAPSHOT_PRUNE_CADENCE = timedelta(days=1)


class BackupHorizonUnknown(Exception):
    """The deployment's settings do not bound how long a backup can outlive a moment."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class BackupHorizon:
    """The horizon and the terms it was taken from, each as the delay it alone would impose."""

    total: timedelta
    archives: timedelta
    remote: timedelta | None
    snapshots: timedelta


def retention_bound(daily: int, weekly: int, monthly: int, gap: timedelta) -> timedelta:
    """Bound the age at which borg prune removes an archive, with prunes at most *gap* apart.

    Borg applies the rules newest first and in order, and an archive kept by an earlier rule does
    not count towards a later one, so the periods the rules keep follow each other rather than
    overlap. Each rule spans its count of periods, or of gaps when runs are further apart than the
    period; one more gap is the run that finally removes the archive, and a day covers calendar
    alignment. The test checks this against a port of borg's own prune over years of runs.
    """
    gap_days = gap / timedelta(days=1)
    days = daily * max(gap_days, 1) + weekly * max(gap_days, 7) + monthly * max(gap_days, 31) + gap_days + 1
    return timedelta(days=days)


def _whole_number(name: str, default: int | None, reasons: list[str], *, minimum: int = 0) -> int | None:
    """Read *name* from settings as a whole number, empty meaning *default*, recording why not."""
    raw = getattr(settings, name, "")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        reasons.append(f"{name}={raw!r} is not a whole number")
        return None
    if value < minimum:
        reasons.append(f"{name}={value} is below {minimum}")
        return None
    return value


def backup_horizon() -> BackupHorizon:
    """The deployment's backup horizon, or ``BackupHorizonUnknown`` naming every term it lacks."""
    reasons: list[str] = []
    daily = _whole_number("BACKUP_KEEP_DAILY", DEFAULT_KEEP_DAILY, reasons)
    weekly = _whole_number("BACKUP_KEEP_WEEKLY", DEFAULT_KEEP_WEEKLY, reasons)
    monthly = _whole_number("BACKUP_KEEP_MONTHLY", DEFAULT_KEEP_MONTHLY, reasons)
    interval = _whole_number("BACKUP_INTERVAL", DEFAULT_INTERVAL_SECONDS, reasons, minimum=1)

    compaction = None
    if settings.BACKUP_REMOTE_CONFIGURED:
        compaction = _whole_number("BACKUP_REMOTE_COMPACTION_DAYS", None, reasons, minimum=1)
        if compaction is None and not any(r.startswith("BACKUP_REMOTE_COMPACTION_DAYS") for r in reasons):
            reasons.append(
                "BORG_REMOTE_REPO is set and BACKUP_REMOTE_COMPACTION_DAYS is not, so nothing bounds how long "
                "the append-only remote keeps a pruned archive"
            )

    snapshot_days = _whole_number("BACKUP_SNAPSHOT_MAX_AGE_DAYS", None, reasons, minimum=1)
    if snapshot_days is None and not any(r.startswith("BACKUP_SNAPSHOT_MAX_AGE_DAYS") for r in reasons):
        reasons.append(
            "BACKUP_SNAPSHOT_MAX_AGE_DAYS is not set, so the snapshots under backups/ are kept by count with no age "
            "limit"
        )

    if reasons:
        raise BackupHorizonUnknown(reasons)

    archives = retention_bound(daily, weekly, monthly, timedelta(seconds=interval) + RUN_ALLOWANCE)
    remote = archives + timedelta(days=compaction) if compaction is not None else None
    snapshots = timedelta(days=snapshot_days) + SNAPSHOT_PRUNE_CADENCE
    total = max(archives, remote or archives, snapshots)
    return BackupHorizon(total=total, archives=archives, remote=remote, snapshots=snapshots)
