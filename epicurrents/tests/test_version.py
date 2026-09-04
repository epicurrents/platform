"""Tests for the platform version and the specifier comparison built on it.

The comparison decides whether a deployment boots, so the cases that matter are
the ones where a plausible wrong implementation still passes the obvious tests.
Chief among them is ordering: compared as strings, ``1.10.0`` sorts below
``1.9.0``, and a project pinned ``>=1.9`` would be refused on the release that
supersedes it. Every other property here is a boundary.
"""

import re

import pytest

from epicurrents import __version__ as reexported_version
from epicurrents.version import (
    VERSION_INFO,
    InvalidVersion,
    __version__,
    compatible_range,
    parse_partial_version,
    parse_version,
    satisfies,
)


class TestTheVersionItself:
    def test_is_a_semver_triple(self):
        assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", __version__), (
            f"{__version__!r} is not MAJOR.MINOR.PATCH; releases do not use pre-release or build metadata"
        )

    def test_version_info_matches_the_string(self):
        assert VERSION_INFO == parse_version(__version__)

    def test_the_package_reexports_it(self):
        """`epicurrents.__version__` is the spelling most callers will reach for."""
        assert reexported_version == __version__

    def test_the_version_module_imports_without_django(self):
        """Asserted as a property of the module, not of this test run.

        The module is read by a system check that runs before most of Django is
        up, and by tooling outside it. An import of Django or Celery added here
        would work in this suite and fail there.
        """
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parent.parent / "version.py"
        tree = ast.parse(source.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert imported <= {"re", "__future__"}, f"version.py grew a dependency: {sorted(imported)}"


class TestCompatibleRange:
    """The cap rule, which differs either side of 1.0 and looks like it does not.

    Semver reserves major zero for initial development, where the *minor* is the
    breaking bump. A cap computed as "major + 1" is right from 1.0 onwards and
    silently admits every breaking release below it — `>=0.1,<1` accepts 0.2,
    0.3 and 0.9, each of which semver permits to break everything.
    """

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("0.1.0", ">=0.1,<0.2"),
            ("0.1.7", ">=0.1,<0.2"),  # the patch does not move the range
            ("0.9.0", ">=0.9,<0.10"),  # and 0.10 follows 0.9, not 0.1
            ("1.0.0", ">=1.0,<2"),
            ("1.4.2", ">=1.4,<2"),
            ("2.0.0", ">=2.0,<3"),
        ],
    )
    def test_the_cap_lands_where_the_next_breaking_release_may(self, version, expected):
        assert compatible_range(version) == expected

    @pytest.mark.parametrize("version", ["0.1.0", "0.9.0", "1.0.0", "1.4.2", "2.0.0"])
    def test_the_suggested_range_always_admits_the_version_it_came_from(self, version):
        """A suggestion the running platform fails is worse than none: it reads
        as authoritative and produces a deployment that refuses to boot."""
        assert satisfies(version, compatible_range(version))

    def test_a_zero_x_range_excludes_the_next_minor(self):
        """Stated on its own because it is the whole point of the 0.x branch."""
        assert not satisfies("0.2.0", compatible_range("0.1.0"))
        assert satisfies("0.1.99", compatible_range("0.1.0"))


class TestParseVersion:
    @pytest.mark.parametrize("value", ["1.0.0", "0.0.0", "10.20.30"])
    def test_accepts_full_triples(self, value):
        assert parse_version(value) == tuple(int(p) for p in value.split("."))

    @pytest.mark.parametrize(
        "value",
        [
            "1.0",  # not a full triple
            "1",
            "1.0.0.0",
            "01.0.0",  # leading zero, semver forbids
            "1.0.0-rc1",  # pre-release deliberately unsupported
            "1.0.0+build",
            "v1.0.0",
            "",
            "one.0.0",
        ],
    )
    def test_rejects_everything_else(self, value):
        with pytest.raises(InvalidVersion):
            parse_version(value)

    def test_tolerates_surrounding_whitespace(self):
        assert parse_version("  1.2.3  ") == (1, 2, 3)


class TestParsePartialVersion:
    @pytest.mark.parametrize(
        "value,expected",
        [("2", (2, 0, 0)), ("1.4", (1, 4, 0)), ("1.4.7", (1, 4, 7)), ("0", (0, 0, 0))],
    )
    def test_pads_missing_components_with_zero(self, value, expected):
        assert parse_partial_version(value) == expected

    @pytest.mark.parametrize("value", ["1.4.7.1", "01", "1.04", "", "1.x"])
    def test_rejects_malformed(self, value):
        with pytest.raises(InvalidVersion):
            parse_partial_version(value)


class TestSatisfies:
    def test_ordering_is_numeric_not_lexicographic(self):
        """The bug a string comparison would introduce, stated on its own.

        `"1.10.0" < "1.9.0"` is true for strings, so a project pinned to at
        least 1.9 would be refused by the release that follows it — and refused
        with a message saying the platform is too old.
        """
        assert satisfies("1.10.0", ">=1.9")
        assert not satisfies("1.9.0", ">=1.10")
        assert satisfies("1.0.10", ">=1.0.9")

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("0.9.0", False),  # below the floor
            ("1.0.0", True),  # on the floor
            ("1.4.2", True),
            ("1.99.99", True),  # anything in the major
            ("2.0.0", False),  # the cap excludes its own version
            ("2.0.1", False),
        ],
    )
    def test_the_documented_range_shape(self, version, expected):
        assert satisfies(version, ">=1.0,<2") is expected

    def test_clauses_are_anded(self):
        assert satisfies("1.5.0", ">=1.0,<2,!=1.4.0")
        assert not satisfies("1.4.0", ">=1.0,<2,!=1.4.0")

    @pytest.mark.parametrize("specifier", [">= 1.0, < 2", ">=1.0 , <2", "  >=1.0,<2  "])
    def test_tolerates_whitespace(self, specifier):
        assert satisfies("1.2.3", specifier)

    def test_equality_compares_the_padded_triple(self):
        """Documented surprise: `==1.4` is `==1.4.0`, not "any 1.4"."""
        assert satisfies("1.4.0", "==1.4")
        assert not satisfies("1.4.1", "==1.4")

    @pytest.mark.parametrize("specifier", ["", "  ", ",", "1.0", "~=1.0", ">=1.0,garbage", ">>1.0"])
    def test_a_malformed_specifier_raises_rather_than_returning_false(self, specifier):
        """A pin nobody can read is a configuration error, not an incompatibility.

        Returning False would report it as "this platform is too old", sending
        the reader to change the wrong thing.
        """
        with pytest.raises(InvalidVersion):
            satisfies("1.0.0", specifier)

    def test_a_malformed_platform_version_raises(self):
        with pytest.raises(InvalidVersion):
            satisfies("1.0", ">=1.0")

    @pytest.mark.parametrize("specifier", [(">=1.0", "<2"), [">=1.0"], 1.0, None])
    def test_a_non_string_specifier_raises_invalidversion_not_attributeerror(self, specifier):
        """`requires_platform` is hand-written on an AppConfig, so a tuple or a
        list is a plausible way to get it wrong. Reaching `.split()` on one
        raises AttributeError from inside this module, which the system check
        does not catch — so `manage.py check` ends in a traceback naming
        version.py instead of a finding naming the project, and takes `migrate`
        and `runserver` with it.
        """
        with pytest.raises(InvalidVersion):
            satisfies("1.0.0", specifier)

    @pytest.mark.parametrize("value", [(1, 0, 0), None, 1.0])
    def test_a_non_string_version_raises_invalidversion(self, value):
        with pytest.raises(InvalidVersion):
            parse_version(value)
