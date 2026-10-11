"""The backup horizon: the archive bound against borg's own prune, the terms, the defaults and the check."""

import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.test import override_settings

from epicurrents.backup_horizon import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_KEEP_DAILY,
    DEFAULT_KEEP_MONTHLY,
    DEFAULT_KEEP_WEEKLY,
    RUN_ALLOWANCE,
    SNAPSHOT_PRUNE_CADENCE,
    BackupHorizonUnknown,
    backup_horizon,
    retention_bound,
)
from epicurrents.checks import check_backup_horizon

ROOT = Path(__file__).resolve().parents[2]

# The period of each rule, as borg's PRUNING_PATTERNS formats it.
_PERIODS = {"daily": "%Y-%m-%d", "weekly": "%G-%V", "monthly": "%Y-%m"}


def _borg_prune(archives, rules):
    """A port of borg's prune_split, applied rule after rule as borg prune does.

    Newest first; an archive opens a period when its period differs from the last one seen, and is
    kept unless an earlier rule already kept it, in which case it does not count towards this rule.
    """
    kept = set()
    newest_first = sorted(archives, reverse=True)
    for rule, count in rules:
        if count == 0:
            continue
        last = None
        taken = 0
        archive = None
        for archive in newest_first:
            period = archive.strftime(_PERIODS[rule])
            if period != last:
                last = period
                if archive not in kept:
                    kept.add(archive)
                    taken += 1
                    if taken == count:
                        break
        if archive is not None and taken < count and archive not in kept:
            kept.add(archive)
    return [archive for archive in archives if archive in kept]


def _worst_lifetime(daily, weekly, monthly, interval, run_time, *, seed=0):
    """The longest any archive lived past its creation before a prune removed it.

    Runs are *interval* plus up to *run_time* apart, over three years from several start dates;
    the first 500 days are left out, since a young repository keeps its oldest archive.
    """
    rng = random.Random(seed)
    rules = [("daily", daily), ("weekly", weekly), ("monthly", monthly)]
    worst = timedelta(0)
    for offset in range(0, 35, 4):
        now = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=offset, hours=rng.random() * 24)
        settled = now + timedelta(days=500)
        end = now + timedelta(days=3 * 365)
        archives = []
        while now < end:
            archives.append(now)
            kept = _borg_prune(archives, rules)
            for removed in set(archives) - set(kept):
                if removed > settled:
                    worst = max(worst, now - removed)
            archives = kept
            now += interval + rng.uniform(0, 1) * run_time
    return worst


class TestRetentionBound:
    @pytest.mark.parametrize(
        "retention",
        [(7, 4, 6), (7, 4, 3), (1, 1, 1), (3, 0, 2), (0, 4, 0), (2, 5, 1), (5, 0, 0), (0, 0, 2)],
    )
    @pytest.mark.parametrize(
        "interval,run_time",
        [
            (timedelta(days=1), timedelta(0)),
            (timedelta(days=1), timedelta(hours=6)),
            (timedelta(hours=6), timedelta(hours=30)),
            (timedelta(days=2), timedelta(days=1)),
            (timedelta(days=7), timedelta(days=1)),
        ],
    )
    def test_no_archive_outlives_the_bound(self, retention, interval, run_time):
        worst = _worst_lifetime(*retention, interval, run_time)
        assert worst <= retention_bound(*retention, interval + run_time)

    def test_the_bound_stays_close_to_borgs_worst_case_at_the_defaults(self):
        for retention in [(7, 3, 5), (7, 3, 2)]:
            worst = _worst_lifetime(*retention, timedelta(days=1), timedelta(0))
            assert retention_bound(*retention, timedelta(days=1)) - worst <= timedelta(days=5)


def _settings(**values):
    base = {
        "BACKUP_KEEP_DAILY": "",
        "BACKUP_KEEP_WEEKLY": "",
        "BACKUP_KEEP_MONTHLY": "",
        "BACKUP_INTERVAL": "",
        "BACKUP_REMOTE_CONFIGURED": False,
        "BACKUP_REMOTE_COMPACTION_DAYS": "",
        "BACKUP_SNAPSHOT_MAX_AGE_DAYS": "30",
        "BACKUP_HORIZON_REQUIRED": False,
    }
    base.update(values)
    return override_settings(**base)


class TestBackupHorizon:
    def test_empty_values_take_the_borg_defaults(self):
        gap = timedelta(seconds=DEFAULT_INTERVAL_SECONDS) + RUN_ALLOWANCE
        with _settings():
            horizon = backup_horizon()
        assert horizon.archives == retention_bound(DEFAULT_KEEP_DAILY, DEFAULT_KEEP_WEEKLY, DEFAULT_KEEP_MONTHLY, gap)
        assert horizon.remote is None
        assert horizon.total == horizon.archives

    def test_the_remote_adds_its_compaction_interval(self):
        with _settings(BACKUP_KEEP_MONTHLY="3", BACKUP_REMOTE_CONFIGURED=True, BACKUP_REMOTE_COMPACTION_DAYS="30"):
            horizon = backup_horizon()
        assert horizon.remote == horizon.archives + timedelta(days=30)
        assert horizon.total == horizon.remote

    def test_an_edu_configuration_comes_to_about_four_months(self):
        values = {"BACKUP_KEEP_WEEKLY": "3", "BACKUP_KEEP_MONTHLY": "2", "BACKUP_REMOTE_CONFIGURED": True}
        with _settings(**values, BACKUP_REMOTE_COMPACTION_DAYS="30"):
            horizon = backup_horizon()
        assert timedelta(days=90) <= horizon.archives <= timedelta(days=100)
        assert timedelta(days=120) <= horizon.total <= timedelta(days=130)

    def test_a_snapshot_limit_past_the_archives_becomes_the_horizon(self):
        with _settings(BACKUP_SNAPSHOT_MAX_AGE_DAYS="400"):
            horizon = backup_horizon()
        assert horizon.total == horizon.snapshots == timedelta(days=400) + SNAPSHOT_PRUNE_CADENCE

    def test_a_remote_without_an_attested_compaction_is_unknown(self):
        with _settings(BACKUP_REMOTE_CONFIGURED=True), pytest.raises(BackupHorizonUnknown) as unknown:
            backup_horizon()
        assert any("BACKUP_REMOTE_COMPACTION_DAYS" in reason for reason in unknown.value.reasons)

    def test_snapshots_without_an_age_limit_are_unknown(self):
        with _settings(BACKUP_SNAPSHOT_MAX_AGE_DAYS=""), pytest.raises(BackupHorizonUnknown) as unknown:
            backup_horizon()
        assert any("BACKUP_SNAPSHOT_MAX_AGE_DAYS" in reason for reason in unknown.value.reasons)

    @pytest.mark.parametrize(
        "name,value",
        [
            ("BACKUP_KEEP_DAILY", "seven"),
            ("BACKUP_KEEP_MONTHLY", "-1"),
            ("BACKUP_INTERVAL", "0"),
            ("BACKUP_SNAPSHOT_MAX_AGE_DAYS", "0"),
            ("BACKUP_REMOTE_COMPACTION_DAYS", "monthly"),
        ],
    )
    def test_a_value_that_does_not_parse_is_unknown_and_named_once(self, name, value):
        values = {"BACKUP_REMOTE_CONFIGURED": True, "BACKUP_REMOTE_COMPACTION_DAYS": "30", name: value}
        with _settings(**values), pytest.raises(BackupHorizonUnknown) as unknown:
            backup_horizon()
        assert len(unknown.value.reasons) == 1 and unknown.value.reasons[0].startswith(name)

    def test_every_missing_term_is_reported_together(self):
        with _settings(BACKUP_REMOTE_CONFIGURED=True, BACKUP_SNAPSHOT_MAX_AGE_DAYS=""):
            with pytest.raises(BackupHorizonUnknown) as unknown:
                backup_horizon()
        assert len(unknown.value.reasons) == 2


class TestDefaultsAgreeWithBorg:
    """The horizon is computed from the numbers borg prunes by; a default changed on one side only
    gives a horizon that is wrong with nothing failing."""

    @pytest.mark.parametrize(
        "name,default",
        [
            ("BACKUP_KEEP_DAILY", DEFAULT_KEEP_DAILY),
            ("BACKUP_KEEP_WEEKLY", DEFAULT_KEEP_WEEKLY),
            ("BACKUP_KEEP_MONTHLY", DEFAULT_KEEP_MONTHLY),
        ],
    )
    def test_retention_defaults(self, name, default):
        emitter = (ROOT / "borgmatic" / "emit_runtime_config.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        assert re.search(rf'_parse_int\("{name}", os\.environ\.get\("{name}", ""\), {default}\)', emitter)
        assert f"{name}=${{{name}:-{default}}}" in compose

    def test_interval_default(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        entrypoint = (ROOT / "borgmatic" / "entrypoint.sh").read_text()
        assert f"BACKUP_INTERVAL=${{BACKUP_INTERVAL:-{DEFAULT_INTERVAL_SECONDS}}}" in compose
        assert entrypoint.count(f"${{BACKUP_INTERVAL:-{DEFAULT_INTERVAL_SECONDS}}}") == 2


class TestCheck:
    def test_a_required_horizon_that_cannot_be_computed_is_an_error(self):
        with _settings(BACKUP_HORIZON_REQUIRED=True, BACKUP_SNAPSHOT_MAX_AGE_DAYS=""):
            issues = check_backup_horizon(None)
        assert [issue.id for issue in issues] == ["epicurrents.E030"]

    def test_a_required_horizon_that_can_be_computed_passes(self):
        with _settings(BACKUP_HORIZON_REQUIRED=True, BACKUP_REMOTE_CONFIGURED=True, BACKUP_REMOTE_COMPACTION_DAYS="30"):
            assert check_backup_horizon(None) == []

    def test_an_uncompacted_remote_warns_where_the_horizon_is_not_required(self):
        with _settings(BACKUP_REMOTE_CONFIGURED=True, BACKUP_SNAPSHOT_MAX_AGE_DAYS=""):
            issues = check_backup_horizon(None)
        assert [issue.id for issue in issues] == ["epicurrents.W030"]

    def test_unlimited_snapshots_alone_are_silent_where_the_horizon_is_not_required(self):
        with _settings(BACKUP_SNAPSHOT_MAX_AGE_DAYS=""):
            assert check_backup_horizon(None) == []
