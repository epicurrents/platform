"""Dry-run tests for bootstrap.sh's project-clone step (6b).

Projects live in their own repositories, so a fresh platform checkout has no
``projects/<name>/`` for ``activate_project`` to act on. Step 6b clones it,
which puts two things under test: where it decides to clone *from*, and what it
does when it cannot decide at all.

The resolver is the part worth pinning. It accepts a bare name, an ``org/name``
pair, any explicit scheme, scp-style SSH, and a local path, and the branches are
close enough together that a pattern reordering would silently reroute one form
into another — a bare name becoming a filesystem path, or an SSH URL becoming a
GitHub one. The tilde case is here because it already failed once: bash expands
an unquoted ``~/`` inside a ``case`` pattern to ``$HOME``, so the literal read
out of .env matched nothing and fell through to the bare-name branch, arriving
at ``https://github.com/~/src/thing``.

See ``conftest.py`` for the fakebin pattern.
"""

import subprocess
from pathlib import Path

import pytest

from scripts.tests.conftest import make_env, make_env_example, run_script

BOOTSTRAP = "bootstrap.sh"
SCRIPT = Path(__file__).resolve().parent.parent / "bootstrap.sh"


def resolve(spec: str) -> str:
    """Run the script's own resolver against one spec, in isolation.

    Extracted from the shipped script rather than reimplemented, so the test
    cannot pass against a copy that has drifted from what bootstrap runs.
    """
    source = SCRIPT.read_text()
    start = source.index("resolve_project_repo() {")
    end = source.index("\n}\n", start) + 3
    fn = source[start:end]
    result = subprocess.run(
        ["bash", "-c", f'{fn}\nresolve_project_repo "$1"', "_", spec],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class TestRepoSpecResolution:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("example", "https://github.com/epicurrents/example"),
            ("someorg/thing", "https://github.com/someorg/thing"),
            ("https://gitlab.example/org/x.git", "https://gitlab.example/org/x.git"),
            ("http://host/org/x.git", "http://host/org/x.git"),
            ("ssh://git@host:2222/org/x.git", "ssh://git@host:2222/org/x.git"),
            ("git@github.com:epicurrents/example.git", "git@github.com:epicurrents/example.git"),
            ("/srv/src/thing", "/srv/src/thing"),
            ("./local", "./local"),
            ("../sibling", "../sibling"),
        ],
    )
    def test_each_accepted_form(self, spec, expected):
        assert resolve(spec) == expected

    def test_tilde_expands_to_home_rather_than_becoming_a_url(self):
        """The regression. A tilde path must stay a path."""
        out = resolve("~/src/thing")
        assert out.endswith("/src/thing")
        assert not out.startswith("https://")
        assert "~" not in out

    def test_a_bare_name_never_resolves_to_a_local_path(self):
        """The two failure directions that matter, stated as their own test:
        a name must not become a path, and a path must not become a URL."""
        assert resolve("example").startswith("https://")
        assert not resolve("/srv/example").startswith("https://")


@pytest.mark.usefixtures("fakebin")
class TestCloneStep:
    def test_clones_when_the_project_directory_is_absent(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        make_env(tmp_path, EPICURRENTS_PROJECT="thing", EPICURRENTS_PROJECT_REPO="thing")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        # Asserted as one exact invocation rather than a choice of shapes: an
        # `or` across two spellings passes whichever one is produced, so it
        # could not tell a deliberate change from an accidental one.
        assert fakebin.has_call("clone --depth 1 https://github.com/epicurrents/thing projects/thing"), (
            "expected a shallow clone of the resolved source into projects/thing"
        )

    def test_does_not_clone_when_the_project_is_already_present(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        make_env(tmp_path, EPICURRENTS_PROJECT="thing", EPICURRENTS_PROJECT_REPO="thing")
        (tmp_path / "projects" / "thing").mkdir(parents=True)
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("projects/thing"), "an existing project directory must not be re-cloned"

    def test_does_not_clone_when_no_project_is_active(self, fakebin, tmp_path):
        make_env_example(tmp_path)
        make_env(tmp_path, EPICURRENTS_PROJECT="", EPICURRENTS_PROJECT_REPO="")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert not fakebin.has_call("git clone"), "the base platform needs no project clone"

    def test_the_clone_runs_before_the_image_is_built(self, fakebin, tmp_path):
        """The ordering is the whole reason the step sits where it does.

        The image build reads the project from disk twice — it installs the
        project's requirements.lock, and its `COPY . .` is how the project's
        Python source reaches the production image, which has no source
        bind-mount. Cloning afterwards builds an image missing both and fails
        nothing: the build succeeds, and the stack dies later on an import.

        Both indices are asserted to exist before they are compared, because
        `.index()` on a missing call raises where a missing *build* should read
        as a failure of this test's own premise rather than of the ordering.
        """
        make_env_example(tmp_path)
        make_env(tmp_path, EPICURRENTS_PROJECT="thing", EPICURRENTS_PROJECT_REPO="thing")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        calls = fakebin.calls()
        clone = [i for i, c in enumerate(calls) if "clone" in c and "projects/thing" in c]
        build = [i for i, c in enumerate(calls) if "compose build web" in c]
        assert clone, "the project was never cloned"
        assert build, "the image was never built"
        assert clone[0] < build[0], (
            "the image is built before the project is cloned, so it carries neither the "
            "project's dependencies nor its source"
        )

    def test_missing_repo_setting_fails_with_an_actionable_message(self, fakebin, tmp_path):
        """The case that decides whether an operator can act on the failure:
        a named project, no directory, and nowhere stated to get it from."""
        make_env_example(tmp_path)
        make_env(tmp_path, EPICURRENTS_PROJECT="thing", EPICURRENTS_PROJECT_REPO="")
        result = run_script(BOOTSTRAP, fakebin, cwd=tmp_path)
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "EPICURRENTS_PROJECT_REPO" in combined
        assert "projects/thing" in combined
