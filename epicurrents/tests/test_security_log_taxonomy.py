"""Contract test for the security-event-type taxonomy.

``epicurrents/security_log.py`` documents the well-known set of event
types in its module docstring.  Operators write SIEM rules against
those specific tokens, so a typo at a call site (``auth.login_faild``
instead of ``auth.login_failed``) creates a separate stream that the
rule set never matches — silently dropping a high-signal event.

This test walks the repository, finds every call to
``log_security_event(...)``, extracts the first argument (the
``event_type`` string literal), and asserts each one is present in
the documented taxonomy.  Dynamic event-type values that aren't string
literals are ignored — if any are added later, this test will need
extending; until then, the literal-only check is enough.

The documented set is also extracted from the module docstring
programmatically so adding a new event type to one side without the
other fails the test.
"""

import ast
import re
from pathlib import Path

from epicurrents import security_log

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Files / directories to skip when walking the tree.  Everything inside
# ``.venv``, ``node_modules``, ``frontend``, etc. is irrelevant.
SKIP_DIRS = {
    ".venv",
    ".git",
    "node_modules",
    "frontend",
    "docs",
    "borgmatic",
    "__pycache__",
}


def _extract_documented_event_types() -> set[str]:
    """Parse the module docstring of ``security_log.py`` to find documented
    event types.  Conventional shape: bullet list entries like
    ``- ``auth.login_failed`` — ...``.  Returns the set of tokens.

    The regex is anchored to the ``- ``…``…`` bullet shape rather than
    matching every backtick-quoted dotted identifier in the docstring
    so that incidental mentions like ``epicurrents.security`` (logger
    name) or ``logger.warning`` (anti-pattern reference) are not
    misread as event types.

    Digits are allowed in the suffix for ``auth.2fa_*``.  Widening the
    character class only adds tokens — every name the earlier
    alpha-only pattern matched still matches — so no documented event
    type drops out of the set and stops being checked.
    """
    doc = security_log.__doc__ or ""
    return set(re.findall(r"^- ``([a-z][a-z0-9]*\.[a-z0-9_]+)``", doc, flags=re.MULTILINE))


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _extract_log_security_event_calls():
    """Yield (file_path, line_number, event_type_literal_or_None) for every
    ``log_security_event(...)`` call in the codebase.

    ``event_type_literal_or_None`` is the string literal when the first
    positional argument is a constant string, or ``None`` when it's a
    dynamic expression (variable, function call, etc.).
    """
    for path in _iter_python_files():
        try:
            source = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "log_security_event" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "log_security_event":
                continue
            literal = None
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                literal = node.args[0].value
            yield path, node.lineno, literal


def test_documented_event_types_are_non_empty():
    """Sanity check — the module docstring must enumerate at least one
    event type, otherwise the rest of the test is vacuous."""
    documented = _extract_documented_event_types()
    assert documented, (
        "No event types found in security_log.__doc__ — the taxonomy "
        "extraction is broken, or the docstring no longer follows the "
        "``- ``event.name`` — ...`` bullet format that this test relies on."
    )


def test_every_call_site_uses_a_documented_event_type():
    documented = _extract_documented_event_types()
    bad = []
    for path, lineno, literal in _extract_log_security_event_calls():
        if literal is None:
            # Dynamic event_type value — we can't statically verify it.
            # If this becomes common, extend the test or document the
            # exception.  Today there are no such call sites.
            continue
        if literal not in documented:
            rel = path.relative_to(REPO_ROOT)
            bad.append(f"{rel}:{lineno} — {literal!r}")

    assert not bad, (
        "Call sites use event_type values not in the documented taxonomy "
        "(security_log.py module docstring).  Either add the event type "
        "to the docstring or fix the call-site typo:\n" + "\n".join(f"  - {entry}" for entry in bad)
    )


def test_every_documented_event_type_has_at_least_one_call_site():
    """The taxonomy is a contract with operators; dead entries pollute
    the documentation and mislead anyone writing SIEM rules.  Assert
    each documented event type has at least one live call site."""
    documented = _extract_documented_event_types()
    used = {literal for _path, _line, literal in _extract_log_security_event_calls() if literal is not None}
    orphans = documented - used
    assert not orphans, (
        "These event types are documented in security_log.__doc__ but "
        "no call site uses them — either delete the documentation or "
        "restore the call site that used to emit them:\n" + "\n".join(f"  - {e}" for e in sorted(orphans))
    )
