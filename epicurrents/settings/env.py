"""Environment-variable helpers shared by the settings modules.

Kept in its own module rather than in ``common.py`` so that ``production.py`` can read
a setting the same way without importing the common namespace.
"""

from decouple import config


def env_bool(name: str, *, default: bool) -> bool:
    """Read a boolean environment variable, treating an empty value as unanswered.

    ``config(name, default=True, cast=bool)`` returns False for a bare ``NAME=`` line in
    .env, and for ``NAME=""`` and ``NAME=`` followed by spaces as well. decouple finds the
    key, so the declared default is never consulted, and its boolean cast maps the empty
    string to False. A half-written line therefore reads as a deliberate opt-out, and
    wherever the default is the safe direction — CSRF enforcement, a restrictive access
    tier — that silently selects the unsafe one, with nothing raised and nothing logged.

    An empty value means the operator has not answered, so *default* stands. Anything else
    goes to decouple's own true/false vocabulary, which rejects an unrecognised token by
    raising, and a setting that cannot be read stops the boot rather than guessing.
    """
    raw = config(name, default=None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return config(name, default=default, cast=bool)
