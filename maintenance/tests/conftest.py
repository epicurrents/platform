"""Fixtures shared by the maintenance tests: an isolated spool and the feature flags."""

import json

import pytest
from django.test import override_settings

from maintenance import lock, spool

PASSWORD = "testpass123"


@pytest.fixture
def spool_dir(tmp_path):
    """A temporary spool as ``MAINTENANCE_SPOOL_PATH``, with the lock cache cleared around it."""
    root = tmp_path / "update"
    root.mkdir()
    lock.invalidate_cache()
    with override_settings(MAINTENANCE_SPOOL_PATH=str(root)):
        yield root
    lock.invalidate_cache()


@pytest.fixture
def enabled(spool_dir):
    """The master flag on, the host tier off."""
    with override_settings(REMOTE_MAINTENANCE_ENABLED=True, REMOTE_UPDATE_ENABLED=False):
        yield spool_dir


@pytest.fixture
def host_enabled(spool_dir):
    """Both flags on."""
    with override_settings(REMOTE_MAINTENANCE_ENABLED=True, REMOTE_UPDATE_ENABLED=True):
        yield spool_dir


@pytest.fixture
def write_flag(spool_dir):
    """Write ``maintenance.json`` and drop the read cache; returns the writer."""

    def _write(phase="updating", **extra):
        data = {
            "protocol": 1,
            "phase": phase,
            "job_id": None,
            "since": "2026-09-20T10:00:00Z",
            "expected_until": None,
            "message": "The platform is being updated.",
        }
        data.update(extra)
        lock.lock_path().write_text(json.dumps(data))
        lock.invalidate_cache()
        return lock.lock_path()

    return _write


@pytest.fixture
def write_status(spool_dir):
    """Write a ``jobs/<id>.status.json`` as the agent would; returns the writer."""

    def _write(job_id, state, *, updated_at="2026-09-20T10:05:00Z", **extra):
        data = {"protocol": 1, "job_id": str(job_id), "state": state, "updated_at": updated_at}
        data.update(extra)
        spool.write_json_atomic(spool.status_path(job_id), data)
        return spool.status_path(job_id)

    return _write


@pytest.fixture
def staff(make_user):
    """A staff account that is not a superuser."""
    return make_user(is_staff=True, password=PASSWORD)


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client, staff


@pytest.fixture
def superuser_client(client, make_superuser):
    """A signed-in superuser with the known password."""
    user = make_superuser(password=PASSWORD)
    client.force_login(user)
    return client, user


@pytest.fixture
def no_push(monkeypatch):
    """Capture push dispatches instead of running the task."""
    sent = []

    def _delay(user_id, title, body, data=None):
        sent.append({"user_id": user_id, "title": title, "body": body, "data": data})

    from notifications.tasks import send_push_to_user

    monkeypatch.setattr(send_push_to_user, "delay", _delay)
    return sent
