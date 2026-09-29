"""Registrations the app makes at boot, and the bookkeeping other files must carry for it."""

import re
from pathlib import Path

from django.conf import settings

from activity.audit import registered_masked_fields
from user.export import RELATION_HANDLING

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = REPO_ROOT / "maintenance"


class TestRegistrations:
    def test_the_user_links_are_classified_for_export(self):
        assert RELATION_HANDLING["maintenance.maintenancejob:requested_by"].mode == "export"
        assert RELATION_HANDLING["maintenance.maintenancepackage:uploaded_by"].mode == "export"

    def test_command_output_is_masked_out_of_the_audit_trail(self):
        assert registered_masked_fields("maintenance.maintenancejob") == frozenset({"output"})

    def test_the_app_is_installed_and_the_beat_tasks_scheduled(self):
        assert "maintenance.apps.MaintenanceConfig" in settings.INSTALLED_APPS
        tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        assert {"maintenance.tasks.sync_spool", "maintenance.tasks.prune_spool"} <= tasks

    def test_the_spool_default_is_the_update_directory_update_sh_uses(self):
        from epicurrents.settings import common

        assert common.MAINTENANCE_SPOOL_PATH == str(common.BASE_DIR / "update")

    def test_the_verify_window_is_clamped(self):
        from epicurrents.settings import common

        assert 5 <= common.REMOTE_UPDATE_VERIFY_WINDOW_MINUTES <= 1440


class TestVerbRegistry:
    """Every verb the app emits is in the registry activity/README.md keeps."""

    def _emitted(self) -> set[str]:
        verbs: set[str] = set()
        for path in APP_DIR.rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text()
            verbs |= set(re.findall(r'log_activity\(\s*verb="([a-z_.]+)"', source))
            verbs |= set(re.findall(r'with_system_activity\(\s*"([a-z_.]+)"', source))
        return verbs

    def test_every_emitted_verb_is_registered(self):
        readme = (REPO_ROOT / "activity" / "README.md").read_text()
        emitted = self._emitted()
        assert emitted, "the scan found no verbs; the regex has drifted from the call sites"
        missing = {verb for verb in emitted if f"`{verb}`" not in readme}
        assert not missing, (
            f"verbs emitted by the maintenance app but absent from activity/README.md: {sorted(missing)}"
        )

    def test_every_verb_has_the_app_prefix(self):
        assert all(verb.startswith("maintenance.") for verb in self._emitted())


class TestDocumentation:
    def test_the_app_has_a_readme_and_agents_lists_it(self):
        assert (APP_DIR / "README.md").is_file()
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        assert "| `maintenance` |" in agents
        assert "/api/v1/maintenance/" in agents

    def test_the_gdpr_inventory_names_the_job_row(self):
        inventory = (REPO_ROOT / "docs" / "gdpr-compliance.md").read_text()
        assert "maintenance.MaintenanceJob" in inventory

    def test_env_example_documents_the_flags(self):
        example = (REPO_ROOT / ".env.example").read_text()
        assert "REMOTE_MAINTENANCE_ENABLED=false" in example
        assert "REMOTE_UPDATE_ENABLED=false" in example


class TestDeploymentPairing:
    """The production overlay must let the containers see the spool the settings point at."""

    def test_the_overlay_mounts_the_spool_into_web_and_celery(self):
        overlay = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert overlay.count("- ./update:/data/update") == 2, "web and celery each bind the spool"
        assert overlay.count("- MAINTENANCE_SPOOL_PATH=/data/update") == 2, "and each tells Django where it is"

    def test_update_sh_keeps_the_spool_out_of_every_sync_and_snapshot(self):
        script = (REPO_ROOT / "scripts" / "update.sh").read_text()
        assert script.count("--exclude") >= 3
        # The code snapshot, the rollback's replace and the update's overlay,
        # each anchored at the root so an app directory of the same name is
        # still code.
        assert (
            '--exclude="./update"' in script and '--exclude="/update/"' in script and "--exclude='/update/'" in script
        )
