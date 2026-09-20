"""Target-contract tests for scripts/update.sh.

These deliberately do **not** exercise update.sh's own branching. They pin the
external *targets* the script depends on — compose service names, the database
container's env vars, the prod overlay, the HOST_PORT setting, the health
endpoint — so that drift in one of those (a renamed service, a moved health
route, a different env var) fails a test here rather than silently breaking a
production update.

Each constant mirrors a concrete assumption in update.sh; the comment names the
step that relies on it. When the script's dependencies change on purpose, update
the constants to match — that edit is the signal that a target moved.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.tests.conftest import REPO_ROOT, SCRIPTS_DIR, requires_built_frontend, requires_rsync

COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
COMPOSE_PROD = REPO_ROOT / "docker-compose.prod.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
FIXTURE = SCRIPTS_DIR / "make-bootstrap-fixture.sh"

# Services update.sh stops, recreates, execs into, or runs one-off commands in:
#   stop / up --force-recreate web celery celery-beat   (stop + recreate steps, rollback)
#   ps db / exec -T db                                  (ensure_db_up, backup, restore)
#   run --rm --no-deps web                              (migrate, collectstatic)
#   --profile build run --rm frontend-build             (repo mode)
#   ps borg                                             (borg backup branch)
REQUIRED_SERVICES = {"web", "celery", "celery-beat", "db", "frontend-build", "borg"}

# update.sh runs `sh -c '... -U "$POSTGRES_USER" "$POSTGRES_DB"'` inside the db
# container for pg_dump and the rollback restore, so the db service must export
# both of these to the container environment.
REQUIRED_DB_ENV = {"POSTGRES_USER", "POSTGRES_DB"}

# update.sh polls this after recreate: curl http://localhost:${HOST_PORT}/api/v1/health
HEALTH_PATH = "/api/v1/health"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader tolerant of Compose's custom merge tags (!override, !reset)."""


def _construct_compose_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_compose_tag)


def _load_compose(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_ComposeLoader)


def _service_env_keys(service: dict) -> set[str]:
    env = service.get("environment", []) or []
    if isinstance(env, dict):
        return set(env.keys())
    return {str(entry).split("=", 1)[0] for entry in env}


def test_compose_defines_every_service_update_touches():
    services = set(_load_compose(COMPOSE_BASE).get("services", {}))
    missing = REQUIRED_SERVICES - services
    assert not missing, (
        f"update.sh names compose services that no longer exist: {sorted(missing)}. "
        "Rename them in update.sh or restore the services."
    )


def test_frontend_build_stays_under_the_build_profile():
    # update.sh: "${COMPOSE[@]}" --profile build run --rm frontend-build
    svc = _load_compose(COMPOSE_BASE)["services"]["frontend-build"]
    assert "build" in (svc.get("profiles") or []), (
        "frontend-build must stay under the 'build' profile; update.sh starts it with --profile build."
    )


def test_db_service_exports_postgres_user_and_db():
    db = _load_compose(COMPOSE_BASE)["services"]["db"]
    missing = REQUIRED_DB_ENV - _service_env_keys(db)
    assert not missing, (
        f"db service no longer exports {sorted(missing)}; update.sh's pg_dump and "
        "rollback restore expand them inside the db container."
    )


def test_prod_overlay_exists():
    # update.sh hardcodes: -f docker-compose.yml -f docker-compose.prod.yml
    assert COMPOSE_PROD.is_file(), "docker-compose.prod.yml is missing; update.sh always runs on the prod overlay."


def test_prod_overlay_web_publishes_host_port():
    # update.sh health-checks http://localhost:${HOST_PORT}; the overlay must publish it.
    web = _load_compose(COMPOSE_PROD)["services"]["web"]
    ports = web.get("ports", []) or []
    assert any("HOST_PORT" in str(p) for p in ports), (
        "prod overlay web service no longer publishes HOST_PORT; "
        "update.sh's post-update health check targets that port."
    )


def test_env_example_defines_host_port():
    # update.sh: grep -E '^HOST_PORT=' .env  (health-check port, default 8000)
    lines = ENV_EXAMPLE.read_text().splitlines()
    assert any(line.startswith("HOST_PORT=") for line in lines), (
        "HOST_PORT is no longer declared in .env.example; update.sh reads it for the post-update health check."
    )


def test_backup_script_present_and_executable():
    # update.sh: [ -x ./scripts/backup.sh ] && ./scripts/backup.sh  (borg branch)
    backup = SCRIPTS_DIR / "backup.sh"
    assert backup.is_file(), "scripts/backup.sh is gone; update.sh's borg backup branch would silently no-op."
    assert os.access(backup, os.X_OK), "scripts/backup.sh must stay executable."


def test_manage_py_present():
    # update.sh: run --rm --no-deps web python manage.py migrate / collectstatic
    assert (REPO_ROOT / "manage.py").is_file()


@pytest.mark.django_db
def test_health_endpoint_responds():
    # update.sh polls http://localhost:${HOST_PORT}/api/v1/health after recreate.
    from django.test import Client

    resp = Client().get(HEALTH_PATH)
    assert resp.status_code == 200, (
        f"{HEALTH_PATH} returned {resp.status_code}; update.sh's health check expects 200. "
        "If the health route moved, update the script's health URL."
    )


@requires_built_frontend
@requires_rsync
def test_distribution_bundles_update_sh_for_archive_mode(tmp_path):
    # Archive mode (the dist's self-update path) consumes a tree whose root holds
    # update.sh + docker-compose.yml and a ./update drop dir. Assemble a demo
    # package and confirm the layout update.sh assumes is what the fixture ships.
    dest = tmp_path / "demo"
    result = subprocess.run(
        ["bash", str(FIXTURE), str(dest), "--demo", "--force"],
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (dest / "update.sh").is_file()
    assert os.access(dest / "update.sh", os.X_OK)
    assert (dest / "update").is_dir()
    assert (dest / "docker-compose.yml").is_file()


# The machine-readable progress lines a caller that drives update.sh parses. A
# renamed step or a dropped line breaks that caller silently — the run still
# succeeds, the caller just stops seeing the step — so the vocabulary is pinned
# here, in source, as the target it is.
PROGRESS_STEPS_UPDATE = [
    "step=check", "step=snapshot", "step=acquire", "step=backup", "step=build", "step=stop",
    "step=migrate", "step=static", "step=vendor", "step=recreate", "step=health",
]
PROGRESS_STEPS_ROLLBACK = [
    "step=restore-db", "step=restore-env", "step=restore-code", "step=build",
]
PROGRESS_FACTS = ["snapshot=", "health=ok", "health=failed", "done", "failed=", "check=ok", "refused=",
                  "archive=", "manifest=", "signature=", "sha256=", "version=", "installed=", "orphan_candidates="]

# The reasons a refusal can name on a ::refused= line. The host agent turns the
# token into the job's failure reason (refused_<token>), so its README and the
# script move together.
REFUSAL_TOKENS = ["signature", "hash", "manifest", "updater_too_old", "incompatible", "version_not_newer"]


def test_update_sh_emits_every_progress_line_a_caller_parses():
    body = (SCRIPTS_DIR / "update.sh").read_text()
    missing = [
        token for token in (*PROGRESS_STEPS_UPDATE, *PROGRESS_STEPS_ROLLBACK, *PROGRESS_FACTS)
        if f'emit "{token}' not in body
    ]
    assert not missing, f"update.sh no longer emits: {missing}"
    # ::failed comes from die(), so every refusal carries it.
    assert 'emit "failed=' in body.split("die()")[1].split("}")[0]


def test_updater_script_version_matches_what_the_packager_requires():
    # A package's manifest names the minimum update.sh it needs, and the packager
    # writes that number; the script declares what it is. The two move together.
    script = (SCRIPTS_DIR / "update.sh").read_text()
    packager = FIXTURE.read_text()
    script_version = re.search(r"^UPDATER_SCRIPT_VERSION=(\d+)$", script, re.MULTILINE).group(1)
    packager_version = re.search(r"^MIN_UPDATER_VERSION=(\d+)$", packager, re.MULTILINE).group(1)
    assert int(packager_version) <= int(script_version), (
        f"the packager requires update.sh version {packager_version} but the script is version {script_version}"
    )


def test_update_sh_never_pipes_its_own_output_into_a_quiet_grep():
    # `printf … | grep -q` under pipefail: grep exits on the first match, the
    # producer takes SIGPIPE on its next write, and a listing longer than one
    # stdio buffer reads as "no match". It refused a valid package on a real
    # host while the identical check had passed a minute earlier. Here-strings
    # have no producer process, so they cannot fail this way.
    body = (SCRIPTS_DIR / "update.sh").read_text()
    offenders = [
        line.strip()
        for line in body.splitlines()
        if not line.strip().startswith("#") and re.search(r"printf .*\| *grep +-[a-zA-Z]*q", line)
    ]
    assert not offenders, offenders


def test_update_sh_names_the_refusals_the_agent_keys_on():
    body = (SCRIPTS_DIR / "update.sh").read_text()
    missing = [token for token in REFUSAL_TOKENS if f"refuse {token} " not in body]
    assert not missing, f"update.sh no longer refuses with: {missing}"
    # Every token the script can emit is one the agent's README documents as a reason.
    readme = (SCRIPTS_DIR / "updater" / "README.md").read_text()
    undocumented = [token for token in REFUSAL_TOKENS if f"`{token}`" not in readme]
    assert not undocumented, f"scripts/updater/README.md does not document: {undocumented}"
