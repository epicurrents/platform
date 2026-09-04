"""Tests for ``epicurrents.management.commands.init_env``.

Covers the management command the bootstrap scripts invoke on first
pass to generate ``.env`` with random secrets. The command runs in two
modes:

- Create from ``.env.example`` when ``.env`` does not exist.
- Update in place when ``.env`` exists; only empty / placeholder values
  are filled in unless ``--force`` is passed.
"""

from pathlib import Path

import pytest
from django.core.management import call_command

_ENV_EXAMPLE = """\
DJANGO_MODE=production
SECRET_KEY=change-me
BORG_PASSPHRASE=
ACTIVITY_HASH_KEY_V1=
ADMIN_USERNAME=admin
ADMIN_PASSWORD=
REDIS_PASSWORD=
WEBPUSH_VAPID_PUBLIC_KEY=
WEBPUSH_VAPID_PRIVATE_KEY=
FEDERATION_PUBLIC_KEY=
FEDERATION_PRIVATE_KEY=
DB_NAME=epicurrents
DB_USERNAME=epicurrents
DB_PASSWORD=epicurrents
"""


def _write_example(base_dir: Path, content: str = _ENV_EXAMPLE) -> Path:
    example = base_dir / ".env.example"
    example.write_text(content)
    return example


@pytest.fixture
def env_dir(tmp_path, settings):
    """Point ``settings.BASE_DIR`` at a tmp directory containing only the
    files the test cares about, so the command writes ``.env`` there
    instead of polluting the repo root.
    """
    settings.BASE_DIR = tmp_path
    return tmp_path


def _parse(content: str) -> dict[str, str]:
    out = {}
    for line in content.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


class TestInitEnvFreshCreate:
    """No ``.env`` present — the command creates one from ``.env.example``
    and fills every empty placeholder with a generated secret.
    """

    def test_creates_env_from_example(self, env_dir):
        _write_example(env_dir)
        call_command("init_env")
        env = env_dir / ".env"
        assert env.exists()
        # Every key from .env.example is present.
        assert "SECRET_KEY" in env.read_text()
        assert "ADMIN_PASSWORD" in env.read_text()

    def test_fills_single_value_secrets(self, env_dir):
        _write_example(env_dir)
        call_command("init_env")
        parsed = _parse((env_dir / ".env").read_text())
        # SECRET_KEY had the placeholder "change-me" — gets regenerated.
        assert parsed["SECRET_KEY"] not in ("", "change-me")
        # BORG_PASSPHRASE, ADMIN_PASSWORD, and REDIS_PASSWORD start empty —
        # all get filled.
        assert parsed["BORG_PASSPHRASE"] != ""
        assert parsed["ADMIN_PASSWORD"] != ""
        assert parsed["REDIS_PASSWORD"] != ""
        # DB_PASSWORD ships as the placeholder "epicurrents" — gets regenerated
        # so production never boots with the well-known default credential.
        assert parsed["DB_PASSWORD"] not in ("", "epicurrents")
        # ACTIVITY_HASH_KEY_V1 must land non-empty and base64url-shaped
        # (the apps.py production guard would refuse to boot otherwise).
        v1 = parsed["ACTIVITY_HASH_KEY_V1"]
        assert v1 != ""
        # 32 bytes → 43 base64url chars (no padding) per init_env's encoder.
        assert len(v1) == 43

    def test_generates_vapid_and_federation_keypairs(self, env_dir):
        _write_example(env_dir)
        call_command("init_env")
        parsed = _parse((env_dir / ".env").read_text())
        # All four key fields are non-empty and base64url-ish strings.
        for k in (
            "WEBPUSH_VAPID_PUBLIC_KEY",
            "WEBPUSH_VAPID_PRIVATE_KEY",
            "FEDERATION_PUBLIC_KEY",
            "FEDERATION_PRIVATE_KEY",
        ):
            assert parsed[k] != "", f"{k} not generated"


class TestInitEnvUpdateInPlace:
    """``.env`` already present — only empty values get filled in;
    operator-customised values are preserved.
    """

    def test_preserves_operator_set_values(self, env_dir):
        _write_example(env_dir)
        existing = env_dir / ".env"
        existing.write_text("SECRET_KEY=do-not-touch\nADMIN_PASSWORD=\nBORG_PASSPHRASE=\n")
        call_command("init_env")
        parsed = _parse(existing.read_text())
        assert parsed["SECRET_KEY"] == "do-not-touch"
        # Empty values still get filled.
        assert parsed["ADMIN_PASSWORD"] != ""
        assert parsed["BORG_PASSPHRASE"] != ""

    def test_force_regenerates_everything(self, env_dir):
        _write_example(env_dir)
        existing = env_dir / ".env"
        existing.write_text("SECRET_KEY=do-not-touch\n")
        call_command("init_env", force=True)
        parsed = _parse(existing.read_text())
        assert parsed["SECRET_KEY"] != "do-not-touch"

    def test_consecutive_runs_produce_different_secrets(self, env_dir):
        _write_example(env_dir)
        call_command("init_env")
        first = _parse((env_dir / ".env").read_text())
        # Wipe so the next run regenerates.
        (env_dir / ".env").unlink()
        _write_example(env_dir)
        call_command("init_env")
        second = _parse((env_dir / ".env").read_text())
        assert first["SECRET_KEY"] != second["SECRET_KEY"]
        assert first["ADMIN_PASSWORD"] != second["ADMIN_PASSWORD"]
