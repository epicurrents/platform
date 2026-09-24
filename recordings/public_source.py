"""The published source a recording may record: a DOI or URL of the dataset its data was taken from.

A recording taken from a published dataset carries a different anonymity argument from one
acquired here. A recipient who can fetch the same data from the publisher gains no means from the
platform's copy (EDPB Guidelines 02/2026 ¶ 26), and the publisher's own anonymity statement is the
document a sharer's contextual assessment naturally adopts. Until now that could be said only in a
grant's ``assessment_reference``; ``Recording.public_source`` says it on the recording, so
``deidentification_report`` and ``grant_assessments`` can report per recording that the data is
already public.

The field takes a locator, not a citation. A DOI or a URL names something anyone can fetch, which
is what "public" has to mean for the argument above to hold; a free-text citation cannot be
checked and is one more channel for a site name. The value is the author's assertion, never the
platform's finding: nothing here resolves the locator or checks what it points at.

Served to every reader of the recording. It names a public dataset, not a person, and a reader
who is told the data is public learns nothing about the subject that the publisher has not
already released.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: Length of ``Recording.public_source``; a DOI or a URL, not a description.
PUBLIC_SOURCE_MAX_LENGTH = 512

_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_DOI_PREFIXES = ("doi:", "https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")

_MESSAGE = "public_source must be a DOI (10.xxxx/...) or an http(s) URL of the published dataset, or empty to clear."


def normalise_public_source(text: str | None) -> str:
    """Return the stored form of a public-source locator, or raise ``ValueError`` naming the problem.

    An empty value clears the field. A DOI, given bare or under a ``doi:`` or ``doi.org`` prefix,
    is stored bare and lower-cased, since DOIs are case-insensitive and one spelling makes two
    recordings from the same dataset comparable. Any other value must be an ``http`` or ``https``
    URL with a host and is stored as given, trimmed.
    """
    value = (text or "").strip()
    if not value:
        return ""
    if len(value) > PUBLIC_SOURCE_MAX_LENGTH:
        raise ValueError(f"public_source may be at most {PUBLIC_SOURCE_MAX_LENGTH} characters.")
    lowered = value.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            candidate = value[len(prefix) :]
            if _DOI.match(candidate):
                return candidate.lower()
            raise ValueError(_MESSAGE)
    if _DOI.match(value):
        return value.lower()
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.netloc:
        return value
    raise ValueError(_MESSAGE)
