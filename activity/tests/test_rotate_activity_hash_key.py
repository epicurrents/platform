"""Tests for the ``rotate_activity_hash_key`` management command.

The command bumps ``ACTIVITY_HASH_KEY_CURRENT`` in ``.env`` to a key
version the operator has already staged in settings. It refuses to roll
forward to an unstaged version so the audit trail never writes under a
key the platform cannot load back.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "DJANGO_MODE=production\n"
        "ACTIVITY_HASH_KEY_V1=aGVsbG8tZHVtbXkta2V5LWZvci10ZXN0LXVzZS1vbmx5\n"
        "ACTIVITY_HASH_KEY_V2=YW5vdGhlci1kdW1teS1rZXktZm9yLXRlc3QtdXNlLW9ubHk=\n"
        "# ACTIVITY_HASH_KEY_CURRENT=1\n"
    )
    return path


class TestRotateActivityHashKey:
    def test_rolls_forward_to_max_staged_version(self, env_file, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32, 2: b"q" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        call_command("rotate_activity_hash_key", "--env-file", str(env_file))
        text = env_file.read_text()
        assert "ACTIVITY_HASH_KEY_CURRENT=2" in text
        # Previous key line still present — operator must keep it for
        # verification of rows already written under it.
        assert "ACTIVITY_HASH_KEY_V1=" in text

    def test_rolls_forward_to_explicit_target(self, env_file, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32, 2: b"q" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        call_command(
            "rotate_activity_hash_key",
            "--target-version",
            "2",
            "--env-file",
            str(env_file),
        )
        assert "ACTIVITY_HASH_KEY_CURRENT=2" in env_file.read_text()

    def test_refuses_unstaged_target(self, env_file, settings):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}  # only v1 staged
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        original = env_file.read_text()
        with pytest.raises(CommandError, match="ACTIVITY_HASH_KEY_V99"):
            call_command(
                "rotate_activity_hash_key",
                "--target-version",
                "99",
                "--env-file",
                str(env_file),
            )
        # .env unchanged after the refusal — the commented placeholder
        # line is still commented, no live ACTIVITY_HASH_KEY_CURRENT row
        # was written.
        assert env_file.read_text() == original

    def test_refuses_when_no_keys_configured(self, env_file, settings):
        settings.ACTIVITY_HASH_KEYS = {}
        with pytest.raises(CommandError, match="No ACTIVITY_HASH_KEY_V"):
            call_command("rotate_activity_hash_key", "--env-file", str(env_file))

    def test_noop_when_target_equals_current(self, env_file, settings, capsys):
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        call_command(
            "rotate_activity_hash_key",
            "--target-version",
            "1",
            "--env-file",
            str(env_file),
        )
        # Source file should not have grown a new ACTIVITY_HASH_KEY_CURRENT
        # line (the comment placeholder is left untouched).
        text = env_file.read_text()
        assert text.count("ACTIVITY_HASH_KEY_CURRENT") == 1

    def test_refuses_missing_env_file(self, tmp_path, settings):
        # Two staged keys + current=1 means the default target (max) is 2,
        # so the command reaches the env-file check rather than the
        # "already at target" short-circuit.
        settings.ACTIVITY_HASH_KEYS = {1: b"k" * 32, 2: b"q" * 32}
        settings.ACTIVITY_HASH_KEY_CURRENT = 1
        with pytest.raises(CommandError, match=".env file not found"):
            call_command(
                "rotate_activity_hash_key",
                "--env-file",
                str(tmp_path / "missing.env"),
            )
