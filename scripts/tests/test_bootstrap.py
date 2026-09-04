"""Dry-run tests for scripts/bootstrap.sh.

Exercises the major branches via the fakebin pattern documented in
``conftest.py``. Each test focuses on one observable: did the script
invoke (or skip) a specific command for the branch under test.
"""

from scripts.tests.conftest import (
    make_env,
    make_env_example,
    run_script,
)

BOOTSTRAP = "bootstrap.sh"


class TestBootstrapShFirstPass:
    """No .env present → install prereqs, write .env, exit before stack-up."""

    def test_writes_env_and_exits(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        # init_env should have been called via docker compose run.
        assert fakebin.has_call("docker compose run")
        assert fakebin.has_call("manage.py init_env")
        # First-pass exits before the borg init / compose up steps.
        assert not fakebin.has_call("borg init")
        assert not fakebin.has_call("compose -f docker-compose.yml")

    def test_pulls_submodules(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("git submodule update --init --recursive")


class TestBootstrapShSecondPass:
    """.env present → frontend build, borg init, stack up."""

    def test_starts_stack_with_prod_overlay(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        # Stack-up uses the prod overlay.
        assert fakebin.has_call("docker-compose.prod.yml")
        assert fakebin.has_call("up -d")

    def test_proxy_overlay_added_when_proxy_domain_is_set(self, fakebin, tmp_path):
        make_env(tmp_path, PROXY_DOMAIN="eeg.example.com")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("docker-compose.proxy.yml")

    def test_proxy_overlay_omitted_when_proxy_domain_is_empty(self, fakebin, tmp_path):
        # The default .env carries no PROXY_DOMAIN; a bare `PROXY_DOMAIN=` line
        # must read as "off" too, or a deployment that terminates TLS elsewhere
        # picks up a caddy container it never asked for and cannot get a
        # certificate for.
        make_env(tmp_path, PROXY_DOMAIN="")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("docker-compose.proxy.yml")
        assert fakebin.has_call("docker-compose.prod.yml")

    def test_no_start_flag_skips_compose_up(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path, args=["--no-start"])
        assert result.returncode == 0, result.stderr
        # Borg init must still run; the compose up step is the gated one.
        assert fakebin.has_call("borg init") or fakebin.has_call("borg info")
        assert not fakebin.has_call("up -d")

    def test_empty_passphrase_skips_borg_init_rather_than_prompting(self, fakebin, tmp_path):
        # .env documents an empty BORG_PASSPHRASE as the way to turn repokey
        # backups off. `borg init --encryption repokey` then asks for one, and
        # `compose run` hands it a TTY to ask on, so without the guard the
        # documented opt-out stalls the bootstrap at a prompt nobody sees.
        make_env(tmp_path, BORG_PASSPHRASE="")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("borg init")

    def test_borg_init_runs_without_a_tty(self, fakebin, tmp_path):
        # -T is the backstop for the same failure from any other cause: a prompt
        # with no terminal fails the step instead of hanging the run.
        make_env(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path, args=["--no-start"])
        assert result.returncode == 0, result.stderr
        borg_calls = [c for c in fakebin.calls() if "--entrypoint borg" in c]
        assert borg_calls, "no borg call was made at all"
        assert all(" -T " in c for c in borg_calls), borg_calls

    def test_dicom_plugin_fetches_ohif_submodule(self, fakebin, tmp_path):
        make_env(tmp_path, EPICURRENTS_PLUGINS="dicom")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        # The conditional submodule fetch references the dicom plugin's OHIF dir.
        assert fakebin.has_call("git submodule update --init --checkout plugins/dicom/ohif-viewer")

    def test_dicom_plugin_detected_among_multiple(self, fakebin, tmp_path):
        # `dicom` as one entry in a comma-separated EPICURRENTS_PLUGINS list
        # must still trigger the OHIF fetch.
        make_env(tmp_path, EPICURRENTS_PLUGINS="other,dicom")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("git submodule update --init --checkout plugins/dicom/ohif-viewer")

    def test_no_dicom_plugin_does_not_fetch_ohif(self, fakebin, tmp_path):
        # An active project is set deliberately: the point is that a project
        # does not pull a plugin's submodule. It needs a repo alongside it now
        # that step 6b refuses to continue with a named-but-absent project.
        make_env(
            tmp_path,
            EPICURRENTS_PROJECT="thing",
            EPICURRENTS_PROJECT_REPO="thing",
            EPICURRENTS_PLUGINS="",
        )
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("plugins/dicom/ohif-viewer")


class TestBootstrapProgressOutput:
    """Checklist rendering modes of scripts/lib/progress.sh."""

    def test_plain_mode_prints_plan_with_interactive_tags(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        # Non-TTY stdout → plain mode: numbered plan up front, interactive
        # steps tagged, then the usual sequential step blocks.
        assert "Steps for this run:" in result.stdout
        assert "Check git (interactive)" in result.stdout
        assert "==> Build frontend bundles" in result.stdout

    def test_fancy_mode_renders_checklist_frames(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"BOOTSTRAP_PROGRESS": "fancy"},
        )
        assert result.returncode == 0, result.stderr
        # Frame repaint = erase-line sequences; steps complete as ✓ lines.
        assert "\033[2K" in result.stdout
        assert "✓" in result.stdout
        # Direct steps print passthrough blocks between frames.
        assert "==> Check git" in result.stdout
        # Captured step output lands in the aggregate log, per-step headers.
        log = (tmp_path / "bootstrap.log").read_text()
        assert "── Build frontend bundles ──" in log
        # The same commands ran as in plain mode.
        assert fakebin.has_call("docker-compose.prod.yml")
        assert fakebin.has_call("up -d")

    def test_fancy_mode_failed_step_dumps_log_tail(self, fakebin, tmp_path):
        make_env(tmp_path)
        fakebin.stub(
            "docker",
            body=r"""
case "$1 $2" in
    "version --format") echo "25.0.0" ;;
    "compose build") echo "boom: simulated build failure"; exit 1 ;;
esac
""",
        )
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            extra_env={"BOOTSTRAP_PROGRESS": "fancy"},
        )
        assert result.returncode != 0
        assert "Build the Python image failed (exit 1)" in result.stderr
        assert "boom: simulated build failure" in result.stderr
        # The pointer to the persisted full output survives the failure.
        assert "bootstrap.log" in result.stderr
        assert "boom: simulated build failure" in (tmp_path / "bootstrap.log").read_text()


class TestEnvDollarGuard:
    """docker compose interpolates .env, including the copy it hands a container
    through env_file, so an unescaped `$` in a value is read as a variable
    reference and replaced with nothing. `smtp$ecret99` arrives as `smtp`.

    init_env no longer generates such a value, but an operator pasting an SMTP
    password or an external credential still can, and the failure is silent on
    both sides. The guard runs on the second pass — the first time bootstrap
    sees .env as the operator left it.
    """

    def test_a_clean_env_is_not_flagged(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        make_env(tmp_path, EMAIL_HOST_PASSWORD="perfectly-ordinary")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "unescaped" not in (result.stdout + result.stderr)

    def test_an_unescaped_dollar_stops_the_run_and_names_the_line(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        make_env(tmp_path, EMAIL_HOST_PASSWORD="smtp$ecret99")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode != 0, "a value compose would truncate must not reach the stack"
        combined = result.stdout + result.stderr
        assert "EMAIL_HOST_PASSWORD" in combined, "the operator has to be told which value"
        assert "$$" in combined, "and how to keep a literal $ if they need one"

    def test_an_escaped_dollar_is_allowed(self, fakebin, tmp_path):
        """$$ is compose's own escape, and the container receives one $. A
        deployment stuck with a credential it does not control needs this."""
        make_env_example(tmp_path)
        make_env(tmp_path, EMAIL_HOST_PASSWORD="smtp$$ecret99")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr

    def test_a_dollar_in_a_comment_is_ignored(self, fakebin, tmp_path):
        """Comments are not interpolated into anything, and flagging them would
        train the operator to ignore the warning."""
        make_env_example(tmp_path)
        env = make_env(tmp_path)
        env.write_text(env.read_text() + "# costs $5 per month\n")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
