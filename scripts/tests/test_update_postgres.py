"""Real-database tests for the rollback restore in scripts/update.sh.

The fakebin suite can prove the restore is *sent* — a schema drop ahead of the
dump, inside one transaction — but not what PostgreSQL does with it, and the
defect this file guards against lived entirely on that side. ``pg_dump --clean``
drops only what it dumped, so a table the failed release created survived a
restore while the restored ``django_migrations`` said its migration never ran.
The next ``migrate`` then died on "relation already exists", after taking a
fresh snapshot, and rolled back again: a loop with no exit but a shell.

So these run the real script against a real server. A throwaway container of
the image the stack's ``db`` service pins is driven through a ``docker`` stub
that forwards the script's ``compose exec -T db`` calls into it and answers
everything else the way the dry-run stubs do — the dump is taken by the script's
own ``pg_dump`` command and restored by its own ``psql`` command, so what is
tested is the text of the script, not a re-statement of it. Skipped where no
Docker daemon answers (the compose ``test`` services, a host without Docker);
the CI runners have one.
"""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess
import time
import uuid

import pytest

from scripts.tests.conftest import REPO_ROOT, make_env, run_script

DOCKER = shutil.which("docker")
DB_USER = "epicurrents"
DB_NAME = "epicurrents"


def _docker_usable() -> bool:
    if not DOCKER:
        return False
    probe = subprocess.run([DOCKER, "info"], capture_output=True, check=False)
    return probe.returncode == 0


requires_docker = pytest.mark.skipif(not _docker_usable(), reason="no usable Docker daemon for a throwaway PostgreSQL")


def _postgres_image() -> str:
    """The exact image the stack's ``db`` service runs, so the dump and restore are
    exercised against the pg_dump and psql a deployment actually has."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    match = re.search(r"^\s*image:\s*(postgres:\S+)", compose, re.MULTILINE)
    assert match, "docker-compose.yml no longer pins a postgres image for the db service"
    return match.group(1)


@pytest.fixture
def postgres():
    """A running PostgreSQL container, removed afterwards whatever happened."""
    name = f"epicurrents-update-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            DOCKER, "run", "-d", "--rm", "--name", name,
            "-e", f"POSTGRES_USER={DB_USER}",
            "-e", "POSTGRES_PASSWORD=update-test",
            "-e", f"POSTGRES_DB={DB_NAME}",
            _postgres_image(),
        ],
        check=True,
        capture_output=True,
    )
    try:
        # Over TCP rather than the socket: the image's init runs a temporary
        # server that listens on the socket only, and readiness there is not
        # readiness of the server the tests will talk to.
        for _ in range(90):
            probe = subprocess.run(
                [DOCKER, "exec", name, "pg_isready", "-h", "localhost", "-U", DB_USER, "-d", DB_NAME],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("PostgreSQL did not become ready")
        yield name
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, check=False)


def sql(container: str, statements: str) -> str:
    """Run ``statements`` in the container's psql and return the unaligned output."""
    result = subprocess.run(
        [DOCKER, "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-tA", "-U", DB_USER, "-d", DB_NAME],
        input=statements,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _deploy(fakebin, tmp_path, container: str):
    """A fake deployment whose ``compose exec -T db`` reaches the real container.

    The last argument of that call is the shell command the script wants run
    inside the db container, with ``$POSTGRES_USER`` and ``$POSTGRES_DB`` to be
    expanded there — which the image's environment provides, since the container
    was started with them. Everything else answers as a running stack.
    """
    make_env(tmp_path)
    tmp_path.chmod(0o777)
    fakebin.stub(
        "docker",
        body=f"""
case "$1" in
    --version) echo "Docker version 29.0.0, build test"; exit 0 ;;
esac
case "$*" in
    *" exec -T db sh -c "*)
        for a in "$@"; do cmd="$a"; done
        exec "{DOCKER}" exec -i "{container}" sh -c "$cmd"
        ;;
    *" ps "*) echo running ;;
    *" exec "*) cat >/dev/null 2>&1 || true ;;
esac
""",
    )
    fakebin.stub("cp", body='exec /bin/cp "$@"')


SEED = """
CREATE TABLE django_migrations (id serial PRIMARY KEY, app varchar(255) NOT NULL, name varchar(255) NOT NULL);
CREATE TABLE app_row (id serial PRIMARY KEY, payload jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE INDEX app_row_payload_idx ON app_row USING gin (payload);
INSERT INTO django_migrations (app, name) VALUES ('recordings', '0001_initial');
INSERT INTO app_row (payload) VALUES ('{"k": 1}');
"""

# What a release that adds a table does: the table, a row in django_migrations
# saying its migration ran, and — because the services were still up — a write
# that landed after the dump.
FAILED_RELEASE = """
CREATE TABLE recordings_newthing (id serial PRIMARY KEY, ref integer REFERENCES app_row (id));
INSERT INTO django_migrations (app, name) VALUES ('recordings', '0002_newthing');
INSERT INTO app_row (payload) VALUES ('{"k": 2}');
"""


def _snapshot_dump(tmp_path):
    snapshots = list((tmp_path / "backups").glob("pre-update-*/db.sql.gz"))
    assert len(snapshots) == 1, snapshots
    return snapshots[0]


def _update_then_break(fakebin, tmp_path, postgres):
    """Seed, take the snapshot through the script, then apply the failed release."""
    _deploy(fakebin, tmp_path, postgres)
    sql(postgres, SEED)
    result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--from", "repo", "--no-pull"])
    assert result.returncode == 0, result.stderr
    dump = _snapshot_dump(tmp_path)
    with gzip.open(dump, "rt") as fh:
        assert "CREATE TABLE public.app_row" in fh.read(), "the snapshot is not a dump of the seeded schema"
    sql(postgres, FAILED_RELEASE)
    assert sql(postgres, "SELECT count(*) FROM django_migrations;") == "2"
    # The call log so far belongs to the update; the rollback gets a clean one.
    fakebin.log.write_text("")
    return dump


@requires_docker
class TestRollbackRestore:
    def test_a_table_the_failed_release_created_is_gone_and_the_retry_works(self, fakebin, tmp_path, postgres):
        _update_then_break(fakebin, tmp_path, postgres)

        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode == 0, result.stderr

        assert sql(postgres, "SELECT to_regclass('public.recordings_newthing') IS NULL;") == "t", (
            "the failed release's table survived the restore"
        )
        assert sql(postgres, "SELECT count(*) FROM django_migrations;") == "1"
        assert sql(postgres, "SELECT count(*) FROM app_row;") == "1", "a write made after the dump survived"
        assert sql(postgres, "SELECT payload->>'k' FROM app_row;") == "1"
        # The retry that used to die on "relation already exists".
        sql(postgres, "CREATE TABLE recordings_newthing (id serial PRIMARY KEY, ref integer REFERENCES app_row (id));")
        # And the restored schema is usable, not merely present: a serial that
        # came back at its dumped value, an index that came back with the table.
        sql(postgres, "INSERT INTO app_row (payload) VALUES ('{\"k\": 3}');")
        assert sql(postgres, "SELECT max(id) FROM app_row;") == "2"
        assert sql(postgres, "SELECT count(*) FROM pg_indexes WHERE indexname = 'app_row_payload_idx';") == "1"

    def test_a_restore_that_fails_leaves_the_database_exactly_as_it_was(self, fakebin, tmp_path, postgres):
        # The schema drop runs inside the restore's transaction, so a dump that
        # fails half way must roll the drop back too — the alternative is an
        # empty database, which is worse than the state the rollback started from.
        dump = _update_then_break(fakebin, tmp_path, postgres)
        with gzip.open(dump, "rt") as fh:
            body = fh.read()
        with gzip.open(dump, "wt") as fh:
            fh.write(body + "\nSELECT 1/0;\n")

        result = run_script("update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"])
        assert result.returncode != 0
        assert "restore FAILED" in result.stderr
        assert not fakebin.has_call("--force-recreate"), "a failed restore must not recreate the stack"

        assert sql(postgres, "SELECT to_regclass('public.recordings_newthing') IS NOT NULL;") == "t"
        assert sql(postgres, "SELECT count(*) FROM django_migrations;") == "2"
        assert sql(postgres, "SELECT count(*) FROM app_row;") == "2"

    def test_a_connection_holding_a_lock_fails_the_restore_rather_than_hanging(self, fakebin, tmp_path, postgres):
        # The borg case: a dump in progress holds a share lock on every table,
        # which the schema drop cannot get past. The script stops borg first;
        # this is what happens if something else holds one. A bounded wait, a
        # failure that names the cause, and a database left untouched.
        _update_then_break(fakebin, tmp_path, postgres)
        subprocess.run(
            [
                DOCKER, "exec", "-d", postgres, "psql", "-U", DB_USER, "-d", DB_NAME,
                "-c", "SELECT (SELECT count(*) FROM app_row), pg_sleep(120);",
            ],
            check=True,
            capture_output=True,
        )
        for _ in range(20):
            active = sql(
                postgres,
                "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(120)%' AND state = 'active'"
                " AND pid <> pg_backend_pid();",
            )
            if active == "1":
                break
            time.sleep(0.5)
        else:
            pytest.fail("the lock-holding session never became active")

        started = time.monotonic()
        result = run_script(
            "update.sh", fakebin, cwd=tmp_path, args=["--rollback", "--yes"],
            extra_env={"UPDATE_RESTORE_LOCK_TIMEOUT": "2s"},
        )
        elapsed = time.monotonic() - started
        assert result.returncode != 0
        assert elapsed < 60, f"the restore waited {elapsed:.0f}s for a lock it was told to give up on after 2s"
        assert "lock timeout" in result.stderr, result.stderr
        assert "restore FAILED" in result.stderr
        assert sql(postgres, "SELECT to_regclass('public.recordings_newthing') IS NOT NULL;") == "t"
        assert sql(postgres, "SELECT count(*) FROM app_row;") == "2"
