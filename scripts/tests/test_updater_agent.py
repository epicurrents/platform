"""Dry-run tests for scripts/updater/epicurrents-updater.sh, the remote-maintenance host agent.

The agent is driven against a staged deployment with the fakebin harness: the
container runtime, the readiness probe and ``chown`` are stubs that log their
calls, and ``update.sh`` is a stand-in that speaks the ``::`` protocol the real
one does, with its behaviour keyed by marker files so a test can make a check
refuse, an update fail before or after its snapshot, or a rollback fail. What
the tests assert is the agent's own contract: which state each situation ends
in, what it wrote into the spool, what it ran and in which order, and what it
never touched. Nothing here runs a container.
"""

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.tests.conftest import SCRIPTS_DIR, FakeBin, system_path

AGENT = SCRIPTS_DIR / "updater" / "epicurrents-updater.sh"
INSTALLER = SCRIPTS_DIR / "updater" / "install-updater.sh"

# A stand-in for update.sh. It records every invocation, one per line, in
# $FAKE_DIR/calls, and speaks the :: lines the agent follows. Marker files in
# $FAKE_DIR change its behaviour: refuse-<token> makes --check-archive refuse
# with that token, fail-before and fail-after make an update exit non-zero
# before or after its snapshot, snapshot-fail fails --snapshot, rollback-fail
# fails --rollback. Versions come from $FAKE_DIR/installed and /package.
FAKE_UPDATE_SH = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DIR/calls"
ROOT=""
MODE=update
LABEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        --check-archive) MODE=check; shift 2 ;;
        --snapshot) MODE=snapshot; LABEL="$2"; shift 2 ;;
        --rollback) MODE=rollback; shift ;;
        --archive) shift 2 ;;
        --release-key) shift 2 ;;
        *) shift ;;
    esac
done
[ -n "$ROOT" ] || { echo "::failed=no --root"; exit 2; }
cd "$ROOT" || exit 2
installed="$(cat "$FAKE_DIR/installed")"
package="$(cat "$FAKE_DIR/package")"
case "$MODE" in
    check)
        for marker in "$FAKE_DIR"/refuse-*; do
            [ -e "$marker" ] || continue
            token="${marker##*/refuse-}"
            echo "::refused=$token"
            echo "::failed=refused ($token) by the stand-in"
            exit 1
        done
        if [ -e "$FAKE_DIR/check-fail-unnamed" ]; then
            echo "::failed=the stand-in refused without a reason"
            exit 1
        fi
        echo "::installed=$installed"
        echo "::version=$package"
        echo "::check=ok"
        exit 0
        ;;
    snapshot)
        if [ "$MODE" = snapshot ] && [ "$LABEL" != post-update ]; then
            # --rollback --snapshot NAME is parsed as MODE=snapshot too; tell them apart by the label.
            :
        fi
        if [ -e "$FAKE_DIR/rollback-mode" ]; then :; fi
        ;;
esac
if [ "$MODE" = snapshot ] && [ "$LABEL" = post-update ]; then
    if [ -e "$FAKE_DIR/snapshot-fail" ]; then
        echo "::failed=the stand-in could not snapshot"
        exit 1
    fi
    mkdir -p "backups/post-update-20260920-120000"
    echo "::step=snapshot"
    echo "::snapshot=./backups/post-update-20260920-120000"
    echo "::done"
    exit 0
fi
if [ "$MODE" = snapshot ] || [ "$MODE" = rollback ]; then
    # --rollback --snapshot NAME
    echo "::step=stop"
    echo "::step=restore-db"
    if [ -e "$FAKE_DIR/rollback-fail" ]; then
        echo "::failed=the stand-in could not restore"
        exit 1
    fi
    echo "::step=restore-code"
    echo "::step=build"
    echo "::step=recreate"
    echo "::step=health"
    echo "::health=ok"
    printf '%s' "$installed" > "$FAKE_DIR/running-version"
    echo "::done"
    exit 0
fi
echo "::step=check"
echo "::step=snapshot"
echo "::step=acquire"
if [ -e "$FAKE_DIR/fail-before" ]; then
    echo "::failed=the stand-in failed before the snapshot"
    exit 1
fi
echo "::step=backup"
mkdir -p "backups/pre-update-20260920-100000"
echo "::snapshot=./backups/pre-update-20260920-100000"
echo "::step=build"
if [ -e "$FAKE_DIR/fail-after" ]; then
    echo "::failed=the stand-in failed after the snapshot"
    exit 1
fi
if [ -e "$FAKE_DIR/chatty" ]; then
    i=0
    while [ $i -lt 400 ]; do
        echo "build output line $i padding padding padding padding padding padding padding"
        i=$((i + 1))
    done
fi
echo "::step=migrate"
echo "::step=recreate"
echo "::step=health"
echo "::health=ok"
printf '%s' "$package" > "$FAKE_DIR/running-version"
echo "::done"
exit 0
"""

DOCKER_STUB = r"""
case "$*" in
    *"--version"*) echo "Docker version 25.0.0, build abc" ;;
    *"exec -T web python -c"*) cat "$FAKE_DIR/running-version"; echo ;;
    *"inspect active"*) cat "$FAKE_DIR/inspect-active" 2>/dev/null || echo "{}" ;;
esac
"""

CURL_STUB = 'cat "$FAKE_DIR/http-code"'
# Behind the harness stub the real tool must be named absolutely, or the stub finds itself.
REAL_SHA256SUM = 'if [ -x /usr/bin/sha256sum ]; then exec /usr/bin/sha256sum "$@"; fi\nexec /usr/bin/shasum -a 256 "$@"'


@dataclass
class Staged:
    """A staged deployment plus the agent's own directories."""

    root: Path
    config_dir: Path
    lib_dir: Path
    state_dir: Path
    fake_dir: Path
    boot_id: Path
    fakebin: FakeBin

    @property
    def spool(self) -> Path:
        return self.root / "update"

    @property
    def jobs(self) -> Path:
        return self.spool / "jobs"

    @property
    def packages(self) -> Path:
        return self.spool / "packages"

    def status(self, job_id: str) -> dict:
        return json.loads((self.jobs / f"{job_id}.status.json").read_text())

    def log(self, job_id: str) -> str:
        path = self.jobs / f"{job_id}.log"
        return path.read_text() if path.exists() else ""

    def flag(self) -> dict | None:
        path = self.spool / "maintenance.json"
        return json.loads(path.read_text()) if path.exists() else None

    def heartbeat(self) -> dict | None:
        path = self.spool / "agent.json"
        return json.loads(path.read_text()) if path.exists() else None

    def update_sh_calls(self) -> list[str]:
        path = self.fake_dir / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def mark(self, name: str) -> None:
        (self.fake_dir / name).write_text("")

    def config(self, **values) -> None:
        current = {}
        for line in (self.config_dir / "config").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                current[key] = value.strip('"')
        current.update({k: str(v) for k, v in values.items()})
        (self.config_dir / "config").write_text("".join(f'{k}="{v}"\n' for k, v in current.items()))


def _stage(tmp_path, fakebin, *, enabled=True, installed="0.1.0", package="0.2.0", **config) -> Staged:
    root = tmp_path / "deploy"
    (root / "update" / "jobs").mkdir(parents=True)
    (root / "update" / "packages").mkdir()
    (root / "epicurrents").mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "docker-compose.prod.yml").write_text("services: {}\n")
    (root / ".env").write_text("HOST_PORT=8001\n")
    (root / "epicurrents" / "version.py").write_text(f'__version__ = "{installed}"\n')

    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "release.pub").write_text(
        "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAtest\n-----END PUBLIC KEY-----\n"
    )
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(AGENT, lib_dir / "epicurrents-updater.sh")
    (lib_dir / "update.sh").write_text(FAKE_UPDATE_SH)
    (lib_dir / "update.sh").chmod(0o755)
    state_dir = tmp_path / "state"
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    (fake_dir / "installed").write_text(installed)
    (fake_dir / "package").write_text(package)
    (fake_dir / "running-version").write_text(installed)
    (fake_dir / "http-code").write_text("200")
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-one\n")

    fakebin.stub("docker", body=DOCKER_STUB)
    fakebin.stub("curl", body=CURL_STUB)
    fakebin.stub("sha256sum", body=REAL_SHA256SUM)
    # The harness stubs cp to a no-op to protect the host during bootstrap; the
    # agent copies the package out of the spool, so it needs the real one.
    fakebin.stub("cp", body='exec /bin/cp "$@"')
    fakebin.stub("chmod", body='exec /bin/chmod "$@"')
    fakebin.stub("logger")

    staged = Staged(root, config_dir, lib_dir, state_dir, fake_dir, boot_id, fakebin)
    values = {
        "DEPLOY_ROOT": str(root),
        "ENABLED": "1" if enabled else "0",
        "ALLOW_CHECKOUT": "0",
        "MIN_FREE_BYTES": "1024",
        "HEALTH_TIMEOUT": "1",
        "DRAIN_TIMEOUT": "1",
        "VERIFY_WINDOW_MINUTES": "30",
    }
    values.update({k: str(v) for k, v in config.items()})
    (config_dir / "config").write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
    return staged


def _package(staged: Staged, content: bytes = b"not really a tarball", *, sha: str | None = None) -> str:
    digest = sha or hashlib.sha256(content).hexdigest()
    pkgdir = staged.packages / digest
    pkgdir.mkdir(parents=True)
    (pkgdir / "package.tar.gz").write_bytes(content)
    (pkgdir / "manifest.json").write_text(f'{{\n  "sha256": "{digest}"\n}}\n')
    (pkgdir / "manifest.sig").write_text("c2lnbmF0dXJl\n")
    return digest


def _request(staged: Staged, sha: str, *, job_id=None, operation="platform.update", protocol=1, window=None, args=None):
    job_id = job_id or str(uuid.uuid4())
    body = {
        "protocol": protocol,
        "job_id": job_id,
        "operation": operation,
        "requested_by_id": 1,
        "requested_at": "2026-09-20T10:00:00Z",
        "args": args if args is not None else {"package_sha256": sha},
    }
    if window is not None:
        body["args"]["verify_window_minutes"] = window
    (staged.jobs / f"{job_id}.json").write_text(json.dumps(body))
    return job_id


def _run(staged: Staged, **env) -> subprocess.CompletedProcess:
    environment = {
        "PATH": f"{staged.fakebin.path}:{system_path(staged.fakebin.path, staged.fakebin.removed)}",
        "HOME": str(staged.root.parent),
        "LC_ALL": "C",
        "FAKE_DIR": str(staged.fake_dir),
        "EPICURRENTS_UPDATER_CONFIG_DIR": str(staged.config_dir),
        "EPICURRENTS_UPDATER_LIB_DIR": str(staged.lib_dir),
        "EPICURRENTS_UPDATER_STATE_DIR": str(staged.state_dir),
        "EPICURRENTS_UPDATER_LOCK_FILE": str(staged.root.parent / "run.lock"),
        "EPICURRENTS_UPDATER_BOOT_ID_FILE": str(staged.boot_id),
        "EPICURRENTS_UPDATER_POLL_SECONDS": "0",
        **env,
    }
    return subprocess.run(
        ["bash", str(staged.lib_dir / "epicurrents-updater.sh")],
        check=False,
        cwd=str(staged.root.parent),
        env=environment,
        capture_output=True,
        text=True,
    )


def _to_window(staged: Staged) -> str:
    """Stage a package and a request and run one tick: the job reaches its window."""
    sha = _package(staged)
    job_id = _request(staged, sha)
    result = _run(staged)
    assert result.returncode == 0, result.stderr
    assert staged.status(job_id)["state"] == "awaiting_verification", staged.log(job_id)
    return job_id


STATUS_FIELDS = {
    "protocol",
    "job_id",
    "state",
    "reason",
    "step",
    "updated_at",
    "started_at",
    "finished_at",
    "verify_deadline",
    "snapshot",
    "post_snapshot",
    "agent_version",
    "installed_version_before",
    "target_version",
    "running_version",
    "retries",
}


class TestHeartbeatAndConfig:
    def test_no_config_does_nothing_and_exits_zero(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.config_dir / "config").unlink()
        result = _run(staged)
        assert result.returncode == 0
        assert staged.heartbeat() is None
        assert not fakebin.has_call("docker")

    def test_a_disabled_agent_reports_itself_and_refuses_requests(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin, enabled=False)
        sha = _package(staged)
        job_id = _request(staged, sha)
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        beat = staged.heartbeat()
        assert beat["protocol"] == 1 and beat["enabled"] is False and beat["version"] == "1"
        assert beat["runtime"] == "docker" and beat["last_run"].endswith("Z")
        assert beat["capabilities"] == ["platform.update"]
        status = staged.status(job_id)
        assert status["state"] == "failed" and status["reason"] == "refused_disabled"
        assert staged.update_sh_calls() == []
        assert staged.flag() is None

    def test_an_enabled_agent_reports_the_updater_script_version_it_carries(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.lib_dir / "update.sh").write_text(
            "#!/bin/sh\nUPDATER_SCRIPT_VERSION=7\n" + FAKE_UPDATE_SH[len("#!/bin/sh\n") :]
        )
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        assert staged.heartbeat()["enabled"] is True
        assert staged.heartbeat()["updater_script"] == "7"

    def test_a_missing_runtime_is_reported_in_the_heartbeat_and_fails_the_tick(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        fakebin.remove("docker")
        fakebin.remove("podman")
        result = _run(staged)
        assert result.returncode != 0
        assert staged.heartbeat()["runtime"] is None
        assert "no container runtime" in result.stderr

    def test_a_podman_host_drives_podman_compose(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        fakebin.remove("docker")
        fakebin.stub("podman", body=DOCKER_STUB.replace("Docker version 25.0.0, build abc", "podman version 5.8.2"))
        _to_window(staged)
        assert staged.heartbeat()["runtime"] == "podman"
        assert fakebin.has_call("podman compose -f docker-compose.yml -f docker-compose.prod.yml stop celery-beat")

    def test_the_proxy_overlay_is_added_when_env_names_a_domain(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.root / ".env").write_text("HOST_PORT=8001\nPROXY_DOMAIN=example.test\n")
        _to_window(staged)
        assert fakebin.has_call("-f docker-compose.proxy.yml stop celery-beat")


class TestAcceptance:
    def test_a_good_request_is_applied_to_its_verification_window(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        sha = _package(staged)
        job_id = _request(staged, sha, window=45)
        result = _run(staged)
        assert result.returncode == 0, result.stderr

        status = staged.status(job_id)
        assert set(status) == STATUS_FIELDS
        assert status["state"] == "awaiting_verification" and status["reason"] == ""
        assert status["installed_version_before"] == "0.1.0" and status["target_version"] == "0.2.0"
        assert status["running_version"] == "0.2.0"
        assert status["snapshot"] == "pre-update-20260920-100000"
        assert status["started_at"].endswith("Z") and status["finished_at"] is None
        assert status["verify_deadline"].endswith("Z") and status["agent_version"] == "1"

        calls = staged.update_sh_calls()
        assert len(calls) == 2
        check, update = calls
        assert check.startswith(f"--root {staged.root} --check-archive {staged.state_dir}/work/{job_id}/package.tar.gz")
        assert "--require-signature" in check and f"--release-key {staged.config_dir}/release.pub" in check
        assert "--require-newer" in check
        assert update.startswith(f"--root {staged.root} --archive {staged.state_dir}/work/{job_id}/package.tar.gz")
        for flag in ("--require-signature", "--require-newer", "--skip-beat", "--keep-lock", "--yes"):
            assert flag in update, update

        flag = staged.flag()
        assert flag["phase"] == "verifying" and flag["job_id"] == job_id
        assert flag["expected_until"] == status["verify_deadline"] and flag["protocol"] == 1
        assert not (staged.spool / "lock").exists(), "the active-run lock outlives the run"
        log = staged.log(job_id)
        assert "::snapshot=" in log and "awaiting verification" in log

    def test_the_window_defaults_to_the_config_and_is_clamped(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin, VERIFY_WINDOW_MINUTES=10)
        sha = _package(staged)
        job_id = _request(staged, sha, window=99999)
        _run(staged)
        deadline = staged.status(job_id)["verify_deadline"]
        # 1440 minutes: the deadline lies a day out, not 99999 minutes.
        from datetime import datetime, timedelta, timezone

        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        assert timedelta(hours=23) < parsed - datetime.now(timezone.utc) <= timedelta(hours=24)

    def test_beat_is_stopped_and_the_worker_drained_before_the_update(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        _to_window(staged)
        calls = fakebin.calls()
        stop = next(i for i, c in enumerate(calls) if "stop celery-beat" in c)
        drain = next(i for i, c in enumerate(calls) if "inspect active" in c)
        assert stop < drain
        # beat stays down through the window: no `up -d celery-beat` yet.
        assert not fakebin.has_call("up -d celery-beat")

    def test_a_busy_worker_is_waited_for_and_then_proceeded_past(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.fake_dir / "inspect-active").write_text('{"celery@host": [{"id": "t1"}]}')
        job_id = _to_window(staged)
        assert "still busy" in staged.log(job_id)

    def test_status_files_are_handed_to_the_tree_owner_before_they_land(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        chowns = [c for c in fakebin.calls() if c.startswith("chown")]
        assert any(f"{job_id}.status.json.tmp" in c for c in chowns), chowns
        assert any("agent.json.tmp" in c for c in chowns)
        assert any("maintenance.json.tmp" in c for c in chowns)
        assert not (staged.jobs / f"{job_id}.status.json.tmp").exists()

    def test_the_agents_update_sh_is_refreshed_from_a_package_that_applied(self, fakebin, tmp_path):
        import tarfile

        staged = _stage(tmp_path, fakebin)
        tree = tmp_path / "pkg" / "epicurrents-0.2.0"
        tree.mkdir(parents=True)
        (tree / "update.sh").write_text("#!/usr/bin/env bash\nUPDATER_SCRIPT_VERSION=3\necho from-the-package\n")
        archive = tmp_path / "pkg.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(tree, arcname="epicurrents-0.2.0")
        sha = _package(staged, archive.read_bytes())
        job_id = _request(staged, sha)
        _run(staged)
        assert staged.status(job_id)["state"] == "awaiting_verification"
        assert "from-the-package" in (staged.lib_dir / "update.sh").read_text()
        assert os.access(staged.lib_dir / "update.sh", os.X_OK)

    def test_a_package_without_an_update_sh_leaves_the_agents_copy(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        assert (staged.lib_dir / "update.sh").read_text() == FAKE_UPDATE_SH
        assert "keeps its copy" in staged.log(job_id)

    def test_a_second_request_waits_while_a_job_is_in_flight(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        first = _to_window(staged)
        second = _request(staged, _package(staged, b"another package"))
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        assert not (staged.jobs / f"{second}.status.json").exists()
        assert staged.status(first)["state"] == "awaiting_verification"
        assert len(staged.update_sh_calls()) == 2

    def test_a_request_whose_body_names_another_job_is_ignored(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        sha = _package(staged)
        job_id = _request(staged, sha)
        body = json.loads((staged.jobs / f"{job_id}.json").read_text())
        body["job_id"] = str(uuid.uuid4())
        (staged.jobs / f"{job_id}.json").write_text(json.dumps(body))
        _run(staged)
        assert not (staged.jobs / f"{job_id}.status.json").exists()


class TestRefusals:
    """Every refusal is a failed status naming the reason, with nothing else touched."""

    def _refused(self, staged: Staged, job_id: str, reason: str) -> None:
        status = staged.status(job_id)
        assert status["state"] == "failed", staged.log(job_id)
        assert status["reason"] == f"refused_{reason}"
        assert status["finished_at"] is not None and status["started_at"] is None
        assert staged.flag() is None
        assert not (staged.spool / "lock").exists()
        assert not any("--archive" in c for c in staged.update_sh_calls()), "the update ran"
        assert not (staged.state_dir / "work" / job_id).exists(), "the work copy was left behind"
        assert not staged.fakebin.has_call("stop celery-beat")

    def test_protocol(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _request(staged, _package(staged), protocol=2)
        _run(staged)
        self._refused(staged, job_id, "protocol")

    def test_operation(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _request(staged, _package(staged), operation="platform.rollback")
        _run(staged)
        self._refused(staged, job_id, "operation")

    def test_a_request_with_a_command_in_its_arguments_is_still_only_a_hash(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _request(staged, "", args={"command": "rm -rf /", "package_sha256": "nope"})
        _run(staged)
        self._refused(staged, job_id, "hash")

    def test_a_hash_that_names_no_package(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _request(staged, "a" * 64)
        _run(staged)
        self._refused(staged, job_id, "hash")
        assert "no uploaded package" in staged.log(job_id)

    def test_a_package_whose_bytes_do_not_match_the_request(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        sha = _package(staged, b"these bytes", sha="b" * 64)
        job_id = _request(staged, sha)
        _run(staged)
        self._refused(staged, job_id, "hash")
        assert "sha256 is" in staged.log(job_id)

    def test_a_git_checkout(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.root / ".git").mkdir()
        job_id = _request(staged, _package(staged))
        _run(staged)
        self._refused(staged, job_id, "checkout")

    def test_a_git_checkout_when_allowed(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin, ALLOW_CHECKOUT=1)
        (staged.root / ".git").mkdir()
        job_id = _request(staged, _package(staged))
        _run(staged)
        assert staged.status(job_id)["state"] == "awaiting_verification"

    def test_too_little_disk(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin, MIN_FREE_BYTES=10**18)
        job_id = _request(staged, _package(staged))
        _run(staged)
        self._refused(staged, job_id, "disk")

    @pytest.mark.parametrize(
        "token", ["signature", "hash", "manifest", "updater_too_old", "incompatible", "version_not_newer"]
    )
    def test_what_update_sh_refuses_by_name(self, fakebin, tmp_path, token):
        staged = _stage(tmp_path, fakebin)
        staged.mark(f"refuse-{token}")
        job_id = _request(staged, _package(staged))
        _run(staged)
        self._refused(staged, job_id, token)
        assert "refused by the stand-in" in staged.log(job_id) or "stand-in" in staged.log(job_id)

    def test_an_unnamed_refusal_by_update_sh(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        staged.mark("check-fail-unnamed")
        job_id = _request(staged, _package(staged))
        _run(staged)
        self._refused(staged, job_id, "check")


class TestTheWindow:
    def test_a_verify_marker_settles_the_job_and_lifts_the_lock(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        (staged.jobs / f"{job_id}.verify").write_text('{"protocol": 1, "at": "2026-09-20T10:30:00Z", "by_user_id": 1}')
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        status = staged.status(job_id)
        assert status["state"] == "succeeded" and status["finished_at"] is not None
        assert status["snapshot"] == "pre-update-20260920-100000", "the snapshot survives for a late rollback"
        assert staged.flag() is None
        assert fakebin.has_call("up -d celery-beat")
        assert not (staged.state_dir / "work" / job_id).exists()
        assert len(staged.update_sh_calls()) == 2

    def test_a_rollback_marker_rolls_back_with_a_post_update_snapshot_first(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        (staged.jobs / f"{job_id}.rollback").write_text(
            '{"protocol": 1, "at": "2026-09-20T10:30:00Z", "by_user_id": 1}'
        )
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        status = staged.status(job_id)
        assert status["state"] == "rolled_back" and status["reason"] == "requested"
        assert status["post_snapshot"] == "post-update-20260920-120000"
        assert status["running_version"] == "0.1.0"
        calls = staged.update_sh_calls()[2:]
        assert calls[0].startswith(f"--root {staged.root} --snapshot post-update")
        assert "--keep-lock" in calls[0]
        assert calls[1].startswith(f"--root {staged.root} --rollback --snapshot pre-update-20260920-100000")
        assert "--yes" in calls[1] and "--skip-beat" in calls[1] and "--keep-lock" in calls[1]
        assert staged.flag() is None
        assert fakebin.has_call("up -d celery-beat")
        assert not (staged.jobs / f"{job_id}.rollback").exists()

    def test_the_flag_reads_rolling_back_while_it_happens(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        (staged.jobs / f"{job_id}.rollback").write_text('{"protocol": 1, "by_user_id": 1}')
        staged.mark("rollback-fail")
        _run(staged)
        assert staged.status(job_id)["state"] == "rollback_failed"
        assert staged.flag()["phase"] == "rolling_back" and staged.flag()["job_id"] == job_id

    def test_a_closed_window_rolls_back(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        status = staged.status(job_id)
        status["verify_deadline"] = "2020-01-01T00:00:00Z"
        (staged.jobs / f"{job_id}.status.json").write_text(json.dumps(status))
        _run(staged)
        assert staged.status(job_id)["state"] == "rolled_back"
        assert staged.status(job_id)["reason"] == "deadline"

    def test_an_open_window_is_left_alone(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        before = staged.status(job_id)
        result = _run(staged)
        assert result.returncode == 0, result.stderr
        assert staged.status(job_id) == before
        assert len(staged.update_sh_calls()) == 2

    def test_a_confirmation_beats_a_rollback_marker_written_after_it(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        (staged.jobs / f"{job_id}.verify").write_text('{"protocol": 1, "by_user_id": 1}')
        (staged.jobs / f"{job_id}.rollback").write_text('{"protocol": 1, "by_user_id": 1}')
        _run(staged)
        assert staged.status(job_id)["state"] == "succeeded"
        _run(staged)
        assert staged.status(job_id)["state"] == "rolled_back"
        assert staged.status(job_id)["reason"] == "late"

    def test_a_late_rollback_needs_its_snapshot(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        (staged.jobs / f"{job_id}.verify").write_text('{"protocol": 1, "by_user_id": 1}')
        _run(staged)
        shutil.rmtree(staged.root / "backups")
        (staged.jobs / f"{job_id}.rollback").write_text('{"protocol": 1, "by_user_id": 1}')
        _run(staged)
        assert staged.status(job_id)["state"] == "succeeded"
        assert not (staged.jobs / f"{job_id}.rollback").exists()
        assert "no longer exists" in staged.log(job_id)
        assert len(staged.update_sh_calls()) == 2

    def test_post_update_snapshots_are_pruned_to_the_newest_two(self, fakebin, tmp_path):
        # update.sh prunes only its own pre-update-* snapshots; the named ones
        # it takes for the agent would otherwise pile up a dump per rollback.
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        backups = staged.root / "backups"
        for stamp in ("20260901-000000", "20260902-000000", "20260903-000000"):
            (backups / f"post-update-{stamp}").mkdir(parents=True)
        (backups / "pre-update-20260101-000000").mkdir()
        (staged.jobs / f"{job_id}.rollback").write_text('{"protocol": 1, "by_user_id": 1}')
        _run(staged)
        assert staged.status(job_id)["state"] == "rolled_back"
        kept = sorted(p.name for p in backups.iterdir())
        assert kept == [
            "post-update-20260903-000000",
            "post-update-20260920-120000",
            "pre-update-20260101-000000",
            "pre-update-20260920-100000",
        ], kept

    def test_a_failed_post_update_snapshot_refuses_to_roll_over_window_data(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = _to_window(staged)
        staged.mark("snapshot-fail")
        (staged.jobs / f"{job_id}.rollback").write_text('{"protocol": 1, "by_user_id": 1}')
        _run(staged)
        assert staged.status(job_id)["state"] == "rollback_failed"
        assert staged.status(job_id)["reason"] == "requested"
        assert not any("--rollback" in c for c in staged.update_sh_calls())


class TestFailedUpdates:
    def test_a_failure_before_the_snapshot_changes_nothing(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        staged.mark("fail-before")
        job_id = _request(staged, _package(staged))
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "failed" and status["reason"] == "update_failed_before_snapshot"
        assert status["snapshot"] == "" and status["step"] == "acquire"
        assert staged.flag() is None
        assert fakebin.has_call("up -d celery-beat")
        assert not (staged.spool / "lock").exists()
        assert not any("--rollback" in c for c in staged.update_sh_calls())

    def test_a_failure_after_the_snapshot_rolls_back_without_a_post_snapshot_requirement(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        staged.mark("fail-after")
        staged.mark("snapshot-fail")
        job_id = _request(staged, _package(staged))
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "rolled_back" and status["reason"] == "update_failed"
        assert status["post_snapshot"] == ""
        assert any(
            c.startswith(f"--root {staged.root} --rollback --snapshot pre-update-") for c in staged.update_sh_calls()
        )
        assert staged.flag() is None

    def test_a_gate_failure_after_a_clean_exit_rolls_back(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.fake_dir / "http-code").write_text("503")
        job_id = _request(staged, _package(staged))
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "rollback_failed" or status["state"] == "rolled_back"
        assert status["reason"] == "health_failed"
        assert "did not answer 200" in staged.log(job_id)

    def test_a_version_mismatch_after_a_clean_exit_rolls_back(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        # The stand-in writes the package version as the running one; pin the
        # container's answer to the old version instead.
        fakebin.stub("docker", body=DOCKER_STUB.replace('cat "$FAKE_DIR/running-version"', "echo 0.1.0"))
        job_id = _request(staged, _package(staged))
        _run(staged)
        status = staged.status(job_id)
        assert status["reason"] == "health_failed"
        assert "serving version 0.1.0, expected 0.2.0" in staged.log(job_id)

    def test_a_failed_rollback_leaves_the_flag_and_names_the_need_for_a_shell(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        staged.mark("fail-after")
        staged.mark("rollback-fail")
        job_id = _request(staged, _package(staged))
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "rollback_failed" and status["reason"] == "update_failed"
        assert staged.flag()["phase"] == "rolling_back"
        assert not (staged.spool / "lock").exists()
        assert not fakebin.has_call("up -d celery-beat")
        assert "needs a shell" in staged.log(job_id)

    def test_the_log_is_head_capped(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        staged.mark("chatty")
        job_id = _request(staged, _package(staged))
        _run(staged, EPICURRENTS_UPDATER_LOG_CAP="4000")
        log = staged.log(job_id)
        assert "log truncated at 4000 bytes" in log
        assert len(log.encode()) < 6000
        assert staged.status(job_id)["state"] == "awaiting_verification", "the cap must not stop the :: parsing"


class TestStaleLock:
    def _stale(self, staged: Staged, job_id: str, *, boot="boot-gone") -> None:
        (staged.spool / "lock").write_text(
            json.dumps(
                {"protocol": 1, "pid": 4000000, "boot_id": boot, "job_id": job_id, "since": "2026-09-20T10:00:00Z"}
            )
        )

    def _in_state(self, staged: Staged, state: str, *, snapshot: str = "", log: str = "") -> str:
        job_id = str(uuid.uuid4())
        _request(staged, _package(staged), job_id=job_id)
        status = {
            "protocol": 1,
            "job_id": job_id,
            "state": state,
            "reason": "",
            "step": "build",
            "updated_at": "2026-09-20T10:05:00Z",
            "started_at": "2026-09-20T10:01:00Z",
            "finished_at": None,
            "verify_deadline": None,
            "snapshot": snapshot,
            "post_snapshot": "",
            "agent_version": "1",
            "installed_version_before": "0.1.0",
            "target_version": "0.2.0",
            "running_version": "",
            "retries": 0,
        }
        (staged.jobs / f"{job_id}.status.json").write_text(json.dumps(status))
        (staged.jobs / f"{job_id}.log").write_text(log)
        return job_id

    def test_a_running_job_with_a_snapshot_is_rolled_back(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.root / "backups" / "pre-update-20260920-100000").mkdir(parents=True)
        job_id = self._in_state(
            staged, "running", log="::step=backup\n::snapshot=./backups/pre-update-20260920-100000\n"
        )
        self._stale(staged, job_id)
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "rolled_back" and status["reason"] == "stale"
        assert status["snapshot"] == "pre-update-20260920-100000"
        assert not (staged.spool / "lock").exists()

    def test_a_running_job_without_a_snapshot_is_failed(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = self._in_state(staged, "running", log="::step=snapshot\n")
        self._stale(staged, job_id)
        _run(staged)
        status = staged.status(job_id)
        assert status["state"] == "failed" and status["reason"] == "stale"
        assert not any("--rollback" in c for c in staged.update_sh_calls())
        assert staged.flag() is None and not (staged.spool / "lock").exists()

    def test_an_interrupted_rollback_is_retried_once(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.root / "backups" / "pre-update-20260920-100000").mkdir(parents=True)
        job_id = self._in_state(staged, "rolling_back", snapshot="pre-update-20260920-100000")
        status = staged.status(job_id)
        status["reason"] = "deadline"
        (staged.jobs / f"{job_id}.status.json").write_text(json.dumps(status))
        self._stale(staged, job_id)
        _run(staged)
        after = staged.status(job_id)
        assert after["state"] == "rolled_back" and after["reason"] == "deadline" and after["retries"] == 1

    def test_a_twice_interrupted_rollback_needs_a_shell(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = self._in_state(staged, "rolling_back", snapshot="pre-update-20260920-100000")
        status = staged.status(job_id)
        status["retries"] = 1
        (staged.jobs / f"{job_id}.status.json").write_text(json.dumps(status))
        self._stale(staged, job_id)
        _run(staged)
        assert staged.status(job_id)["state"] == "rollback_failed"
        assert staged.update_sh_calls() == []

    def test_a_live_lock_from_this_boot_is_respected(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        job_id = self._in_state(staged, "running")
        (staged.spool / "lock").write_text(
            json.dumps({"protocol": 1, "pid": os.getpid(), "boot_id": "boot-one", "job_id": job_id})
        )
        result = _run(staged)
        assert result.returncode == 0
        assert staged.status(job_id)["state"] == "running"
        assert (staged.spool / "lock").exists()

    def test_a_lock_naming_no_job_is_removed(self, fakebin, tmp_path):
        staged = _stage(tmp_path, fakebin)
        (staged.spool / "lock").write_text(
            json.dumps({"protocol": 1, "pid": 4000000, "boot_id": "x", "job_id": "nope"})
        )
        _run(staged)
        assert not (staged.spool / "lock").exists()


class TestInstaller:
    def _install(self, fakebin, tmp_path, *args, key=True, as_root=True):
        root = tmp_path / "deploy"
        (root / "updater").mkdir(parents=True, exist_ok=True)
        (root / "docker-compose.yml").write_text("services: {}\n")
        (root / "docker-compose.prod.yml").write_text("services: {}\n")
        (root / "update.sh").write_text("#!/usr/bin/env bash\nUPDATER_SCRIPT_VERSION=2\n")
        if key:
            (root / "RELEASE_KEY.pub").write_text(
                "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAtest\n-----END PUBLIC KEY-----\n"
            )
        for name in (
            "epicurrents-updater.sh",
            "install-updater.sh",
            "epicurrents-updater.service",
            "epicurrents-updater.timer",
            "README.md",
        ):
            shutil.copy(SCRIPTS_DIR / "updater" / name, root / "updater" / name)
        if as_root:
            fakebin.stub("id", body='case "${1:-}" in -u) echo 0 ;; *) echo "uid=0(root)" ;; esac')
        else:
            fakebin.stub("id", body="echo 1000")
        # The real install, minus the ownership flags a non-root test cannot honour.
        fakebin.stub(
            "install",
            body=r"""
n=$#; i=0; skip=0
while [ $i -lt $n ]; do
    a="$1"; shift; i=$((i + 1))
    if [ $skip = 1 ]; then skip=0; continue; fi
    case "$a" in -o|-g) skip=1; continue ;; esac
    set -- "$@" "$a"
done
exec /usr/bin/install "$@"
""",
        )
        if "systemctl" not in fakebin.removed:
            fakebin.stub("systemctl")
        fakebin.stub("openssl", exit_code=1)
        fakebin.stub("rsync")
        etc = tmp_path / "etc"
        lib = tmp_path / "lib"
        state = tmp_path / "state"
        units = tmp_path / "units"
        units.mkdir(exist_ok=True)
        env = {
            "PATH": f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}",
            "HOME": str(tmp_path),
            "LC_ALL": "C",
            "EPICURRENTS_UPDATER_CONFIG_DIR": str(etc),
            "EPICURRENTS_UPDATER_LIB_DIR": str(lib),
            "EPICURRENTS_UPDATER_STATE_DIR": str(state),
            "EPICURRENTS_UPDATER_UNIT_DIR": str(units),
        }
        result = subprocess.run(
            ["bash", str(root / "updater" / "install-updater.sh"), *args],
            check=False,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
        )
        return result, root, etc, lib, units

    def test_installs_everything_disabled_and_enables_the_timer(self, fakebin, tmp_path):
        result, root, etc, lib, units = self._install(fakebin, tmp_path)
        assert result.returncode == 0, result.stderr
        config = (etc / "config").read_text()
        assert f'DEPLOY_ROOT="{root}"' in config and "ENABLED=0" in config and "ALLOW_CHECKOUT=0" in config
        assert (etc / "release.pub").read_text() == (root / "RELEASE_KEY.pub").read_text()
        assert (lib / "update.sh").read_text() == (root / "update.sh").read_text()
        assert os.access(lib / "epicurrents-updater.sh", os.X_OK)
        assert (units / "epicurrents-updater.service").is_file() and (units / "epicurrents-updater.timer").is_file()
        assert str(lib) in (units / "epicurrents-updater.service").read_text()
        assert fakebin.has_call("systemctl daemon-reload")
        assert fakebin.has_call("systemctl enable --now epicurrents-updater.timer")
        for sub in ("update", "update/packages", "update/jobs"):
            assert (root / sub).is_dir()
        assert "ENABLED=1 is" in result.stdout

    def test_enable_and_allow_checkout_flags_edit_the_config(self, fakebin, tmp_path):
        result, _, etc, _, _ = self._install(fakebin, tmp_path, "--enable", "--allow-checkout")
        assert result.returncode == 0, result.stderr
        config = (etc / "config").read_text()
        assert "ENABLED=1" in config and "ALLOW_CHECKOUT=1" in config

    def test_a_rerun_keeps_the_config(self, fakebin, tmp_path):
        result, root, etc, lib, units = self._install(fakebin, tmp_path, "--enable")
        assert result.returncode == 0, result.stderr
        (etc / "config").write_text((etc / "config").read_text() + "HEALTH_TIMEOUT=900\n")
        result = subprocess.run(
            ["bash", str(root / "updater" / "install-updater.sh")],
            check=False,
            cwd=str(root),
            capture_output=True,
            text=True,
            env={
                "PATH": f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}",
                "HOME": str(tmp_path),
                "LC_ALL": "C",
                "EPICURRENTS_UPDATER_CONFIG_DIR": str(etc),
                "EPICURRENTS_UPDATER_LIB_DIR": str(lib),
                "EPICURRENTS_UPDATER_STATE_DIR": str(tmp_path / "state"),
                "EPICURRENTS_UPDATER_UNIT_DIR": str(units),
            },
        )
        assert result.returncode == 0, result.stderr
        assert "HEALTH_TIMEOUT=900" in (etc / "config").read_text() and "ENABLED=1" in (etc / "config").read_text()

    def test_refuses_without_root_a_key_or_systemd(self, fakebin, tmp_path):
        result, *_ = self._install(fakebin, tmp_path, key=False)
        assert result.returncode != 0 and "No release key" in result.stderr
        result, *_ = self._install(fakebin, tmp_path, as_root=False)
        assert result.returncode != 0 and "as root" in result.stderr
        fakebin.remove("systemctl")
        result, *_ = self._install(fakebin, tmp_path)
        assert result.returncode != 0 and "no systemd" in result.stderr


def test_the_agent_and_installer_parse_and_carry_a_version():
    for script in (AGENT, INSTALLER):
        subprocess.run(["bash", "-n", str(script)], check=True)
    body = AGENT.read_text()
    assert "AGENT_VERSION=1" in body
    assert 'OPERATION_UPDATE="platform.update"' in body
