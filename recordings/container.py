"""The container the viewer's EDF export produces: a plain EDF with a JSON footer appended.

The viewer converts a recording in the browser and hands the platform one file. The signal part is EDF with the
EDF+ header conventions for its identification fields and no annotation channel, and everything the file would
otherwise carry as annotation records rides in a footer after the last data record: the events with the codes of
the platform's own vocabularies, the interruptions, the labels, the channel descriptions and the subject fields,
in the sidecar shape the viewer's ``EdfEncoder`` defines. The reserved field of the header marks the container:

    <EDF|BDF>[+C|+D] EC:<byte size of header and data records>:<footer size in whole KiB>

The container is transport only. Ingest detaches the footer before the EDF processor sees the file, so the stored
recording is the EDF alone with a standard reserved field, and the footer's contents become rows: one ``Event``
per event through ``recordings.event_translation``, resolved from the code the event declares and falling back to
the mappers and tables for one that declares none, one ``Interruption`` per interruption, the raw record, and
one ``Label`` per label that declares a code of a registered vocabulary. A file whose reserved field carries no
marker is left alone, so an ordinary EDF upload never reaches this module's writes. The footer's channel
descriptions and its subject fields are read by nothing here: the channel block is de-identified from the file
itself, and identification has no place on the platform. A label without a code is not stored either, since what
it carries is text.

The footer is the viewer's sidecar appended to the file, and a pooled submission sends the same sidecar beside a
plain EDF. Both reach the same pool, the upload through a release run and the submission through the pooled ingest,
so both are written by :func:`save_viewer_sidecar`. The pooled ingest always discards what carries the file's text;
the upload path follows ``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS``, which a release-gated deployment must set
(``library.E002``), so every recording that can reach a pool arrives in the same shape.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from recordings.event_translation import SourceEvent, annotation_hash, write_source_events
from recordings.processors.edf import EdfParseError, parse_edf_header, parse_signal_infos

logger = logging.getLogger(__name__)

#: The byte offset and width of the reserved field in the fixed header.
_RESERVED_OFFSET = 192
_RESERVED_WIDTH = 44
_FIXED_HEADER = 256
_SIGNAL_HEADER = 256

#: The container marker, as the viewer's encoder writes it into the reserved field.
_MARKER = re.compile(r"^(?P<format>EDF|BDF)(?P<plus>\+[CD])?\s+EC:(?P<total>\d+):(?P<kib>\d+)$")

#: The standards a footer event may declare a term of. The term itself says which one owns it.
_DECLARING_STANDARDS = ("epicurrents.eeg", "epicurrents.biosignal")


class ContainerError(EdfParseError):
    """The reserved field names a footer the file does not carry, or the footer is not the JSON object it claims."""


def read_container_marker(data: bytes) -> tuple[str, int, int] | None:
    """The marker in *data*'s reserved field, as ``(reserved_without_marker, total_bytes, footer_kib)``, or ``None``.

    *data* is at least the fixed header. The first element is what the reserved field says once the marker is
    removed, ``"EDF"``, ``"EDF+D"`` and so on, which is what the field is rewritten to when the footer is detached.
    """
    if len(data) < _FIXED_HEADER:
        return None
    reserved = data[_RESERVED_OFFSET : _RESERVED_OFFSET + _RESERVED_WIDTH].decode("ascii", errors="replace").strip()
    match = _MARKER.match(reserved)
    if match is None:
        return None
    plain = match.group("format") + (match.group("plus") or "")
    return plain, int(match.group("total")), int(match.group("kib"))


def is_container(path: Path) -> bool:
    """True when the file at *path* carries the container marker."""
    with path.open("rb") as handle:
        return read_container_marker(handle.read(_FIXED_HEADER)) is not None


def detach_footer(path: Path) -> dict | None:
    """Detach the footer of the container at *path* in place and return it, or ``None`` when the file carries none.

    The file is truncated to the size the marker states and the reserved field is rewritten to the standard EDF
    form, so what remains is an ordinary EDF: a plain one gets the blank field the specification asks for, an EDF+
    one keeps its continuity marker. The marker is checked against the file and against the header's own geometry
    before anything is written: the recording it names must be exactly the header record and the data records the
    header describes, and the footer must end where the file does. Raises :class:`ContainerError` otherwise, or
    when the footer does not parse as a JSON object, since a file that claims to be a container and is not one is
    not a recording to store; a header that does not parse raises as it would for any EDF.
    """
    with path.open("rb") as handle:
        head = handle.read(_FIXED_HEADER)
    marker = read_container_marker(head)
    if marker is None:
        return None
    plain, total, kib = marker
    if kib < 1:
        raise ContainerError("container marker names a zero-length footer")
    size = path.stat().st_size
    footer_end = total + kib * 1024
    if footer_end != size:
        raise ContainerError(f"container marker names {footer_end} bytes but the file holds {size}")
    header = parse_edf_header(head)
    with path.open("rb") as handle:
        header_record = handle.read(_FIXED_HEADER + header.signal_count * _SIGNAL_HEADER)
    parse_signal_infos(header_record, header)
    described = header.header_record_bytes + header.data_record_count * header.record_byte_size
    if header.data_record_count < 0 or total != described:
        raise ContainerError(f"container marker names {total} bytes of recording but the header describes {described}")
    with path.open("rb") as handle:
        handle.seek(total)
        raw = handle.read(kib * 1024)
    try:
        footer = json.loads(raw.rstrip(b"\x00").decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError included; the message names a position, never content.
        raise ContainerError(f"container footer is not JSON: {exc}") from exc
    if not isinstance(footer, dict):
        raise ContainerError("container footer is not a JSON object")
    # The EDF specification leaves the reserved field blank for a plain EDF; "EDF" alone is not a value it defines.
    reserved = "" if plain in ("EDF", "BDF") else plain
    # Truncate before rewriting the field: should the rewrite then fail, the marker still names a footer the file
    # no longer holds and a retry refuses the file, rather than storing the footer as trailing bytes of a plain EDF.
    with path.open("r+b") as handle:
        handle.truncate(total)
        handle.seek(_RESERVED_OFFSET)
        handle.write(reserved.ljust(_RESERVED_WIDTH).encode("ascii"))
    return footer


def _number(value: Any) -> float | None:
    """*value* as a float when it is a number and not a bool, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def footer_events(footer: dict) -> list[SourceEvent]:
    """The footer's ``events`` as source events, or ``ValueError`` naming the first item that is not one.

    An event's ``value`` is the text the viewer shows and is what a table matches; ``label`` stands in when the value
    is not a string. A ``comment`` is a text annotation to the translation, any other class a typed event, so the
    placeholder for an untranslated one is named by that kind. A zero duration is an instant event. The code is the
    one the event declares under either of the platform's own standards.
    """
    events = footer.get("events")
    if events is None:
        return []
    if not isinstance(events, list):
        raise ValueError('footer "events" is not a list')  # noqa: TRY004 — one type per violation
    sources: list[SourceEvent] = []
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"footer events[{index}] is not an object")  # noqa: TRY004 — one type per violation
        start = _number(item.get("start"))
        if start is None:
            raise ValueError(f"footer events[{index}] has no numeric start")
        duration = _number(item.get("duration"))
        event_class = _text(item, "class")
        codes = item.get("codes")
        code = ""
        if isinstance(codes, dict):
            for standard in _DECLARING_STANDARDS:
                declared = codes.get(standard)
                if isinstance(declared, str) and declared:
                    code = declared
                    break
        sources.append(
            SourceEvent(
                onset=start,
                duration=duration if duration else None,
                label=_text(item, "value") or _text(item, "label"),
                type="" if event_class in ("", "comment") else event_class,
                code=code,
            )
        )
    return sources


def footer_interruptions(footer: dict) -> list[tuple[float, float]]:
    """The footer's ``interruptions`` as ``(start, duration)`` pairs in data time, or ``ValueError``."""
    items = footer.get("interruptions")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError('footer "interruptions" is not a list')  # noqa: TRY004 — one type per violation
    pairs: list[tuple[float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"footer interruptions[{index}] is not a [start, duration] pair")
        start, duration = _number(item[0]), _number(item[1])
        if start is None or duration is None or start < 0 or duration <= 0:
            raise ValueError(f"footer interruptions[{index}] is not a non-negative start and a positive duration")
        pairs.append((start, duration))
    return pairs


def footer_carries_events(footer: dict | None) -> bool:
    """True when *footer* is a footer with at least one well-formed event, so that its events own the rows."""
    if not isinstance(footer, dict):
        return False
    try:
        return bool(footer_events(footer))
    except ValueError:
        return False


def save_footer_events(recording, footer: dict, *, discard_text: bool | None = None) -> None:
    """Write the footer's events and interruptions as rows on *recording*, and the raw record of both.

    Nothing else in the footer is stored here: not an event's free text beyond the value the raw record keeps, not
    its subject fields; the labels are :func:`save_footer_labels`'s. Raises ``ValueError`` on a footer whose events or
    interruptions are not the shape the viewer writes. Under ``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS`` the raw
    record is not written and the placeholders are skipped by the writer; *discard_text* set to a bool decides it
    instead of the setting. The interruptions are written regardless, as the EDF+ seam does, because a gap is geometry
    rather than annotation.
    """
    from django.conf import settings
    from django.contrib.contenttypes.models import ContentType

    from annotations.models import Annotation, Interruption
    from epicurrents.system_user import get_system_user

    sources = footer_events(footer)
    interruptions = footer_interruptions(footer)
    if not sources and not interruptions:
        return
    sources.sort(key=lambda source: source.onset)

    system_user = get_system_user()
    recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    for index, (start, duration) in enumerate(interruptions):
        Interruption.objects.create(
            author=system_user,
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash=annotation_hash(recording.pk, f"footer-interruption:{index}"),
            timestamp=start,
            duration=duration,
        )

    discard = (
        getattr(settings, "RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS", False) if discard_text is None else discard_text
    )
    write_source_events(recording, sources, hash_prefix="footer-event", discard_text=discard)

    if discard:
        return

    content: dict = {}
    if sources:
        content["events"] = [
            {
                "onset": source.onset,
                "duration": source.duration,
                "label": ": ".join(part for part in (source.type, source.label) if part),
            }
            for source in sources
        ]
    if interruptions:
        content["interruptions"] = [{"onset": start, "duration": duration} for start, duration in interruptions]
    Annotation.objects.create(
        author=system_user,
        name="Source events",
        target_content_type=recording_ct,
        target_object_id=str(recording.pk),
        object_hash=annotation_hash(recording.pk, "footer-events"),
        content=content,
    )


def footer_label_codes(footer: dict) -> list[list[tuple[str, str]]]:
    """The codes each of the footer's ``labels`` declares, as ``(standard, value)`` pairs in the order declared.

    One list per label, in footer order, empty for a label that declares none; a code whose value is not a non-empty
    string is no code, as for an event. Nothing else of a label is read. Raises ``ValueError`` naming the first item
    that is not the shape the viewer writes.
    """
    labels = footer.get("labels")
    if labels is None:
        return []
    if not isinstance(labels, list):
        raise ValueError('footer "labels" is not a list')  # noqa: TRY004 — one type per violation
    declared: list[list[tuple[str, str]]] = []
    for index, item in enumerate(labels):
        if not isinstance(item, dict):
            raise ValueError(f"footer labels[{index}] is not an object")  # noqa: TRY004 — one type per violation
        codes = item.get("codes")
        if codes is not None and not isinstance(codes, dict):
            raise ValueError(f'footer labels[{index}] "codes" is not an object')
        declared.append(
            [
                (standard, value)
                for standard, value in (codes or {}).items()
                if isinstance(standard, str) and standard and isinstance(value, str) and value
            ]
        )
    return declared


def _accepted_code(standard: str, value: str):
    """The vocabulary registered for *standard* when it accepts *value*, else ``None``: the registry, fail-closed.

    Unlike the API's ``validate_code``, an unregistered standard is refused whatever the strict-vocabulary setting
    says, because a code nobody validates is text a file supplied.
    """
    from annotations.vocabularies import get_vocabulary

    vocabulary = get_vocabulary(standard)
    if vocabulary is None:
        return None
    try:
        vocabulary.validator(value, None)
    except Exception:  # a validator is project code, so any failure of it refuses the code
        return None
    return vocabulary


def _term_name(vocabulary, value: str) -> str | None:
    """The display name *vocabulary* gives *value*, or ``None`` when it gives none or its lookup fails."""
    if vocabulary.term_name is None:
        return None
    try:
        name = vocabulary.term_name(value)
    except Exception:  # project code, as for the validator; the code stands in for the name
        return None
    return name if isinstance(name, str) and name else None


def save_footer_labels(recording, footer: dict) -> tuple[int, int]:
    """Write one system-authored ``Label`` on *recording* per footer label declaring an accepted code.

    A code is accepted when a vocabulary is registered for its standard and that vocabulary's validator accepts the
    value. The label's accepted codes become its ``Code`` rows; its ``name`` is the display name the first accepted
    code's vocabulary gives the term, or the code where it gives none, and its ``value`` is that code, never the
    template's own value. A label with no accepted code writes nothing: there is no placeholder, since a label
    records a fact rather than a moment, and nothing of the template's text or annotator is stored anywhere.
    Returns ``(written, skipped)`` and logs the skipped count, never a value. Raises ``ValueError`` as
    :func:`footer_label_codes` does, before any row is written.
    """
    from django.contrib.contenttypes.models import ContentType

    from annotations.models import Code, Label
    from epicurrents.system_user import get_system_user

    declared = footer_label_codes(footer)
    written = skipped = 0
    system_user = None
    recording_ct = label_ct = None
    for index, codes in enumerate(declared):
        accepted = [
            (standard, value, vocabulary)
            for standard, value in codes
            if (vocabulary := _accepted_code(standard, value))
        ]
        if not accepted:
            skipped += 1
            continue
        if system_user is None:
            system_user = get_system_user()
            recording_ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
            label_ct = ContentType.objects.get_for_model(Label)
        _, first_value, first_vocabulary = accepted[0]
        name = _term_name(first_vocabulary, first_value) or first_value
        label = Label.objects.create(
            author=system_user,
            target_content_type=recording_ct,
            target_object_id=str(recording.pk),
            object_hash=annotation_hash(recording.pk, f"footer-label:{index}"),
            name=name[:255],
            value=first_value,
        )
        for standard, value, _ in accepted:
            Code.objects.create(content_type=label_ct, object_id=str(label.pk), standard=standard, value=value)
        written += 1
    if skipped:
        logger.info(
            "save_footer_labels: %d of %d labels of recording %s declared no accepted code and were not stored",
            skipped,
            len(declared),
            recording.pk,
        )
    return written, skipped


def check_viewer_sidecar(document: Any) -> None:
    """Raise ``ValueError`` naming the first part of *document* that is not the viewer's sidecar shape.

    The events, the interruptions and the labels are checked; every other key is left to whoever reads it. The
    message names a position, never a value, so it can be shown and logged.
    """
    if not isinstance(document, dict):
        raise ValueError("the sidecar is not a JSON object")  # noqa: TRY004 — one type per violation
    footer_events(document)
    footer_interruptions(document)
    footer_label_codes(document)


def save_viewer_sidecar(recording, document: dict, *, discard_text: bool | None = None) -> None:
    """Write the rows of the viewer's sidecar on *recording*: its events, interruptions and coded labels.

    One writer for both ways a viewer export reaches the platform, the footer of an uploaded container and the
    sidecar of a pooled submission. The whole document is checked before anything is written, so a malformed one
    writes nothing and raises ``ValueError``. *discard_text* set to a bool decides whether the placeholders and the
    raw record are skipped, instead of ``RECORDINGS_DISCARD_EMBEDDED_ANNOTATIONS``; the coded labels carry no text
    and are written either way.
    """
    check_viewer_sidecar(document)
    save_footer_events(recording, document, discard_text=discard_text)
    save_footer_labels(recording, document)
