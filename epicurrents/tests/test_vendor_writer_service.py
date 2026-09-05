"""The vendored asset tree has exactly one writer, and the deploy scripts use it.

``frontend/vendor/`` holds the Pyodide runtime and the pre-computed lead fields the
viewer fetches from the deployment's own origin. Production mounts it into ``web``
read-only on purpose — web serves the interpreter every viewer session executes — so
the tree is populated by the ``vendor`` service instead, the one container that mounts
it writable.

Nothing at runtime connects the two halves. A deploy step pointed at ``web`` fails on a
read-only filesystem *after* migrations have run, and a deployment whose tree was never
written looks entirely healthy until someone opens the analysis panel in a browser.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
UPDATE = REPO_ROOT / "scripts" / "update.sh"

VENDOR_MOUNT = "./frontend/vendor:/code/frontend/vendor"
#: The management commands that write into the tree, whichever script calls them.
WRITERS = ("vendor_pyodide", "generate_compute_static")


class _ComposeLoader(yaml.SafeLoader):
    """Safe loader that tolerates compose's local tags (``!override``, ``!reset``)."""


def _keep_local_tags(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _keep_local_tags)


def _services(path):
    return yaml.load(path.read_text(), Loader=_ComposeLoader)["services"]


class TestOneWriter:
    def test_web_mounts_the_tree_read_only_in_production(self):
        mounts = _services(PROD_COMPOSE)["web"]["volumes"]
        assert f"{VENDOR_MOUNT}:ro" in mounts, (
            "web must keep the vendored tree read-only — it serves the interpreter "
            "every viewer session runs, so a write path from the request-handling "
            "container is where a compromise reaches every visitor's browser"
        )

    def test_the_vendor_service_mounts_the_tree_writable(self):
        mounts = _services(PROD_COMPOSE)["vendor"]["volumes"]
        assert VENDOR_MOUNT in mounts, (
            "the vendor service is the only writer a production deployment has; "
            "without this mount both deploy steps fail on a read-only filesystem"
        )
        assert f"{VENDOR_MOUNT}:ro" not in mounts

    def test_the_vendor_service_is_profile_gated(self):
        # A one-shot writer that `up` starts would be restarted forever by the
        # production restart policy, and would hold the tree open for no reason.
        assert _services(BASE_COMPOSE)["vendor"]["profiles"] == ["vendor"]


class TestDeployScriptsUseTheWriter:
    """A step pointed at ``web`` aborts the deploy after migrations, mid-update."""

    def test_every_writing_command_runs_in_the_vendor_service(self):
        for script in (BOOTSTRAP, UPDATE):
            for line in script.read_text().splitlines():
                if not any(f"manage.py {command}" in line for command in WRITERS):
                    continue
                assert re.search(r"\s-T vendor python manage\.py", line), (
                    f"{script.name} writes the vendored tree through a service other "
                    f"than `vendor`, which mounts it read-only:\n    {line.strip()}"
                )

    def test_the_profile_is_enabled_on_those_calls(self):
        # `run` enables the target service's profile itself on current Compose, but
        # the flag is what keeps the call working on a runtime that does not.
        for script in (BOOTSTRAP, UPDATE):
            for line in script.read_text().splitlines():
                if not any(f"manage.py {command}" in line for command in WRITERS):
                    continue
                assert "--profile vendor" in line, f"{script.name}: {line.strip()}"
