"""Contract tests for the image's packaging: the multi-stage split and the lock.

The runtime image must not carry the test toolchain. pytest, coverage,
model-bakery and pytest-httpserver in a production container are reachable code
that no deployment needs, and the packaging manifest stops being honest about
what ships the moment they are back.

These are static assertions against the Dockerfile and the compose services
rather than a build: a real build takes minutes and needs a daemon, and what
regresses here is the wiring — a new service that forgets `target:`, or a
`pip install -r requirements-test.txt` that drifts back into the shared stage.
The build itself was exercised by hand when this landed: the runtime image
answers `manage.py check` and raises ModuleNotFoundError on `import pytest`,
while the test image imports the toolchain fine.

Note for anyone reproducing that locally: a working copy whose files are mode
0600 produces an image the unprivileged user cannot read, which surfaces as
PermissionError on an arbitrary module rather than anything naming permissions.
Git records only the executable bit, so a fresh checkout under a normal umask
does not have this. Build from a mode-normalised export when in doubt.
"""

import re
from pathlib import Path

import yaml
from django.conf import settings

REPO = Path(settings.BASE_DIR)
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"
LOCK = REPO / "requirements.lock"
REQUIREMENTS = REPO / "requirements.txt"

# Distribution packages that must not be installed in the runtime image.
TEST_ONLY_PACKAGES = ("pytest", "pytest-django", "pytest-cov", "pytest-httpserver", "model-bakery")


class _Tolerant(yaml.SafeLoader):
    """Compose carries custom tags (``!override``) that SafeLoader rejects."""


def _ignore_tag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_Tolerant.add_multi_constructor("!", _ignore_tag)


def _stages() -> dict[str, str]:
    """Map each named build stage to its body text.

    The name pattern admits a hyphen. It did not when the only stages were
    base / test / runtime, and `project-reqs` then keyed as `project` — the
    truncation is silent, because the offsets stay correct and every existing
    assertion still finds the stage it was looking for.
    """
    source = DOCKERFILE.read_text()
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^FROM\s+\S+\s+AS\s+([\w-]+)", source, re.MULTILINE)]
    stages = {}
    for index, (offset, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(source)
        stages[name] = source[offset:end]
    return stages


def _normalise(name: str) -> str:
    """PEP 503 name normalisation, so Django / django and _ / - / . compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(text: str) -> set[str]:
    """Distribution names from a requirements or lock file, ignoring directives."""
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(_normalise(match.group(1)))
    return names


def _pip_commands(source: str | None = None) -> list[str]:
    """Every individual `pip install ...` invocation in the Dockerfile.

    Line continuations are joined and the result split on ``&&``, because the
    install steps are chained inside one RUN. Treating the joined RUN as a single
    line would report two separate invocations as one — which is exactly the
    distinction the --require-hashes rules below turn on.
    """
    text = source if source is not None else DOCKERFILE.read_text()
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [part.strip() for part in joined.replace("\n", " && ").split("&&") if "pip install" in part]


def _installs_test_requirements(stage_body: str) -> bool:
    """Whether a stage runs a pip install that names requirements-test.txt.

    Line continuations are joined first, since the RUN in the base stage spans
    several backslash-continued lines and the install would otherwise be split
    away from the filename it installs.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", stage_body)
    return any("pip install" in line and "requirements-test.txt" in line for line in joined.splitlines())


def _services() -> dict:
    return yaml.load(COMPOSE.read_text(), Loader=_Tolerant)["services"]


def _build_target(spec: dict) -> str | None:
    """The stage a service builds, or None when it names none.

    The short form ``build: .`` parses to a string rather than a mapping, so
    reaching for .get on it raises AttributeError instead of reporting a
    finding — and reverting to the short form is precisely the regression the
    caller is looking for.
    """
    build = spec.get("build")
    return build.get("target") if isinstance(build, dict) else None


class TestStageLayout:
    def test_the_expected_stages_exist(self):
        assert {"base", "test", "runtime"} <= set(_stages())

    def test_runtime_is_the_final_stage(self):
        """A bare `docker build .` with no --target builds the last stage, so the
        lean image has to be the default. CI's smoke job relies on this."""
        names = re.findall(r"^FROM\s+\S+\s+AS\s+([\w-]+)", DOCKERFILE.read_text(), re.MULTILINE)
        assert names[-1] == "runtime"

    def test_test_requirements_are_installed_only_in_the_test_stage(self):
        """Matches any pip install naming requirements-test.txt, not one exact
        spelling of it. The regression this guards against arrives as another
        -r appended to the existing install line, which an exact-substring check
        sails straight past — that version of this test passed the mutation.

        Scoped to install commands because the base stage legitimately COPYs the
        file: both stages share one requirements layer, and only the test stage
        acts on it.
        """
        stages = _stages()
        assert _installs_test_requirements(stages["test"]), "the test stage no longer installs the test toolchain"
        for name in ("base", "runtime"):
            assert not _installs_test_requirements(stages[name]), (
                f"the {name} stage installs the test toolchain, which puts pytest back in the runtime image"
            )

    def test_runtime_installs_the_production_requirements(self):
        """Guards the inverse mistake: a split that leaves the runtime image
        without its own dependencies fails at container start, not at build.

        Asserts an install rather than the mere presence of the filename — the
        base stage COPYs several requirements files it does not act on, so a
        substring check here would pass against a stage that installs nothing.
        """
        installs = _pip_commands(_stages()["base"])
        assert any("requirements.lock" in cmd for cmd in installs), (
            "the shared stage installs no production dependencies"
        )

    def test_both_leaf_stages_drop_privilege(self):
        stages = _stages()
        for name in ("test", "runtime"):
            assert "USER appuser" in stages[name], f"{name} runs as root by default"


class TestDependencyLock:
    """Version pins alone let two builds of one commit install different
    transitive trees, and make pip-audit report on a resolution nobody
    necessarily gets. The lock pins artifacts, and --require-hashes turns a
    substituted one into a failed build. Verified by hand when this landed: a
    single corrupted hash in the lock fails the install with pip's tampering
    error rather than resolving to something else.
    """

    def test_the_lock_exists(self):
        assert LOCK.exists(), "requirements.lock is missing — run scripts/lock-requirements.sh"

    def test_every_pinned_package_carries_a_hash(self):
        """One unhashed entry is enough for pip to reject the whole file under
        --require-hashes, so this fails the build rather than shipping — but it
        fails it at image-build time, which is a slow way to find out."""
        text = LOCK.read_text()
        # Join the backslash continuations that carry each package's hashes.
        joined = re.sub(r"\\\s*\n\s*", " ", text)
        unhashed = [
            line.split()[0]
            for line in joined.splitlines()
            if line.strip() and not line.strip().startswith("#") and "==" in line and "--hash=" not in line
        ]
        assert not unhashed, f"lock entries without a hash: {unhashed}"

    def test_every_direct_dependency_is_locked(self):
        """Catches the common mistake: adding a package to requirements.txt and
        forgetting to regenerate, which installs the old closure silently."""
        missing = _requirement_names(REQUIREMENTS.read_text()) - _requirement_names(LOCK.read_text())
        assert not missing, (
            f"in requirements.txt but not requirements.lock: {sorted(missing)} — "
            "run scripts/lock-requirements.sh and commit the result"
        )

    def test_the_lock_is_installed_with_require_hashes(self):
        """Without the flag the hashes in the file are inert: pip reads the pins
        and ignores the hashes, so the lock documents an intent it does not
        enforce."""
        install = [cmd for cmd in _pip_commands() if "requirements.lock" in cmd]
        assert install, "the Dockerfile does not install requirements.lock"
        for command in install:
            assert "--require-hashes" in command


class TestComposeTargets:
    def test_every_building_service_names_a_target(self):
        """Without an explicit target a service silently builds the final stage.
        That is the safe one today, but it makes the choice invisible and a
        reordering of the Dockerfile would change what every service runs."""
        missing = [name for name, spec in _services().items() if spec.get("build") and not _build_target(spec)]
        assert not missing, f"compose services build without naming a stage: {missing}"

    def test_application_services_build_the_runtime_stage(self):
        services = _services()
        for name in ("web", "celery", "celery-beat", "migrate"):
            assert services[name]["build"]["target"] == "runtime", f"{name} would ship the test toolchain"

    def test_test_services_build_the_test_stage(self):
        services = _services()
        for name in ("test", "test-postgres"):
            assert services[name]["build"]["target"] == "test"

    def test_test_only_packages_are_not_in_the_runtime_requirements(self):
        """The stage split is worth nothing if a test package migrates into
        requirements.txt, which every stage installs."""
        runtime_reqs = (REPO / "requirements.txt").read_text().lower()
        for package in TEST_ONLY_PACKAGES:
            assert not re.search(rf"^{re.escape(package)}\b", runtime_reqs, re.MULTILINE), (
                f"{package} is in requirements.txt, so it reaches the runtime image regardless of the stage split"
            )
