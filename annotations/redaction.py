"""Which annotation rows must reach a caller without their text.

⚠️ LOAD-BEARING — annotation-text sanitisation.
A read grant carrying ``apply_middleware`` de-identifies the *bytes* of a
recording: the fixed EDF header is stripped of patient identification and the
annotation text inside the signal file is replaced by timekeeping records. The
same annotations also exist as rows, and a surface that serialises them without
consulting this module hands back in JSON exactly what the pipeline removed from
the file. That is invisible in a byte-level test, which is why the decision lives
in one place and the serialisers take the answer as a required argument.

See AGENTS.md → *Annotation text follows ``apply_middleware``*. Contract tests are in
``annotations/tests/test_text_redaction.py`` for the local surfaces and in
``recordings/tests/test_federation.py`` for the federated half of the time-slice
endpoint, where the peer-impersonation helpers live.

The rule
--------
A caller whose read terms carry ``apply_middleware`` receives no annotation text
they did not write: ``Event.name`` / ``value``, ``Label.name`` / ``value`` and
``Annotation.content``. Timing, hashes, author ids, interruptions and
classification codes are unaffected — they carry no free text from the source.

Two exemptions, both narrow:

* **The caller's own rows.** A rater annotating a recording they reach under a
  de-identifying grant must keep reading their own work; their text never came
  from the source file.
* **Machine-produced rows**, declared through :func:`register_exempt_rows`. A
  detector's findings are a function of the (already de-identified) signal, not a
  transcription of anything a clinician typed, and withholding them would leave a
  grantee an analysis view with no findings in it. The registry exists so this
  module does not import the apps that produce such rows: ``compute`` owns the
  provenance link and registers from its ``AppConfig.ready``.

A federated peer authors nothing locally, so under a sanitising grant every row it
did not produce is withheld from it. The machine-produced exemption does not depend
on the caller: a finding is computed from the de-identified signal whoever asks for
it, so a peer receives those as a local grantee would.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

#: Callables that answer "which of these rows did a machine produce". Each takes
#: the sequence of rows being serialised and returns the primary keys among them
#: that are exempt. Registered from an owning ``AppConfig.ready``.
_EXEMPT_ROW_PROVIDERS: list[Callable[[Sequence[Any]], set]] = []


def register_exempt_rows(provider: Callable[[Sequence[Any]], set]) -> None:
    """Register a provider of machine-produced row ids.

    ``provider`` receives the rows about to be serialised — all of one concrete
    model — and returns the subset of their primary keys whose text is a machine's
    output rather than a person's. It must tolerate rows of a model it knows
    nothing about by returning an empty set, since every annotation type passes
    through it.
    """
    _EXEMPT_ROW_PROVIDERS.append(provider)


def _exempt_ids(rows: Sequence[Any]) -> set:
    """Union of every registered provider's answer for *rows*."""
    exempt: set = set()
    for provider in _EXEMPT_ROW_PROVIDERS:
        exempt |= provider(rows)
    return exempt


def withheld_row_ids(rows: Iterable[Any], *, caller: Any, terms: Any) -> set:
    """Return the primary keys of *rows* whose text must not reach this caller.

    ``terms`` is the :class:`~epicurrents.permissions.ReadAccessTerms` resolved for
    the caller against the annotations' target. ``caller`` is the local user, or
    ``None`` for a federated peer or an anonymous share-token reader, neither of
    which can own a row.

    Returns an empty set when the terms do not ask for de-identification, which is
    the common case: a caller reading their own or a raw-granted object sees
    everything.
    """
    if not getattr(terms, "apply_middleware", False):
        return set()

    rows = list(rows)
    if not rows:
        return set()

    caller_id = getattr(caller, "pk", None) if caller is not None else None
    exempt = _exempt_ids(rows)
    return {row.pk for row in rows if row.pk not in exempt and row.author_id != caller_id}
