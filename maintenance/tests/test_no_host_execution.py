"""Source scans: the web tier never executes anything, and every endpoint is gated.

The design's one invariant that no runtime test can see: the maintenance app
records intent and something outside the containers acts on it. A subprocess
call, a socket to a container runtime, or a registry entry that lets a request
name a command would each turn the app into what the design refuses to build.
"""

import ast
import inspect
from pathlib import Path

import pytest

from epicurrents.tests.test_api_auth_sweep import _iter_operations

APP_DIR = Path(__file__).resolve().parent.parent

FORBIDDEN_MODULES = {"subprocess", "pty", "docker", "podman"}
FORBIDDEN_OS_NAMES = {
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
}
FORBIDDEN_STRINGS = ("docker", "podman", "compose ", "update.sh", "/var/run")


def _source_files():
    for path in sorted(APP_DIR.rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        yield path


def _is_docstring(node, parent) -> bool:
    body = getattr(parent, "body", None)
    if not isinstance(body, list) or not body:
        return False
    return body[0] is node and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


class TestNoHostExecution:
    @pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(APP_DIR)))
    def test_no_module_imports_a_way_to_run_a_process(self, path):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
                assert not names & FORBIDDEN_MODULES, f"{path.name} imports {names & FORBIDDEN_MODULES}"
            if isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in FORBIDDEN_MODULES, f"{path.name} imports from {node.module}"
                if root == "os":
                    names = {alias.name for alias in node.names}
                    assert not names & FORBIDDEN_OS_NAMES, f"{path.name} imports os.{names & FORBIDDEN_OS_NAMES}"

    @pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(APP_DIR)))
    def test_no_module_calls_an_exec_family_function(self, path):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_OS_NAMES, f"{path.name} reaches for .{node.attr}"
            if isinstance(node, ast.Name):
                assert node.id not in {"exec", "eval"}, f"{path.name} calls {node.id}"

    @pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(APP_DIR)))
    def test_no_code_string_names_a_container_runtime_or_the_script(self, path):
        """Docstrings may explain what the agent does; code strings may not spell it."""
        tree = ast.parse(path.read_text(), filename=str(path))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            expr = parents.get(node)
            if expr is not None and _is_docstring(expr, parents.get(expr)):
                continue
            lowered = node.value.lower()
            hit = [token for token in FORBIDDEN_STRINGS if token in lowered]
            assert not hit, f"{path.name} carries {hit!r} in a code string: {node.value!r}"


@pytest.mark.django_db
class TestEveryEndpointIsGated:
    def _maintenance_operations(self):
        found = [(path, methods, view) for path, methods, view in _iter_operations() if "api/v1/maintenance/" in path]
        assert len(found) >= 9, "the maintenance API must be mounted for this scan to mean anything"
        return found

    def test_every_operation_calls_the_gate_and_a_tier_guard(self):
        for path, methods, view in self._maintenance_operations():
            source = inspect.getsource(view)
            assert "_gate_enabled(request)" in source, f"{methods} {path} is not gated on REMOTE_MAINTENANCE_ENABLED"
            assert "_require_staff(request)" in source or "_require_superuser(request)" in source, (
                f"{methods} {path} has no tier guard"
            )
            assert "_require_auth(request)" not in source, f"{methods} {path} settles for any signed-in user"

    def test_the_gate_precedes_the_guard(self):
        """404 before 401 or 403, so a disabled deployment reveals nothing about the route."""
        for path, methods, view in self._maintenance_operations():
            source = inspect.getsource(view)
            gate = source.index("_gate_enabled(request)")
            guard = min(
                i
                for i in (source.find("_require_staff(request)"), source.find("_require_superuser(request)"))
                if i >= 0
            )
            assert gate < guard, f"{methods} {path} guards before it gates"

    def test_unsafe_operations_require_a_superuser(self):
        for path, methods, view in self._maintenance_operations():
            if set(methods) & {"POST", "PUT", "PATCH", "DELETE"}:
                assert "_require_superuser(request)" in inspect.getsource(view), (
                    f"{methods} {path} writes without a superuser"
                )

    def test_no_argument_schema_carries_a_command_field(self):
        from maintenance.operations import FORBIDDEN_ARG_FIELDS, registered_operations

        for operation in registered_operations():
            assert not FORBIDDEN_ARG_FIELDS & set(operation.args_schema.model_fields), operation.key
            for name, info in operation.args_schema.model_fields.items():
                assert info.annotation is not str or name == "package_sha256", (
                    f"{operation.key}.{name} is a free string; strings reach argv and need a pattern"
                )
