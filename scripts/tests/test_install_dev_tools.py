"""Dry-run tests for scripts/install-dev-tools.sh.

The subject is the git-hook install, which writes a shim into .git/hooks/ rather
than symlinking. `ln -s` is not portable: Git for Windows materialises the link
as a plain file holding the target path, with no shebang, and git then refuses
every commit with "cannot spawn .git/hooks/pre-commit: No such file or
directory" — naming a file that exists.

The install script carries that reasoning; what earns these tests is that a hook
which fails this way reports nothing, so no other check would notice.

They assert on the installed artifact rather than on a real commit: whether git
then runs it is Tier 3, and the conftest docstring explains why that harness is
out of scope here.

The second subject is the nested-repository install, which copies instead of
shimming. ``cp`` and ``chmod`` are fakebin stubs, so nothing lands on disk there
and the call log is where the copy is observable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from .conftest import run_script

INSTALL = "install-dev-tools.sh"


@pytest.fixture
def checkout(tmp_path: Path, fakebin) -> Path:
    """A repo skeleton with a hooks source, a .git/hooks dir and a git stub.

    ``install-dev-tools.sh`` takes its repo root from ``git rev-parse
    --show-toplevel``, so the stub has to answer with the temp checkout or the
    script installs into the developer's real repository.
    """
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    hooks_src = tmp_path / "scripts" / "git-hooks"
    hooks_src.mkdir(parents=True)
    # Real chmod, not the script's: `chmod` is a fakebin stub, so a hook the
    # script installs is never actually made executable here and `exec` would
    # fail on it for a reason that has nothing to do with the shim.
    (hooks_src / "pre-commit").write_text("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(hooks_src / "pre-commit", 0o755)
    (tmp_path / ".review" / "agents").mkdir(parents=True)
    fakebin.stub("git", body=f'echo "{tmp_path.as_posix()}"\n')
    return tmp_path


def _install(checkout: Path, fakebin):
    # SKIP_DEV_TOOLS_INSTALL defaults on in run_script so other scripts' tests
    # never touch a real .git; this suite is the one that wants the install.
    return run_script(INSTALL, fakebin, cwd=checkout, extra_env={"SKIP_DEV_TOOLS_INSTALL": ""})


def test_hook_is_a_regular_file_not_a_symlink(checkout, fakebin):
    # The heart of it: a symlink is what Git for Windows cannot reproduce.
    _install(checkout, fakebin)
    hook = checkout / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    assert not hook.is_symlink()


def test_hook_is_made_executable(checkout, fakebin):
    # git will not spawn a hook without the exec bit. `chmod` is a fakebin stub
    # here — the harness must not alter modes on the host — so the call log is
    # where the mode change is observable, not the file itself.
    _install(checkout, fakebin)
    hook = checkout / ".git" / "hooks" / "pre-commit"
    assert fakebin.has_call(f"chmod +x {hook}")


def test_hook_starts_with_a_shebang(checkout, fakebin):
    # A hook without one produces the misleading ENOENT: git finds the file and
    # then has no interpreter to hand it to.
    _install(checkout, fakebin)
    hook = checkout / ".git" / "hooks" / "pre-commit"
    assert hook.read_text().startswith("#!")


def test_hook_execs_the_tracked_hook_by_repo_root(checkout, fakebin):
    # Resolved at run time rather than from $0, so a commit made from a
    # subdirectory still finds it.
    _install(checkout, fakebin)
    body = (checkout / ".git" / "hooks" / "pre-commit").read_text()
    assert "git rev-parse --show-toplevel" in body
    assert "scripts/git-hooks/pre-commit" in body


def test_shim_does_not_impose_its_own_interpreter(checkout, fakebin):
    # The tracked hooks use bash arrays and process substitution, so the shim
    # has to exec the hook and let its own shebang pick bash. Running it as
    # `sh <hook>` would override that and fail on syntax the hook relies on.
    _install(checkout, fakebin)
    body = (checkout / ".git" / "hooks" / "pre-commit").read_text()
    assert "exec sh " not in body
    assert "exec bash " not in body


def test_shim_is_valid_shell(checkout, fakebin):
    # Every other assertion here matches substrings, which a shim with a syntax
    # error would satisfy just as well — and a hook that dies in its own parse
    # is the same silent non-gate the symlink was.
    _install(checkout, fakebin)
    shim = checkout / ".git" / "hooks" / "pre-commit"
    assert subprocess.run(["sh", "-n", str(shim)], capture_output=True, check=False).returncode == 0


def test_shim_reaches_the_real_hook(checkout, fakebin):
    # The property the substring assertions only stand in for. An exit code the
    # shim cannot invent proves it execs the tracked hook rather than merely
    # naming it, and that the hook's verdict is what git ends up seeing.
    _install(checkout, fakebin)
    real = checkout / "scripts" / "git-hooks" / "pre-commit"
    real.write_text("#!/usr/bin/env bash\nexit 3\n")
    os.chmod(real, 0o755)
    result = subprocess.run(
        ["sh", str(checkout / ".git" / "hooks" / "pre-commit")],
        env={"PATH": f"{fakebin.path}:/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 3, result.stderr.decode()


def test_shim_names_its_generator(checkout, fakebin):
    # The file is not tracked and looks hand-written in a diff-less directory;
    # editing it instead of the real hook would be lost at the next install.
    _install(checkout, fakebin)
    body = (checkout / ".git" / "hooks" / "pre-commit").read_text()
    assert "install-dev-tools.sh" in body


def test_every_hook_in_the_source_directory_is_installed(checkout, fakebin):
    # The loop is over scripts/git-hooks/*, so a second hook added later gets
    # the same treatment without anyone revisiting this script.
    (checkout / "scripts" / "git-hooks" / "pre-push").write_text("#!/usr/bin/env bash\nexit 0\n")
    _install(checkout, fakebin)
    for name in ("pre-commit", "pre-push"):
        hook = checkout / ".git" / "hooks" / name
        assert hook.is_file(), f"{name} was not installed"
        assert f"scripts/git-hooks/{name}" in hook.read_text()


def test_rerunning_replaces_a_stale_hook(checkout, fakebin):
    # Idempotence is the script's stated contract, and the upgrade path from a
    # broken install runs through it: the pseudo-symlink Windows left behind is
    # exactly the "existing file" this has to overwrite.
    hook = checkout / ".git" / "hooks" / "pre-commit"
    hook.write_text("../../scripts/git-hooks/pre-commit")
    _install(checkout, fakebin)
    assert hook.read_text().startswith("#!")
    assert fakebin.has_call(f"chmod +x {hook}")


def test_install_is_idempotent(checkout, fakebin):
    _install(checkout, fakebin)
    first = (checkout / ".git" / "hooks" / "pre-commit").read_text()
    _install(checkout, fakebin)
    assert (checkout / ".git" / "hooks" / "pre-commit").read_text() == first


@pytest.fixture
def nested(tmp_path: Path, fakebin) -> Path:
    """A checkout holding the four nested-repository shapes that matter.

    An ordinary nested clone, whose `.git` is a directory and whose hooks sit at
    a path relative to it. A gitlink submodule, whose `.git` is a file and whose
    hooks live under the superproject, so the answer is absolute and must be used
    as given. A vendored tree, excluded by path. And a repository inside
    `node_modules`, which the search must not reach — a dependency that ships its
    own `.git` is not ours to put a hook in.
    """
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    hooks_src = tmp_path / "scripts" / "git-hooks"
    hooks_src.mkdir(parents=True)
    for name in ("commit-msg", "pre-commit"):
        (hooks_src / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(hooks_src / name, 0o755)
    (tmp_path / ".review" / "agents").mkdir(parents=True)

    (tmp_path / "packages" / "one" / ".git" / "hooks").mkdir(parents=True)
    submodule = tmp_path / "docs" / "vendored-docs"
    submodule.mkdir(parents=True)
    (submodule / ".git").write_text("gitdir: ../../.git/modules/docs/vendored-docs\n")
    (tmp_path / ".git" / "modules" / "docs" / "vendored-docs" / "hooks").mkdir(parents=True)
    (tmp_path / "frontend" / "viewer" / "ohif" / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / ".git" / "hooks").mkdir(parents=True)

    # The stub answers the two questions the script asks, and answers the second
    # one differently for the submodule, which is the case a fixed
    # `<repo>/.git/hooks` would get wrong.
    fakebin.stub(
        "git",
        body=f"""
case "$*" in
    *show-toplevel*) echo "{tmp_path.as_posix()}" ;;
    *"--git-path hooks"*)
        case "$PWD" in
            */docs/vendored-docs) echo "{tmp_path.as_posix()}/.git/modules/docs/vendored-docs/hooks" ;;
            *) echo ".git/hooks" ;;
        esac
        ;;
esac
""",
    )
    return tmp_path


def test_nested_repo_gets_a_copy_of_the_shared_hook(nested, fakebin):
    # A copy, not a shim: the shim resolves the repo root at run time, which
    # inside a nested repository is that repository, where scripts/git-hooks/
    # does not exist.
    _install(nested, fakebin)
    src = nested / "scripts" / "git-hooks" / "commit-msg"
    dst = nested / "packages" / "one" / ".git" / "hooks" / "commit-msg"
    assert fakebin.has_call(f"cp {src} {dst}")
    assert fakebin.has_call(f"chmod +x {dst}")


def test_submodule_hooks_path_is_used_as_given(nested, fakebin):
    # A submodule's `.git` is a file pointing into .git/modules, so the answer is
    # absolute. Prefixing it with the submodule directory would write inside the
    # working tree, where git never looks, and the install would report success.
    _install(nested, fakebin)
    dst = nested / ".git" / "modules" / "docs" / "vendored-docs" / "hooks" / "commit-msg"
    assert fakebin.has_call(f"cp {nested / 'scripts' / 'git-hooks' / 'commit-msg'} {dst}")
    assert not fakebin.has_call("docs/vendored-docs/.git/hooks/commit-msg")


def test_vendored_repo_is_skipped(nested, fakebin):
    # Upstream's commits are not ours to gate, and the hook would refuse them.
    result = _install(nested, fakebin)
    assert "Skipped vendored repo: frontend/viewer/ohif" in result.stdout
    assert not fakebin.has_call("frontend/viewer/ohif")


def test_repos_under_node_modules_are_not_visited(nested, fakebin):
    # A dependency shipping its own .git is not part of the tree we hold the
    # convention over, and installing there would be undone by the next install.
    _install(nested, fakebin)
    assert not fakebin.has_call("node_modules/dep")


def test_only_the_shared_hooks_reach_nested_repos(nested, fakebin):
    # pre-commit gates on .review/findings/, which exists in the top repository
    # alone, so copying it outward would be a gate over nothing.
    _install(nested, fakebin)
    dst = nested / "packages" / "one" / ".git" / "hooks" / "pre-commit"
    assert not fakebin.has_call(f"cp {nested / 'scripts' / 'git-hooks' / 'pre-commit'} {dst}")


def test_top_repo_still_gets_a_shim_not_a_copy(nested, fakebin):
    # The nested branch must not capture the repository the script runs in; its
    # hooks are shimmed so an edit to the tracked hook takes effect immediately.
    _install(nested, fakebin)
    hook = nested / ".git" / "hooks" / "commit-msg"
    assert hook.is_file()
    assert "scripts/git-hooks/commit-msg" in hook.read_text()
    assert not fakebin.has_call(f"cp {nested / 'scripts' / 'git-hooks' / 'commit-msg'} {hook}")


def test_nested_install_is_idempotent(nested, fakebin):
    first = _install(nested, fakebin)
    second = _install(nested, fakebin)
    assert first.stdout == second.stdout
