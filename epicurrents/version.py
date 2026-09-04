"""The platform's version, and the comparison a project's pin is resolved with.

Its own module rather than a line in ``epicurrents/__init__.py``, because that
package imports the Celery app: anything reading the version from there drags
Celery in, and the readers include a shell script at release time and a system
check that runs before most of Django is up. Nothing here imports anything but
``re``. ``epicurrents.__version__`` re-exports it for the usual spelling.

Versions are `semantic <https://semver.org>`_, and the promise is deliberately
narrower than the whole codebase — see `epicurrents/README.md`_ for what a major
version covers. A number that promised compatibility across every module would
have to bump its major on nearly every change, which is the same as promising
nothing.

Releases are plain ``MAJOR.MINOR.PATCH``. Pre-release and build metadata are
part of semver and are rejected here rather than half-supported: their
precedence rules are subtle, nothing needs them yet, and a comparison that is
almost right about ``1.1.0-rc1`` is worse than one that refuses it. Adding them
is a deliberate change to :func:`parse_version`, not an oversight to fix.

.. _epicurrents/README.md: README.md
"""

from __future__ import annotations

import re

#: The running platform version. A release tags this exact value as ``v<version>``.
#:
#: Below 1.0.0 by intent, not by omission: semver reserves major version zero
#: for initial development, and the surface this number promises over — the
#: extension points a project builds on — is not stable yet. See
#: :func:`compatible_range` for what that does to a cap.
__version__ = "0.1.0"

# Semver's own grammar for the numeric core: no leading zeroes, so "1.01.0" is
# rejected rather than silently read as 1.1.0.
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Leading zeroes are rejected on the specifier side too, but one or two
# components are allowed: `<2` and `>=1.4` are how a cap and a floor are
# naturally written, and demanding `<2.0.0` would be pedantry.
_PARTIAL_RE = re.compile(r"^(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?$")

# `>=` before `>` and `<=` before `<`: alternation is ordered, so the two-character
# operators have to come first or `>=1.0` parses as `>` with version `=1.0`.
_CLAUSE_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(.+)$")

_OPERATORS = {
    ">=": lambda actual, wanted: actual >= wanted,
    "<=": lambda actual, wanted: actual <= wanted,
    ">": lambda actual, wanted: actual > wanted,
    "<": lambda actual, wanted: actual < wanted,
    "==": lambda actual, wanted: actual == wanted,
    "!=": lambda actual, wanted: actual != wanted,
}


class InvalidVersion(ValueError):
    """A version or specifier string that cannot be read as semver."""


def _require_str(value, what: str) -> str:
    """Reject a non-string before a string method does it less helpfully.

    These values come from a project's ``AppConfig``, so a tuple or a list is a
    plausible way to get the declaration wrong. Left to ``.strip()`` it raises
    ``AttributeError`` from inside this module, which surfaces as a traceback
    naming this file rather than a system-check finding naming the project — and
    it takes ``migrate`` and ``runserver`` down with it, since both run checks.
    """
    if not isinstance(value, str):
        raise InvalidVersion(f"{what} must be a string, not {type(value).__name__}")
    return value


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse a full ``MAJOR.MINOR.PATCH`` string into a comparable tuple.

    Strict: this is the form the platform's own version must take, and a typo
    that parsed leniently would compare wrongly against every project's pin.
    """
    match = _VERSION_RE.match(_require_str(value, "a version").strip())
    if not match:
        raise InvalidVersion(
            f"{value!r} is not a MAJOR.MINOR.PATCH version. "
            "Pre-release and build metadata are not used by this platform."
        )
    return tuple(int(part) for part in match.groups())


def parse_partial_version(value: str) -> tuple[int, int, int]:
    """Parse a one-, two-, or three-component version, padding with zeroes.

    Only for the right-hand side of a specifier clause, where ``<2`` means
    ``<2.0.0`` and ``>=1.4`` means ``>=1.4.0``. Note what the padding does to an
    equality: ``==1.4`` is ``==1.4.0`` and matches no other patch release, which
    is why a range is the documented way to say "any 1.4".
    """
    match = _PARTIAL_RE.match(_require_str(value, "a version").strip())
    if not match:
        raise InvalidVersion(f"{value!r} is not a version number")
    major, minor, patch = match.groups()
    return int(major), int(minor or 0), int(patch or 0)


def satisfies(version: str, specifier: str) -> bool:
    """Whether *version* meets every comma-separated clause of *specifier*.

    Clauses are ANDed, in the shape ``">=0.1,<0.2"``. Raises
    :class:`InvalidVersion` rather than returning ``False`` for a malformed
    specifier: an unreadable pin is a configuration error, and answering "no"
    would report it as an ordinary incompatibility with a misleading remedy.
    """
    actual = parse_version(version)
    clauses = [clause.strip() for clause in _require_str(specifier, "a version specifier").split(",") if clause.strip()]
    if not clauses:
        raise InvalidVersion(f"{specifier!r} contains no version clauses")
    for clause in clauses:
        match = _CLAUSE_RE.match(clause)
        if not match:
            raise InvalidVersion(
                f"{clause!r} is not a version clause; expected an operator "
                f"({', '.join(_OPERATORS)}) followed by a version, as in '>=1.0'"
            )
        operator, wanted = match.groups()
        if not _OPERATORS[operator](actual, parse_partial_version(wanted)):
            return False
    return True


def compatible_range(version: str) -> str:
    """The range a project developed against *version* should pin.

    The floor is that version's minor, because a project built against 0.4 must
    not claim to run on 0.1. The cap is where the next breaking change is
    allowed to land, and semver puts that in a different place below 1.0: major
    version zero is initial development, where the *minor* is the breaking bump
    and the patch is everything else. So 0.1.0 caps at ``<0.2`` and 1.4.2 caps
    at ``<2``.

    Encoded here rather than written out wherever a range is suggested, because
    the two rules look similar enough that a cap computed as "major + 1" reads
    as correct and silently admits every breaking release of a 0.x platform.
    """
    major, minor, _ = parse_version(version)
    if major == 0:
        return f">=0.{minor},<0.{minor + 1}"
    return f">={major}.{minor},<{major + 1}"


#: ``__version__`` as a tuple, for callers comparing without re-parsing.
VERSION_INFO = parse_version(__version__)
