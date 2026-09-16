"""Tests for env_bool, the boolean settings reader that treats an empty value as unanswered.

The case worth pinning is a bare ``NAME=`` line in .env. decouple finds the key, so the
declared default never applies, and its boolean cast reads the empty string as False —
which silently picks the unsafe side of every setting whose safe side is its default.
"""

import pytest
from decouple import Config, RepositoryEnv

from epicurrents.settings import env

FLAG = "EPICURRENTS_TEST_FLAG"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point env_bool at a .env file the test writes, through the real decouple reader."""

    def _write(body: str):
        path = tmp_path / ".env"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(env, "config", Config(RepositoryEnv(str(path))))

    return _write


@pytest.mark.parametrize("line", [f"{FLAG}=\n", f'{FLAG}=""\n', f"{FLAG}=   \n"])
def test_an_empty_value_leaves_the_default_standing(env_file, line):
    env_file(line)

    assert env.env_bool(FLAG, default=True) is True
    assert env.env_bool(FLAG, default=False) is False


def test_an_absent_key_leaves_the_default_standing(env_file):
    env_file("SOMETHING_ELSE=true\n")

    assert env.env_bool(FLAG, default=True) is True
    assert env.env_bool(FLAG, default=False) is False


@pytest.mark.parametrize(
    ("written", "expected"),
    [("true", True), ("True", True), ("1", True), ("on", True), ("false", False), ("0", False), ("off", False)],
)
def test_a_written_value_is_honoured_over_the_default(env_file, written, expected):
    env_file(f"{FLAG}={written}\n")

    assert env.env_bool(FLAG, default=not expected) is expected


def test_an_unrecognised_token_raises_rather_than_guessing(env_file):
    env_file(f"{FLAG}=maybe\n")

    with pytest.raises(ValueError):
        env.env_bool(FLAG, default=True)


def test_the_upstream_behaviour_this_helper_exists_for(env_file):
    """Pin decouple's own answer, so a version bump that changes it is visible here.

    If this ever stops being False, the helper has become redundant rather than wrong.
    """
    env_file(f"{FLAG}=\n")

    assert env.config(FLAG, default=True, cast=bool) is False
