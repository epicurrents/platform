"""Contract tests for the production runtime posture of the compose stack.

Properties that are invisible until the day they matter:

- **Every long-running service restarts.** Without a policy, a host reboot or an
  OOM kill leaves the database, the web tier or the workers stopped until a human
  notices. Before this was fixed only ``borg`` and ``tailscale`` carried one.
- **Every long-running service caps its logs.** Docker's json-file driver has no
  size limit by default, and gunicorn writes an access line per request, so an
  untended host fills its root disk — surfacing as Postgres refusing writes
  rather than as anything that names the disk.
- **Every service with a probe declares one.** A container that is running but
  wedged is indistinguishable from a healthy one — to compose, to an operator
  reading ``ps``, and to any load balancer in front of it.
- **The broker persists.** Redis carries the Celery queue, so without an
  append-only file a restart drops queued conversions and leaves the Recording
  rows that were waiting on them stuck with no worker coming.
- **The worker pool is bounded.** Celery's default pool is the host's CPU count,
  which sizes the container's peak memory by the host rather than by the workload.

Each is a line or two per service and each is easy to omit when adding one, which
is why they are asserted rather than documented. The inverse matters too: a
one-shot must *not* carry a restart policy, because ``unless-stopped`` restarts a
container whenever it stops, a clean exit included, so a job that runs to
completion would run forever.
"""

import re
from pathlib import Path

import yaml
from django.conf import settings

COMPOSE_DIR = Path(settings.BASE_DIR)
BASE = COMPOSE_DIR / "docker-compose.yml"
PROD = COMPOSE_DIR / "docker-compose.prod.yml"

# Services that run to completion and exit. They must never gain a restart
# policy; see the module docstring.
ONE_SHOT = {"init-volumes", "migrate"}

# Services that must declare a healthcheck. Listed rather than derived from
# "long-running", because the remaining three have no probe worth writing:
# celery-beat's DatabaseScheduler leaves no local heartbeat to read, borg spends
# the interval asleep between runs so liveness says nothing about whether backups
# succeed (that gap is the backup-alerting item, not a probe), and tailscale
# reports its own state to the tailnet.
PROBED = {"db", "redis", "web", "celery"}


def _profile_gated(services: dict) -> set:
    """Services started only via ``docker compose --profile``, read from the file.

    Derived rather than listed. A hardcoded set drifts in both directions: adding
    a profile to a service makes the test demand a restart policy it should not
    have, and removing one silently drops a service from the check — the second
    being the failure that matters.
    """
    return {name for name, spec in services.items() if (spec or {}).get("profiles")}


class _Tolerant(yaml.SafeLoader):
    """Compose carries custom tags (``!override``) that SafeLoader rejects."""


def _ignore_tag(loader, suffix, node):
    """Return the tagged node's plain value, discarding the tag.

    ``!override`` carries merge semantics this test does not care about; what
    matters is that the document parses so the keys under test are reachable.
    The node kind has to be dispatched on — a bare ``construct_object(node.value)``
    hands a list to a mapping constructor and raises "unhashable type".
    """
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_Tolerant.add_multi_constructor("!", _ignore_tag)


def _merged() -> dict:
    """Approximate `docker compose config` for the keys under test.

    A shallow per-service merge is enough here: nothing in this file's scope is
    set in both files with different values, and the alternative is shelling out
    to a compose binary the test suite cannot assume exists.
    """
    base = yaml.load(BASE.read_text(), Loader=_Tolerant)["services"]
    prod = yaml.load(PROD.read_text(), Loader=_Tolerant)["services"]
    merged = {name: dict(spec or {}) for name, spec in base.items()}
    for name, spec in prod.items():
        merged.setdefault(name, {}).update(spec or {})
    return merged


def _long_running(merged: dict) -> dict:
    gated = _profile_gated(merged)
    return {n: s for n, s in merged.items() if n not in ONE_SHOT and n not in gated}


class TestRestartPolicy:
    def test_every_long_running_service_restarts(self):
        missing = [n for n, s in _long_running(_merged()).items() if not s.get("restart")]
        assert not missing, (
            f"no restart policy on {missing}; a host reboot would leave these stopped. "
            "Add the service to docker-compose.prod.yml with <<: *production-runtime."
        )

    def test_one_shots_do_not_restart(self):
        merged = _merged()
        looping = [n for n in ONE_SHOT if merged.get(n, {}).get("restart")]
        assert not looping, (
            f"{looping} run to completion, and `unless-stopped` restarts a container on any stop including a "
            "clean exit — these would restart forever"
        )


class TestLogRotation:
    def test_every_long_running_service_caps_its_logs(self):
        uncapped = []
        for name, spec in _long_running(_merged()).items():
            options = (spec.get("logging") or {}).get("options") or {}
            if not options.get("max-size") or not options.get("max-file"):
                uncapped.append(name)
        assert not uncapped, (
            f"unbounded container logs for {uncapped}; the json-file driver has no default cap, so these can "
            "fill the host's root disk. Add the service to docker-compose.prod.yml with <<: *production-runtime."
        )

    def test_the_shared_anchor_sets_both(self):
        """Both properties come from one anchor, so a service either has the whole
        posture or none of it."""
        anchor = yaml.load(PROD.read_text(), Loader=_Tolerant)["x-production-runtime"]
        assert anchor.get("restart") == "unless-stopped"
        assert (anchor.get("logging") or {}).get("options", {}).get("max-size")


class TestBrokerPersistence:
    """Redis is the Celery broker as well as the result backend. Verified against
    a real container while this was written: with the settings below a queued task
    survives SIGKILL plus a restart, and on the stock image it does not.
    """

    def _redis(self):
        return _merged()["redis"]

    def test_redis_keeps_its_data_on_a_volume(self):
        mounts = self._redis().get("volumes") or []
        assert any(str(m).endswith(":/data") for m in mounts), (
            "redis writes its append-only file to /data; without a volume there the queue is "
            "recreated empty on every container replacement"
        )

    def test_append_only_file_is_enabled(self):
        """A volume alone is not enough — the stock image persists only via RDB
        snapshots, which are minutes apart and drop everything since the last one."""
        command = " ".join(str(part) for part in self._redis().get("command") or [])
        assert "--appendonly yes" in command

    def test_the_volume_is_declared(self):
        volumes = yaml.load(BASE.read_text(), Loader=_Tolerant).get("volumes") or {}
        assert "redis-data" in volumes


class TestHealthchecks:
    def test_probed_services_declare_a_healthcheck(self):
        merged = _merged()
        missing = [name for name in PROBED if not merged.get(name, {}).get("healthcheck")]
        assert not missing, (
            f"no healthcheck on {missing}; a wedged-but-running container is indistinguishable from a healthy "
            "one to compose, to depends_on and to any proxy in front of it"
        )

    def test_the_probe_host_matches_the_boot_warning(self):
        """``_warn_healthcheck_host`` tells operators to keep 127.0.0.1 in
        ALLOWED_HOSTS because that is the host the probe sends. Nothing else
        binds the two: point the healthcheck at localhost and the warning stays
        silent for an ALLOWED_HOSTS that now rejects every probe.
        """
        command = " ".join(_merged()["web"]["healthcheck"]["test"])
        host = re.search(r"https?://([^:/]+)", command)
        assert host, "the web healthcheck no longer requests a URL"
        assert host.group(1) == "127.0.0.1", (
            f"the probe requests {host.group(1)}, but _warn_healthcheck_host in epicurrents/apps.py "
            "checks ALLOWED_HOSTS for 127.0.0.1 — change both or the warning misleads"
        )

    def test_web_probes_readiness_not_liveness(self):
        """The web probe must hit ``/ready``, not ``/health``.

        ``/health`` answers ``{"status": "ok"}`` from a container whose database
        has gone away, so a probe pointed at it reports healthy through exactly
        the outage it exists to detect.
        """
        command = " ".join(_merged()["web"]["healthcheck"]["test"])
        assert "/api/v1/ready" in command
        assert "/api/v1/health" not in command

    def test_celery_probe_is_scoped_to_its_own_node(self):
        """An unscoped ``inspect ping`` is answered by any worker on the broker,
        so a wedged container reports healthy on a sibling's reply."""
        command = " ".join(_merged()["celery"]["healthcheck"]["test"])
        assert "inspect ping" in command
        assert "-d celery@" in command


class TestWorkerPoolIsBounded:
    """Celery sizes its own pool from the host's CPU count, which makes the worker
    container's peak scale with the host rather than with the workload: every child
    can be holding a whole recording, and a denoise peaks at roughly 20x the raw
    signal array. ``CELERY_MEM_LIMIT`` does not cover this — it is a cgroup ceiling,
    so a pool that overruns it dies by SIGKILL with no traceback rather than queueing.
    """

    def _command(self) -> str:
        command = _merged()["celery"]["command"]
        if isinstance(command, str):
            return command
        return " ".join(str(part) for part in command)

    def _default_pool_size(self):
        """The pool size compose applies when ``CELERY_CONCURRENCY`` is unset.

        Reads the effective default rather than the spelling, so pinning the flag
        to a literal is accepted and only an unbounded pool fails.
        """
        flag = re.search(r"--concurrency[= ](\S+)", self._command())
        if not flag:
            return None
        value = flag.group(1)
        interpolated = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(\d+)\}", value)
        if interpolated:
            return int(interpolated.group(1))
        return int(value) if value.isdigit() else None

    def test_the_pool_size_is_pinned(self):
        assert "--concurrency" in self._command(), (
            "the celery command sets no --concurrency, so the pool is the host's CPU count and the "
            "container's peak memory scales with the host instead of the workload"
        )

    def test_it_is_still_bounded_with_nothing_in_the_env(self):
        """The flag on its own is not enough. An interpolation with no fallback
        expands to nothing on any deployment that never sets the variable, which is
        every deployment that copies .env.example without editing it — and .env.example
        ships the line commented out.
        """
        size = self._default_pool_size()
        assert size is not None and size > 0, (
            "--concurrency has no usable default, so an unset CELERY_CONCURRENCY leaves the pool "
            "unbounded or the worker unable to start; give the interpolation a ${VAR:-N} fallback"
        )
