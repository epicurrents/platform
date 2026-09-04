"""Tests for borgmatic/emit_runtime_config.py.

The emitter is the runtime-config generator that replaced the heredoc-based
YAML construction in entrypoint.sh. The heredoc was vulnerable to YAML
injection via newline-bearing env values; these tests pin the safe shape so
a regression toward textual substitution can't slip through.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

_EMITTER_PATH = Path(__file__).resolve().parents[1] / "emit_runtime_config.py"


def _load_emitter():
    spec = importlib.util.spec_from_file_location("borgmatic_emit_runtime_config", _EMITTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_emitter(tmp_path, monkeypatch):
    emitter = _load_emitter()

    def _run(env: dict | None = None):
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        for var in (
            "BORG_REMOTE_REPO",
            "BORG_MONITOR_URL",
            "BORG_MONITOR_SEND_LOGS",
            "BACKUP_KEEP_DAILY",
            "BACKUP_KEEP_WEEKLY",
            "BACKUP_KEEP_MONTHLY",
            "BORG_ARCHIVE_PREFIX",
            "BACKUP_LOCAL_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        emitter.main()
        with open(tmp_path / "config.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)

    return _run


class TestEmitRuntimeConfig:
    def test_default_config_shape(self, run_emitter):
        config = run_emitter()
        assert config["source_directories"] == [
            "/data/recordings",
            "/data/media/uploads",
        ]
        assert config["repositories"] == [{"path": "/backup", "label": "local"}]
        assert config["postgresql_databases"] == [{"name": "all", "hostname": "db"}]
        assert config["keep_daily"] == 7
        assert config["keep_weekly"] == 4
        assert config["keep_monthly"] == 6

    def test_postgres_section_omits_credentials(self, run_emitter):
        """PGUSER / PGPASSWORD reach pg_dump via libpq env vars — the YAML
        must never carry credentials so a YAML-injection or config-leak
        cannot escalate to a credential disclosure."""
        config = run_emitter()
        pg = config["postgresql_databases"][0]
        assert "username" not in pg
        assert "password" not in pg

    def test_remote_repo_appended_when_set(self, run_emitter):
        config = run_emitter({"BORG_REMOTE_REPO": "user@host:/srv/borg"})
        assert config["repositories"] == [
            {"path": "/backup", "label": "local"},
            {"path": "user@host:/srv/borg", "label": "remote"},
        ]

    def test_remote_repo_with_newline_aborts(self, tmp_path, monkeypatch):
        """A newline in BORG_REMOTE_REPO is the canonical YAML-injection
        payload. The emitter must abort before writing any file."""
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        monkeypatch.setenv(
            "BORG_REMOTE_REPO",
            "user@host:/srv/borg\nhooks:\n  before_backup:\n    - rm -rf /data",
        )
        with pytest.raises(SystemExit) as exc:
            emitter.main()
        assert exc.value.code == 1
        assert not (tmp_path / "config.yaml").exists()

    def test_carriage_return_in_repo_also_aborts(self, tmp_path, monkeypatch):
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        monkeypatch.setenv("BORG_REMOTE_REPO", "user@host:/srv/borg\rinjected")
        with pytest.raises(SystemExit) as exc:
            emitter.main()
        assert exc.value.code == 1

    def test_non_integer_retention_aborts(self, tmp_path, monkeypatch):
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        monkeypatch.setenv("BACKUP_KEEP_DAILY", "abc; rm -rf /")
        with pytest.raises(SystemExit) as exc:
            emitter.main()
        assert exc.value.code == 1

    def test_custom_retention_overrides_defaults(self, run_emitter):
        config = run_emitter(
            {
                "BACKUP_KEEP_DAILY": "14",
                "BACKUP_KEEP_WEEKLY": "8",
                "BACKUP_KEEP_MONTHLY": "12",
            }
        )
        assert config["keep_daily"] == 14
        assert config["keep_weekly"] == 8
        assert config["keep_monthly"] == 12

    def test_stale_remote_yaml_is_cleaned(self, tmp_path, monkeypatch):
        """Removing BORG_REMOTE_REPO between runs must not leave a stale
        remote.yaml behind. The emitter wipes the active dir of yaml files
        before regenerating."""
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        stale = tmp_path / "remote.yaml"
        stale.write_text("repositories:\n  - path: leftover\n")
        monkeypatch.delenv("BORG_REMOTE_REPO", raising=False)
        emitter.main()
        assert not stale.exists()
        assert (tmp_path / "config.yaml").exists()

    def test_yaml_injection_payload_in_passphrase_does_not_reach_yaml(self, tmp_path, monkeypatch):
        """End-to-end: even if a passphrase carrying YAML-injection bytes
        were somehow exposed to the emitter, it wouldn't be written to the
        config because the emitter doesn't read BORG_PASSPHRASE at all. This
        test pins that contract."""
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        monkeypatch.setenv(
            "BORG_PASSPHRASE",
            "secret\nhooks:\n  before_backup:\n    - touch /tmp/pwned\n",
        )
        emitter.main()
        config_text = (tmp_path / "config.yaml").read_text()
        assert "Starting Epicurrents backup" in config_text  # the echo hook
        assert "touch /tmp/pwned" not in config_text
        assert "secret" not in config_text


def _error_hook(config):
    """The single ``after: error`` entry from the emitted command hooks."""
    return next(hook for hook in config["commands"] if hook.get("after") == "error")


class TestFailureVisibility:
    """A backup that has been failing for weeks is indistinguishable from one
    that has been working until a restore is attempted. Two independent reports
    close that: a log line from borgmatic's error hook, and an external monitor
    that also alerts on silence.
    """

    def test_error_hook_emits_the_failure_token(self, run_emitter):
        command = _error_hook(run_emitter())["run"][0]
        assert _load_emitter().FAILURE_TOKEN in command

    def test_error_hook_names_the_repository_and_the_error(self, run_emitter):
        """Without both, the alert says a backup failed but not which repository
        or why, and the operator is back to reading the whole container log."""
        command = _error_hook(run_emitter())["run"][0]
        assert "{repository_label}" in command
        assert "{error}" in command

    def test_error_hook_writes_to_stderr(self, run_emitter):
        assert _error_hook(run_emitter())["run"][0].endswith(">&2")

    def test_entrypoint_uses_the_same_failure_token(self):
        """The entrypoint catches the case the hook cannot — borgmatic failing
        before it runs any hook at all (unparseable config, unreachable remote).
        One token has to cover both or an alert rule catches only half of them.
        """
        entrypoint = (_EMITTER_PATH.parent / "entrypoint.sh").read_text()
        assert _load_emitter().FAILURE_TOKEN in entrypoint

    def test_monitoring_absent_by_default(self, run_emitter):
        assert "healthchecks" not in run_emitter()

    def test_monitoring_configured_when_url_set(self, run_emitter):
        config = run_emitter({"BORG_MONITOR_URL": "https://hc-ping.com/deadbeef"})
        assert config["healthchecks"]["ping_url"] == "https://hc-ping.com/deadbeef"

    def test_logs_are_not_shipped_by_default(self, run_emitter):
        """The monitor is a third-party destination, so the default ping carries
        the fact of a failure and no content. Shipping the run's log tail is a
        processor flow and has to be opted into."""
        config = run_emitter({"BORG_MONITOR_URL": "https://hc-ping.com/deadbeef"})
        assert config["healthchecks"]["send_logs"] is False

    def test_logs_shipped_when_opted_in(self, run_emitter):
        config = run_emitter({"BORG_MONITOR_URL": "https://hc-ping.com/deadbeef", "BORG_MONITOR_SEND_LOGS": "true"})
        assert config["healthchecks"]["send_logs"] is True

    def test_opt_in_requires_an_affirmative_value(self, run_emitter):
        """An unset, empty or "false" value must not enable the flow — a
        privacy-relevant default that flips on a typo is not a default."""
        for value in ("", "false", "no", "0", "maybe"):
            config = run_emitter({"BORG_MONITOR_URL": "https://hc-ping.com/deadbeef", "BORG_MONITOR_SEND_LOGS": value})
            assert config["healthchecks"]["send_logs"] is False, value

    def test_monitor_url_with_newline_aborts(self, tmp_path, monkeypatch):
        """Same YAML-injection guard as the repo path; the ping URL is equally
        operator-supplied and equally reaches the config."""
        emitter = _load_emitter()
        monkeypatch.setenv("BORGMATIC_ACTIVE_DIR", str(tmp_path))
        monkeypatch.setenv(
            "BORG_MONITOR_URL", "https://hc-ping.com/x\ncommands:\n  - after: error\n    run: [rm -rf /data]"
        )
        with pytest.raises(SystemExit) as exc:
            emitter.main()
        assert exc.value.code == 1
        assert not (tmp_path / "config.yaml").exists()

    def test_monitor_url_is_not_printed(self, run_emitter, capsys):
        """The ping URL is a bearer credential — anyone holding it can report
        the backups as healthy — so it must not land in the container log."""
        run_emitter({"BORG_MONITOR_URL": "https://hc-ping.com/deadbeef"})
        captured = capsys.readouterr()
        assert "deadbeef" not in captured.out + captured.err

    def test_warns_when_no_monitor_is_configured(self, run_emitter, capsys):
        run_emitter()
        assert "BORG_MONITOR_URL is unset" in capsys.readouterr().err

    def test_warns_when_backups_are_local_only(self, run_emitter, capsys):
        run_emitter()
        assert "BORG_REMOTE_REPO is unset" in capsys.readouterr().err

    def test_no_local_only_warning_with_a_remote(self, run_emitter, capsys):
        run_emitter({"BORG_REMOTE_REPO": "user@host:/srv/borg"})
        assert "BORG_REMOTE_REPO is unset" not in capsys.readouterr().err


class TestArchiveNameFormat:
    """The archive name is what retention matches on, so it must not vary per run.

    Borgmatic derives the ``--glob-archives`` it hands ``borg prune`` from
    ``archive_name_format``. Its default embeds ``{hostname}``, which inside a
    container is the container ID and therefore changes on every recreate — so
    prune matches only what the current container wrote and every older archive
    is kept for ever, with retention configured, running, and reporting success.
    Found on a live deployment whose archives were named after three different
    container IDs.
    """

    def test_format_is_set_and_free_of_hostname(self, run_emitter):
        config = run_emitter()
        fmt = config["archive_name_format"]
        assert "{hostname}" not in fmt
        assert "{fqdn}" not in fmt
        assert fmt.startswith("epicurrents-")

    def test_format_is_stable_across_runs(self, run_emitter):
        assert run_emitter()["archive_name_format"] == run_emitter()["archive_name_format"]

    def test_prefix_is_overridable(self, run_emitter):
        config = run_emitter({"BORG_ARCHIVE_PREFIX": "site-a"})
        assert config["archive_name_format"].startswith("site-a-")

    def test_empty_prefix_falls_back_to_the_default(self, run_emitter):
        assert run_emitter({"BORG_ARCHIVE_PREFIX": ""})["archive_name_format"].startswith("epicurrents-")

    @pytest.mark.parametrize(
        "bad",
        [
            "with space",
            "star*",
            "question?",
            "brack[et]",
            "brace{hostname}",
            "-leading-hyphen",
            "sla/sh",
            "new\nline",
        ],
    )
    def test_a_prefix_that_would_widen_the_prune_glob_is_refused(self, run_emitter, bad):
        # A glob metacharacter here does not corrupt the archive name so much as
        # the match: `borg prune --glob-archives 'star*-*'` is a wider set than
        # the operator asked to prune, and pruning is the irreversible direction.
        with pytest.raises(SystemExit) as exc:
            run_emitter({"BORG_ARCHIVE_PREFIX": bad})
        assert exc.value.code != 0


class TestRepositoryTiers:
    """The local and remote repositories are selected independently.

    The local one restores fast and dies with the host; the remote one is
    append-only and survives it. A deployment may want either or both — but
    never neither, which is the case this fails closed on.
    """

    def test_local_only_by_default(self, run_emitter):
        labels = [r["label"] for r in run_emitter()["repositories"]]
        assert labels == ["local"]

    def test_both_tiers_when_a_remote_is_set(self, run_emitter):
        config = run_emitter({"BORG_REMOTE_REPO": "ssh://borg@host/srv/borg/repo"})
        assert [r["label"] for r in config["repositories"]] == ["local", "remote"]

    def test_remote_only_drops_the_local_repository(self, run_emitter):
        config = run_emitter({"BORG_REMOTE_REPO": "ssh://borg@host/srv/borg/repo", "BACKUP_LOCAL_ENABLED": "false"})
        assert [r["label"] for r in config["repositories"]] == ["remote"]
        assert all(r["path"] != "/backup" for r in config["repositories"])

    def test_no_repository_at_all_is_refused(self, run_emitter):
        # borgmatic accepts an empty repository list and reports a clean run
        # against nothing, so the deployment would look backed up while no
        # archive existed anywhere. An error here is the lesser outcome.
        with pytest.raises(SystemExit) as exc:
            run_emitter({"BACKUP_LOCAL_ENABLED": "false"})
        assert exc.value.code != 0

    @pytest.mark.parametrize("value", ["true", "TRUE", "yes", "on", "1"])
    def test_affirmative_spellings_keep_the_local_tier(self, run_emitter, value):
        assert run_emitter({"BACKUP_LOCAL_ENABLED": value})["repositories"][0]["label"] == "local"

    @pytest.mark.parametrize("value", ["false", "FALSE", "no", "off", "0"])
    def test_negative_spellings_drop_it(self, run_emitter, value):
        config = run_emitter({"BACKUP_LOCAL_ENABLED": value, "BORG_REMOTE_REPO": "ssh://borg@host/srv/borg/repo"})
        assert [r["label"] for r in config["repositories"]] == ["remote"]

    @pytest.mark.parametrize("value", ["flase", "maybe", "disabled", "y", "n"])
    def test_a_value_outside_the_vocabulary_is_refused_not_defaulted(self, run_emitter, value):
        # Defaulting a typo back to "keep the local tier" gives an operator who
        # believes they turned it off a deployment that did not, and nothing
        # about the running system says which of the two happened.
        with pytest.raises(SystemExit) as exc:
            run_emitter({"BACKUP_LOCAL_ENABLED": value, "BORG_REMOTE_REPO": "ssh://borg@host/srv/repo"})
        assert exc.value.code != 0


class TestComposeWiring:
    """Every variable the emitter reads must actually reach the borg container.

    The borg service takes an explicit ``environment:`` list rather than an
    ``env_file``, so a variable added to the emitter and to .env.example is
    invisible inside the container until it is also named in compose. The
    emitter then silently uses its default while .env says otherwise, which is
    indistinguishable from the feature not working. Caught exactly that way:
    BACKUP_LOCAL_ENABLED was read, documented, and never passed.
    """

    # A test hook with a default, set by the fixture rather than by an operator.
    NOT_OPERATOR_SETTABLE = {"BORGMATIC_ACTIVE_DIR"}

    def _borg_service_block(self):
        compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
        start = compose.index("\n  borg:\n")
        return compose[start : compose.index("\n  borg-restore:")]

    def test_every_variable_the_emitter_reads_is_passed_to_the_container(self):
        source = _EMITTER_PATH.read_text()
        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', source)) - self.NOT_OPERATOR_SETTABLE
        assert read, "no environment reads found; the parse is wrong, not the compose file"
        block = self._borg_service_block()
        missing = sorted(name for name in read if f"- {name}=" not in block)
        assert not missing, f"read by the emitter but never passed to the borg service: {missing}"
