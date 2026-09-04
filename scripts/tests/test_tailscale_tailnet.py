"""Dry-run tests for the two tailnet scripts and the bootstrap.sh tailnet flags.

Uses the fakebin pattern from ``conftest.py``. The load-bearing property shared by
both scripts is that the single-use auth key never reaches .env — it is passed
only in the process environment of the call that consumes it, which the fakebin
stubs log (args only, never env), so the assertions check .env contents directly.

The second property is that the two scripts stay distinguishable. ``serve`` runs
a userspace container and touches nothing on the host; ``join`` installs the host
package and is the only one that gives containers a route out. A change that
quietly made either do the other's job would leave a deployment either unable to
reach an evidence host or holding a host install nobody asked for, and in both
cases the first symptom arrives days later as an absent-log alert.
"""

import shutil

from scripts.tests.conftest import SCRIPTS_DIR, make_env, run_script

SERVE = "tailscale-serve.sh"
JOIN = "tailscale-join.sh"
BOOTSTRAP = "bootstrap.sh"
FAKE_KEY = "tskey-abc123SECRET"


def _env_text(tmp_path):
    return (tmp_path / ".env").read_text()


def _stage_tailnet_scripts(tmp_path):
    """Copy both tailnet scripts alongside bootstrap.sh so bootstrap can invoke them."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name in (SERVE, JOIN):
        dst = scripts_dir / name
        shutil.copy(SCRIPTS_DIR / name, dst)
        dst.chmod(0o755)


def _tailscale_stub(fakebin, *, joined: bool, stateful_filtering_flag: bool = True) -> None:
    """Install a ``tailscale`` stub reporting either a joined or a logged-out node.

    ``status`` is the join test in the script, so its exit code is what selects the
    branch: 0 means already on a tailnet (reconcile the name, never spend the key),
    non-zero means log in. ``up --help`` is how the script decides whether this
    Tailscale understands ``--stateful-filtering``, so the stub can present an
    older version by omitting the flag from that text.
    """
    status_exit = "0" if joined else "1"
    help_text = "--accept-dns --ssh --hostname"
    if stateful_filtering_flag:
        help_text += " --stateful-filtering"
    fakebin.stub(
        "tailscale",
        body=f"""
case "$1 ${{2:-}}" in
    "up --help") echo "{help_text}"; exit 0 ;;
esac
case "$1" in
    version) echo "1.102.3" ;;
    status) exit {status_exit} ;;
    ip) echo "100.64.0.1" ;;
esac
exit 0
""",
    )


class TestTailscaleServe:
    def test_writes_hostname_and_starts_service(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(
            SERVE,
            fakebin,
            cwd=tmp_path,
            args=["--authkey", FAKE_KEY, "--hostname", "mybox"],
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("compose --profile tailnet up -d tailscale")
        assert "TS_HOSTNAME=mybox" in _env_text(tmp_path)

    def test_authkey_never_written_to_env(self, fakebin, tmp_path):
        make_env(tmp_path)
        run_script(SERVE, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY, "--hostname", "mybox"])
        env = _env_text(tmp_path)
        assert FAKE_KEY not in env
        assert "TS_AUTHKEY" not in env

    def test_authkey_from_env_var(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(
            SERVE,
            fakebin,
            cwd=tmp_path,
            args=["--hostname", "mybox"],
            extra_env={"TS_AUTHKEY": FAKE_KEY},
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("compose --profile tailnet up -d tailscale")
        assert FAKE_KEY not in _env_text(tmp_path)

    def test_missing_authkey_fails(self, fakebin, tmp_path):
        make_env(tmp_path)
        result = run_script(SERVE, fakebin, cwd=tmp_path, args=["--hostname", "mybox"])
        assert result.returncode != 0
        assert not fakebin.has_call("up -d tailscale")

    def test_missing_env_fails(self, fakebin, tmp_path):
        # No make_env — the deployment isn't set up.
        result = run_script(SERVE, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        assert result.returncode != 0
        assert not fakebin.has_call("up -d tailscale")

    def test_hostname_defaults_from_env_without_duplicating(self, fakebin, tmp_path):
        make_env(tmp_path, TS_HOSTNAME="existinghost")
        result = run_script(SERVE, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        assert result.returncode == 0, result.stderr
        env = _env_text(tmp_path)
        assert "TS_HOSTNAME=existinghost" in env
        assert env.count("TS_HOSTNAME=") == 1

    def test_never_installs_on_the_host(self, fakebin, tmp_path):
        # The whole reason to choose serve over join. An install creeping in here
        # would make the two modes the same thing with different names.
        make_env(tmp_path)
        run_script(SERVE, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        assert not fakebin.has_call("tailscale.com/install.sh")
        assert not fakebin.has_call("tailscale up --authkey")


class TestTailscaleJoin:
    def test_attempts_the_documented_install_when_absent(self, fakebin, tmp_path):
        make_env(tmp_path)
        fakebin.remove("tailscale")
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        # The stub is gone, so the post-install `command -v tailscale` check fails
        # and the script dies — which is the assertion: an install that produced
        # no binary must stop rather than continue into `tailscale up`.
        assert fakebin.has_call("tailscale.com/install.sh")
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale up --authkey")

    def test_joins_with_stateful_filtering_disabled(self, fakebin, tmp_path):
        # Containers NAT out through the host, so their replies look unsolicited
        # and a stateful-filtering node drops them. Losing this flag breaks the
        # log shipper while everything looks fine from the host itself.
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY, "--hostname", "mybox"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("--stateful-filtering=false")
        assert fakebin.has_call("--accept-dns=true")
        assert fakebin.has_call("--ssh=false")
        assert fakebin.has_call("--hostname=mybox")

    def test_older_tailscale_joins_without_the_flag_and_says_so(self, fakebin, tmp_path):
        # Capability is probed rather than attempted-and-retried: a failed `up`
        # cannot distinguish an unknown flag from a spent key, and retrying would
        # spend a second one. The join must still happen, and the degradation must
        # be stated — a container that cannot reach the tailnet is the symptom.
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False, stateful_filtering_flag=False)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("tailscale up --authkey")
        assert not fakebin.has_call("--stateful-filtering")
        assert "Containers on this host may not be able to reach the tailnet" in result.stdout
        # One attempt, not an attempt plus a fallback.
        assert len([c for c in fakebin.calls() if c.startswith("tailscale up --authkey")]) == 1

    def test_authkey_never_written_to_env(self, fakebin, tmp_path):
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        env = _env_text(tmp_path)
        assert FAKE_KEY not in env
        assert "TS_AUTHKEY" not in env

    def test_authkey_from_env_var(self, fakebin, tmp_path):
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        result = run_script(
            JOIN, fakebin, cwd=tmp_path, args=["--hostname", "mybox"], extra_env={"TS_AUTHKEY": FAKE_KEY}
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("tailscale up --authkey")
        assert FAKE_KEY not in _env_text(tmp_path)

    def test_already_joined_does_not_spend_the_key(self, fakebin, tmp_path):
        # Auth keys are single-use by default, so a re-run that logged in again
        # would fail on a host that is already fine. Idempotency here is the
        # difference between a script you can put in a runbook and one you cannot.
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=True)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY, "--hostname", "mybox"])
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("tailscale up --authkey")

    def test_already_joined_needs_no_key(self, fakebin, tmp_path):
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=True)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--hostname", "mybox"])
        assert result.returncode == 0, result.stderr

    def test_already_joined_reconciles_the_hostname(self, fakebin, tmp_path):
        make_env(tmp_path, TS_HOSTNAME="oldname")
        _tailscale_stub(fakebin, joined=True)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--hostname", "newname"])
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("tailscale set --hostname=newname")
        env = _env_text(tmp_path)
        assert "TS_HOSTNAME=newname" in env
        assert env.count("TS_HOSTNAME=") == 1

    def test_missing_authkey_when_not_joined_fails(self, fakebin, tmp_path):
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        result = run_script(JOIN, fakebin, cwd=tmp_path)
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale up --authkey")

    def test_missing_env_fails(self, fakebin, tmp_path):
        _tailscale_stub(fakebin, joined=False)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY])
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale up --authkey")

    def test_no_install_refuses_rather_than_installing(self, fakebin, tmp_path):
        make_env(tmp_path)
        fakebin.remove("tailscale")
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY, "--no-install"])
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale.com/install.sh")

    def test_a_hostname_that_would_corrupt_env_is_refused_before_writing(self, fakebin, tmp_path):
        # The name lands in a sed replacement, where a `|` ends the expression and
        # the rest becomes flags. Validated first, so a bad name costs an error
        # rather than an .env whose next-read setting has silently changed.
        make_env(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        before = _env_text(tmp_path)
        result = run_script(JOIN, fakebin, cwd=tmp_path, args=["--authkey", FAKE_KEY, "--hostname", "a|b/g"])
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale up --authkey")
        assert _env_text(tmp_path) == before


class TestBootstrapTailscale:
    def test_defaults_to_joining_the_host(self, fakebin, tmp_path):
        # The default changed from the serve container to a host join, because a
        # bootstrapped server that cannot route out cannot ship its logs anywhere.
        make_env(tmp_path)
        _stage_tailnet_scripts(tmp_path)
        _tailscale_stub(fakebin, joined=False)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=["--tailscale-authkey", FAKE_KEY, "--tailscale-hostname", "mybox"],
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("tailscale up --authkey")
        assert not fakebin.has_call("--profile tailnet")
        env = _env_text(tmp_path)
        assert "TS_HOSTNAME=mybox" in env
        assert FAKE_KEY not in env

    def test_serve_mode_runs_the_container(self, fakebin, tmp_path):
        make_env(tmp_path)
        _stage_tailnet_scripts(tmp_path)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=[
                "--tailscale-authkey",
                FAKE_KEY,
                "--tailscale-hostname",
                "mybox",
                "--tailscale-mode",
                "serve",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert fakebin.has_call("compose --profile tailnet up -d tailscale")
        assert FAKE_KEY not in _env_text(tmp_path)

    def test_unknown_mode_is_rejected(self, fakebin, tmp_path):
        make_env(tmp_path)
        _stage_tailnet_scripts(tmp_path)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=["--tailscale-authkey", FAKE_KEY, "--tailscale-mode", "sevre"],
        )
        assert result.returncode != 0
        assert not fakebin.has_call("tailscale up --authkey")
        assert not fakebin.has_call("--profile tailnet")

    def test_no_tailscale_without_flags(self, fakebin, tmp_path):
        make_env(tmp_path)
        _stage_tailnet_scripts(tmp_path)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("--profile tailnet")
        assert not fakebin.has_call("tailscale up --authkey")

    def test_no_start_skips_the_tailnet_step(self, fakebin, tmp_path):
        make_env(tmp_path)
        _stage_tailnet_scripts(tmp_path)
        result = run_script(
            BOOTSTRAP,
            fakebin,
            cwd=tmp_path,
            args=["--no-start", "--tailscale-authkey", FAKE_KEY],
        )
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("up -d tailscale")
        assert not fakebin.has_call("tailscale up --authkey")
