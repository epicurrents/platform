"""The erasure record: written outside the database, read by rollbacks, re-applied after a restore."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from maintenance import erasures


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 20, hour, 0, tzinfo=UTC)


@pytest.mark.django_db
class TestRecord:
    def test_a_line_carries_identifiers_and_times_only(self, spool_dir):
        assert erasures.record_erasure(7, _at(9), at=_at(12))
        [line] = erasures.record_path().read_text().splitlines()
        assert json.loads(line) == {"at": "2026-09-20T12:00:00Z", "date_joined": "2026-09-20T09:00:00Z", "user_id": 7}

    def test_appends_and_does_not_record_the_same_account_twice(self, spool_dir):
        erasures.record_erasure(7, _at(9), at=_at(12))
        erasures.record_erasure(8, _at(9), at=_at(13))
        erasures.record_erasure(7, _at(9), at=_at(14))
        assert [r["user_id"] for r in erasures.read_records()] == [7, 8]

    def test_a_missing_spool_is_reported_not_raised(self, tmp_path, settings):
        settings.MAINTENANCE_SPOOL_PATH = str(tmp_path / "absent")
        assert erasures.record_erasure(7, _at(9)) is False

    def test_a_symlinked_record_is_not_followed(self, spool_dir, tmp_path):
        target = tmp_path / "elsewhere"
        target.write_text("")
        erasures.record_path().symlink_to(target)
        assert erasures.record_erasure(7, _at(9)) is False
        assert target.read_text() == ""

    def test_garbage_lines_are_skipped(self, spool_dir):
        erasures.record_path().write_text('not json\n{"user_id": "x"}\n\n')
        erasures.record_erasure(7, _at(9), at=_at(12))
        assert [r["user_id"] for r in erasures.read_records()] == [7]

    def test_counts_distinct_accounts_erased_after_a_moment(self, spool_dir):
        erasures.record_erasure(1, _at(1), at=_at(10))
        erasures.record_erasure(2, _at(1), at=_at(12))
        erasures.record_erasure(3, _at(2), at=_at(13))
        assert erasures.erasures_since(_at(11)) == 2
        assert erasures.erasures_since(None) == 3
        assert erasures.erasures_since(_at(14)) == 0


@pytest.mark.django_db
class TestReapply:
    def test_an_account_a_restore_brought_back_is_erased_again(self, spool_dir, make_user):
        from django.contrib.auth import get_user_model

        user = make_user()
        erasures.record_erasure(user.pk, user.date_joined)
        assert [u.pk for u in erasures.resurrected()] == [user.pk]
        assert erasures.reapply() == {"erased": 1, "failed": 0}
        assert not get_user_model().objects.filter(pk=user.pk).exists()

    def test_a_reused_primary_key_is_left_alone(self, spool_dir, make_user):
        from django.contrib.auth import get_user_model

        user = make_user()
        erasures.record_erasure(user.pk, user.date_joined - timedelta(days=3))
        assert erasures.resurrected() == []
        assert erasures.reapply() == {"erased": 0, "failed": 0}
        assert get_user_model().objects.filter(pk=user.pk).exists()

    def test_the_task_runs_it(self, spool_dir, make_user):
        from maintenance.tasks import reapply_erasures

        user = make_user()
        erasures.record_erasure(user.pk, user.date_joined)
        assert reapply_erasures() == {"erased": 1, "failed": 0}
