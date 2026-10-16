"""Settings for the *hedscore* plugin, merged between ``common`` and the active project."""

# Top-level schema branches a ``hed`` code may use, by name (``Modulator``, ``Episode``, ``Property``). Empty accepts
# every branch. A project narrows it, which is the control a project whose codes must stay condition markers relies
# on; ``manage.py check`` refuses a name the pinned schema does not have.
HEDSCORE_ALLOWED_BRANCHES: list[str] = []
