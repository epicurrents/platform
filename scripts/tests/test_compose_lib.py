"""Tests for scripts/lib/compose.sh, and the pin that holds the copies of its logic to it.

The helper is the one statement of how a host script detects Docker Engine or Podman and spells its compose command.
The operator scripts that live only in a checkout source it. update.sh, the updater agent and the start.sh a package
carries cannot: each runs as a single file with no ``lib/`` beside it, and a release is applied by the previous
release's update.sh. Those three keep a copy of the detection, and ``TestPinnedCopies`` lifts each copy out of its
script and runs it beside the helper for every runtime case, so a change to one that is not made to the others fails
here rather than on a Podman host. See ``conftest.py`` for the fakebin pattern.
"""

import subprocess

import pytest

from scripts.tests.conftest import SCRIPTS_DIR, system_path

HELPER = SCRIPTS_DIR / "lib" / "compose.sh"

#: The scripts that source the helper. Each lives only in a checkout, beside scripts/lib/.
SOURCING_SCRIPTS = ("switch_project.sh", "enable_plugin.sh", "disable_plugin.sh", "rebuild-frontend.sh")

#: The scripts that run as a single file somewhere, a package's root or the agent's root-owned directory, and so
#: must never come to depend on a file beside them.
SINGLE_FILE_SCRIPTS = (
    "update.sh",
    "updater/epicurrents-updater.sh",
    "updater/install-updater.sh",
    "tailscale-join.sh",
    "tailscale-serve.sh",
    "make-bootstrap-fixture.sh",
)

PODMAN_AS_DOCKER = '[ "$1" = "--version" ] && echo "podman version 5.8.2"'

RUNTIME_CASES = ("docker", "podman", "podman-docker", "none")

#: Printed after the code under test: the runtime name, then one line per word of COMPOSE. The array expansion is
#: guarded because bash 3.2 treats an empty array as unset under ``set -u``.
REPORT = """
printf 'runtime=%s\\n' "${CONTAINER_RUNTIME-<unset>}"
for word in ${COMPOSE[@]+"${COMPOSE[@]}"}; do printf 'word=%s\\n' "$word"; done
"""


def _set_runtime(fakebin, case, *, root=False):
    """Arrange the fake host: which of docker and podman answer, and whether the caller is root."""
    if case == "podman":
        fakebin.remove("docker")
    elif case == "podman-docker":
        fakebin.stub("docker", body=PODMAN_AS_DOCKER)
    elif case == "none":
        fakebin.remove("docker")
        fakebin.remove("podman")
    if root:
        fakebin.stub("id", body="echo 0")


def _bash(fakebin, cwd, code):
    """Run ``code`` under ``set -euo pipefail`` with the fake binaries leading PATH."""
    script = cwd / "snippet.sh"
    script.write_text("set -euo pipefail\n" + code)
    return subprocess.run(
        ["bash", str(script)],
        check=False,
        cwd=cwd,
        env={"PATH": f"{fakebin.path}:{system_path(fakebin.path, fakebin.removed)}", "LC_ALL": "C"},
        capture_output=True,
        text=True,
    )


def _settled(fakebin, cwd, code):
    """Run ``code`` and return what it left behind: ``(runtime, compose words)``."""
    result = _bash(fakebin, cwd, code + REPORT)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    runtime = next(line for line in lines if line.startswith("runtime=")).split("=", 1)[1]
    return runtime, [line.split("=", 1)[1] for line in lines if line.startswith("word=")]


def _helper(*calls):
    return f'. "{HELPER}"\n' + "\n".join(calls) + "\n"


def _slice(path, start, end):
    """Return the lines of ``path`` from the first that starts with ``start`` through the next equal to ``end``."""
    lines = path.read_text().splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i in range(first, len(lines)) if lines[i] == end)
    return "\n".join(lines[first : last + 1]) + "\n"


class TestDetectRuntime:
    def test_docker_engine_gives_docker_compose(self, fakebin, tmp_path):
        _set_runtime(fakebin, "docker")
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == ("docker", ["docker", "compose"])

    def test_docker_wins_when_both_are_installed(self, fakebin, tmp_path):
        # The shared fixture stubs both, which is the host this case describes.
        runtime, _ = _settled(fakebin, tmp_path, _helper("compose_detect_runtime"))
        assert runtime == "docker"

    def test_podman_alone_gives_rootful_podman_through_sudo(self, fakebin, tmp_path):
        _set_runtime(fakebin, "podman")
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == (
            "podman",
            ["sudo", "-E", "podman", "compose"],
        )

    def test_a_docker_that_is_podman_gives_the_podman_form(self, fakebin, tmp_path):
        _set_runtime(fakebin, "podman-docker")
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == (
            "podman",
            ["sudo", "-E", "podman", "compose"],
        )

    def test_a_root_caller_drives_podman_without_sudo(self, fakebin, tmp_path):
        _set_runtime(fakebin, "podman", root=True)
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == ("podman", ["podman", "compose"])

    def test_a_root_caller_changes_nothing_for_docker(self, fakebin, tmp_path):
        _set_runtime(fakebin, "docker", root=True)
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == ("docker", ["docker", "compose"])

    def test_no_runtime_leaves_the_name_empty_and_falls_back_to_docker(self, fakebin, tmp_path):
        _set_runtime(fakebin, "none")
        assert _settled(fakebin, tmp_path, _helper("compose_detect_runtime")) == ("", ["docker", "compose"])

    def test_a_docker_that_is_podman_with_no_podman_binary_is_no_runtime(self, fakebin, tmp_path):
        fakebin.stub("docker", body=PODMAN_AS_DOCKER)
        fakebin.remove("podman")
        runtime, _ = _settled(fakebin, tmp_path, _helper("compose_detect_runtime"))
        assert runtime == ""

    def test_detection_replaces_an_earlier_compose(self, fakebin, tmp_path):
        _set_runtime(fakebin, "docker")
        code = _helper("COMPOSE=(stale -f old.yml)", "compose_detect_runtime")
        assert _settled(fakebin, tmp_path, code)[1] == ["docker", "compose"]


class TestSourcing:
    def test_sourcing_runs_nothing_and_sets_nothing(self, fakebin, tmp_path):
        runtime, words = _settled(fakebin, tmp_path, _helper())
        assert (runtime, words) == ("<unset>", [])
        assert not fakebin.calls()

    def test_sourcing_prints_nothing(self, fakebin, tmp_path):
        result = _bash(fakebin, tmp_path, _helper())
        assert result.returncode == 0, result.stderr
        assert result.stdout == "" and result.stderr == ""

    def test_the_helper_carries_no_bash_4_syntax(self):
        # macOS ships bash 3.2: no associative arrays, mapfile, case-modifying expansions or `&>>`.
        code = "\n".join(line for line in HELPER.read_text().splitlines() if not line.lstrip().startswith("#"))
        for construct in ("declare -A", "local -A", "mapfile", "readarray", "${COMPOSE,,", "^^}", "&>>", "local -n"):
            assert construct not in code


class TestRequireRuntime:
    @pytest.mark.parametrize("case", ["docker", "podman", "podman-docker"])
    def test_a_found_runtime_passes_silently(self, fakebin, tmp_path, case):
        _set_runtime(fakebin, case)
        result = _bash(fakebin, tmp_path, _helper("compose_detect_runtime", "compose_require_runtime", "echo passed"))
        assert result.returncode == 0, result.stderr
        assert result.stdout == "passed\n" and result.stderr == ""

    def test_no_runtime_is_refused_in_the_words_update_sh_uses(self, fakebin, tmp_path):
        _set_runtime(fakebin, "none")
        result = _bash(
            fakebin, tmp_path, _helper("compose_detect_runtime", "compose_require_runtime || exit 1", "echo passed")
        )
        assert result.returncode == 1
        assert "passed" not in result.stdout
        message = "No container runtime found — this needs Docker Engine or Podman, either one with Compose v2."
        assert message in result.stderr
        assert message in (SCRIPTS_DIR / "update.sh").read_text()

    def test_it_refuses_before_detection_has_run(self, fakebin, tmp_path):
        result = _bash(fakebin, tmp_path, _helper("compose_require_runtime || exit 1"))
        assert result.returncode == 1


class TestProductionOverlays:
    BASE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml"]

    def _overlaid(self, fakebin, tmp_path, call="compose_add_production_overlays"):
        return _settled(fakebin, tmp_path, _helper("compose_detect_runtime", call))[1]

    def test_no_env_file_gives_base_and_production(self, fakebin, tmp_path):
        assert self._overlaid(fakebin, tmp_path) == self.BASE

    def test_a_proxy_domain_adds_the_proxy_overlay_last(self, fakebin, tmp_path):
        (tmp_path / ".env").write_text("OTHER=1\nPROXY_DOMAIN=eeg.example.org\n")
        assert self._overlaid(fakebin, tmp_path) == [*self.BASE, "-f", "docker-compose.proxy.yml"]

    @pytest.mark.parametrize("line", ["PROXY_DOMAIN=", "PROXY_DOMAIN= ", "# PROXY_DOMAIN=eeg.example.org", "OTHER=1"])
    def test_a_blank_or_absent_proxy_domain_adds_nothing(self, fakebin, tmp_path, line):
        (tmp_path / ".env").write_text(line + "\n")
        assert self._overlaid(fakebin, tmp_path) == self.BASE

    def test_the_env_file_can_be_named(self, fakebin, tmp_path):
        (tmp_path / "deployment").mkdir()
        (tmp_path / "deployment" / ".env").write_text("PROXY_DOMAIN=eeg.example.org\n")
        words = self._overlaid(fakebin, tmp_path, 'compose_add_production_overlays "deployment/.env"')
        assert words == [*self.BASE, "-f", "docker-compose.proxy.yml"]


class TestCommandLine:
    def test_plain_words_print_as_typed(self, fakebin, tmp_path):
        code = _helper("COMPOSE=(sudo -E podman compose -f /opt/epicurrents/docker-compose.yml)", "compose_command_line")
        result = _bash(fakebin, tmp_path, code)
        assert result.stdout == "sudo -E podman compose -f /opt/epicurrents/docker-compose.yml"

    def test_a_word_with_a_space_survives_a_paste(self, fakebin, tmp_path):
        code = _helper('COMPOSE=(docker compose -f "/srv/my deployment/docker-compose.yml")', "compose_command_line")
        printed = _bash(fakebin, tmp_path, code).stdout
        # Evaluated back by the shell, the printed line is the same four words.
        result = _bash(fakebin, tmp_path, f"set -- {printed}\nprintf '%s\\n' \"$@\"\n")
        assert result.stdout.splitlines() == ["docker", "compose", "-f", "/srv/my deployment/docker-compose.yml"]


class TestPinnedCopies:
    """The detection restated in the single-file scripts answers as the helper does, case by case."""

    def _helper_production(self, fakebin, cwd):
        return _settled(fakebin, cwd, _helper("compose_detect_runtime", "compose_add_production_overlays"))

    @staticmethod
    def _update_sh():
        script = SCRIPTS_DIR / "update.sh"
        return _slice(script, "if command -v docker >/dev/null 2>&1 && ! docker --version", "fi") + _slice(
            script, "PROXY_ENABLED=false", "fi"
        )

    @staticmethod
    def _agent():
        return _slice(SCRIPTS_DIR / "updater" / "epicurrents-updater.sh", "detect_runtime() {", "}")

    @staticmethod
    def _start_sh():
        """The two pieces of the start.sh template: the preflight's detection, and the array built from it."""
        packager = SCRIPTS_DIR / "make-bootstrap-fixture.sh"
        return (
            _slice(packager, 'CONTAINER_RUNTIME=""', "fi")
            + _slice(packager, "read_env() {", "}")
            + _slice(packager, 'if [ "$CONTAINER_RUNTIME" = podman ]; then', "fi")
            + _slice(packager, 'PROXY_DOMAIN="$(read_env PROXY_DOMAIN)"', "fi")
        )

    @pytest.mark.parametrize("proxy", [False, True])
    @pytest.mark.parametrize("root", [False, True])
    @pytest.mark.parametrize("case", RUNTIME_CASES)
    def test_update_sh_settles_what_the_helper_settles(self, fakebin, tmp_path, case, root, proxy):
        _set_runtime(fakebin, case, root=root)
        (tmp_path / ".env").write_text("PROXY_DOMAIN=eeg.example.org\n" if proxy else "PROXY_DOMAIN=\n")
        assert _settled(fakebin, tmp_path, self._update_sh()) == self._helper_production(fakebin, tmp_path)

    @pytest.mark.parametrize("proxy", [False, True])
    @pytest.mark.parametrize("root", [False, True])
    @pytest.mark.parametrize("case", ["docker", "podman", "podman-docker"])
    def test_the_agent_settles_what_the_helper_settles(self, fakebin, tmp_path, case, root, proxy):
        _set_runtime(fakebin, case, root=root)
        (tmp_path / ".env").write_text("PROXY_DOMAIN=eeg.example.org\n" if proxy else "OTHER=1\n")
        code = f'DEPLOY_ROOT="{tmp_path}"\n' + self._agent() + "detect_runtime\n"
        assert _settled(fakebin, tmp_path, code) == self._helper_production(fakebin, tmp_path)

    def test_the_agent_reports_no_runtime_where_the_helper_names_none(self, fakebin, tmp_path):
        # The agent's copy returns 1 and its caller dies; the helper leaves the name empty and
        # compose_require_runtime refuses. Both are "no runtime", decided on the same two probes.
        _set_runtime(fakebin, "none")
        code = (
            f'DEPLOY_ROOT="{tmp_path}"\nCONTAINER_RUNTIME=""\n' + self._agent() + "detect_runtime && echo found\n"
            'printf "runtime=%s\\n" "$CONTAINER_RUNTIME"\n'
        )
        result = _bash(fakebin, tmp_path, code)
        assert result.stdout == "runtime=\n"
        assert self._helper_production(fakebin, tmp_path)[0] == ""

    @pytest.mark.parametrize("case", RUNTIME_CASES)
    def test_start_sh_names_the_runtime_the_helper_names(self, fakebin, tmp_path, case):
        _set_runtime(fakebin, case)
        code = _slice(SCRIPTS_DIR / "make-bootstrap-fixture.sh", 'CONTAINER_RUNTIME=""', "fi")
        assert _settled(fakebin, tmp_path, code)[0] == _settled(fakebin, tmp_path, _helper("compose_detect_runtime"))[0]

    @pytest.mark.parametrize("case", ["docker", "podman", "podman-docker"])
    def test_start_sh_spells_compose_as_the_helper_does(self, fakebin, tmp_path, case):
        # start.sh refuses to run as root, so only the unprivileged spelling exists there. Without a
        # PROXY_DOMAIN it comes up on the base file alone, which is its own rule and not the helper's.
        _set_runtime(fakebin, case)
        (tmp_path / ".env").write_text("PROXY_DOMAIN=\n")
        runtime, words = _settled(fakebin, tmp_path, _helper("compose_detect_runtime"))
        assert _settled(fakebin, tmp_path, self._start_sh()) == (runtime, [*words, "-f", "docker-compose.yml"])

    @pytest.mark.parametrize("case", ["docker", "podman", "podman-docker"])
    def test_start_sh_in_production_layers_what_the_helper_layers(self, fakebin, tmp_path, case):
        _set_runtime(fakebin, case)
        (tmp_path / ".env").write_text("PROXY_DOMAIN=eeg.example.org\n")
        assert _settled(fakebin, tmp_path, self._start_sh()) == self._helper_production(fakebin, tmp_path)


class TestWhoSourcesTheHelper:
    @pytest.mark.parametrize("name", SOURCING_SCRIPTS)
    def test_an_operator_script_sources_it(self, name):
        assert '. "$SCRIPT_DIR/lib/compose.sh"' in (SCRIPTS_DIR / name).read_text()

    @pytest.mark.parametrize("name", SOURCING_SCRIPTS)
    def test_an_operator_script_names_no_runtime_itself(self, name):
        # A bare call below the detection breaks Podman silently: compose would still work and only
        # that one call would fail. Comments and printed text are not calls.
        for line in (SCRIPTS_DIR / name).read_text().splitlines():
            code = line.strip()
            if not code or code.startswith(("#", "echo ", "printf ", "info ", "die ")):
                continue
            words = code.replace("$(", " ").replace("|", " ").replace(";", " ").split()
            assert not {"docker", "podman", "docker-compose", "podman-compose"} & set(words), line

    @pytest.mark.parametrize("name", SINGLE_FILE_SCRIPTS)
    def test_a_single_file_script_does_not_depend_on_it(self, name):
        # update.sh ships at a package's root and as the agent's root-owned copy, with no lib/ beside
        # either, and the release that would add the file is applied by an update.sh that predates it.
        for line in (SCRIPTS_DIR / name).read_text().splitlines():
            if not line.lstrip().startswith("#"):
                assert "lib/compose.sh" not in line, line
