"""Recording pipeline configuration and format-converter registry.

A ``RecordingPipeline`` is a named set of processing options applied to an EDF/BDF
file at ingest time. Two built-in pipelines are defined:

``"web"``
    Applied to files uploaded through the web API.
``"import"``
    Applied to files ingested via the ``import_recordings`` management command.

Override or extend via the ``RECORDING_PIPELINES`` setting::

    RECORDING_PIPELINES = {
        # Override a built-in:
        "web": {"header": {"strip_annotation_text": True}},
        # Add a custom pipeline using a dotted import path to a RecordingPipeline
        # instance or a zero-argument factory callable:
        "research": "mysite.pipelines.research_pipeline",
    }

Format converters
-----------------

A *converter* is a callable with the signature::

    (input_path: Path, output_dir: Path) -> Path | tuple[Path, dict | None]

It receives a source file and an empty temporary directory, converts the file
to EDF, and returns either the EDF path alone or a two-tuple
``(edf_path, sidecar_data)`` where *sidecar_data* is an optional dict of
structured event/metadata extracted during conversion (saved as a "Source
events" annotation when present).

The one built-in converter maps ``.csv`` (tabular signal data) to EDF. The
platform carries no vendor-specific conversion code: a converter for a
proprietary format is a separate program the deployment describes here, run
through :mod:`recordings.converters.command`. Override or extend via
``RECORDING_CONVERTERS``::

    RECORDING_CONVERTERS = {
        # Disable the built-in .csv converter:
        ".csv": None,
        # An in-process converter for another format:
        ".ncs": "mysite.converters.ncs.convert",
        # An external program, described rather than imported:
        ".e": {
            "command": ["{python}", "-m", "nicolet_e2edf.nicolet.cli",
                        "--in", "{input}", "--out", "{output}", "--json-sidecar"],
            "requires": "nicolet_e2edf",
        },
    }

Registering here is the only step: ``converter_extensions()`` reports the
registered set, ``converter_extensions(available_only=True)`` narrows it to the
converters actually installed, and ``import_recordings`` asks for the latter
rather than keeping a list of its own.

Pre / post conversion hooks
---------------------------

Three extension points fire around the converter call inside
``recordings.tasks.process_recording``:

* ``pre_convert(recording, source_path, ext)`` — fires before the converter
  runs. ``source_path`` is the as-uploaded file (still at its original format).
  Use for preservation of the source bytes, pre-validation, or provenance
  logging before any transformation.

* ``post_convert(recording, source_path, converted_path, sidecar_data)`` —
  fires after the converter succeeds, before the source file is removed.
  ``source_path`` still exists at this point; the converted file is at
  ``converted_path``. Use for sidecar parsing keyed on the converter
  (``recordings.converters.sidecar`` is the canonical worked
  example), source-to-converted mapping records, or post-conversion
  validation.

* ``convert_failed(recording, source_path, exception)`` — fires when the
  converter raises, before the exception re-raises and the ingest task
  aborts cleanup. ``source_path`` still points at the as-uploaded file at
  this point. Use for preservation of the source on conversion failure
  (closes the Phase 3 ``"failed"`` preservation gap for converter-bound
  formats) or for incident logging.

Register from your app's ``ready()`` method to avoid import-time circular
references::

    from recordings.pipelines import register_pre_convert
    register_pre_convert(my_handler, fail_mode="soft")

Handler failure modes:

* ``"soft"`` (default) — exceptions raised by the handler are caught and
  logged; ingest continues. Use for preservation, archival, provenance, and
  other observers that should never block the upload.
* ``"hard"`` — exceptions raised by the handler propagate; ingest aborts
  and the recording row is deleted via the outer task error path. Use for
  pre-validation that must refuse the upload.
"""

from __future__ import annotations

import copy
import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class HeaderPipelineOptions:
    """Options controlling EDF/BDF header-level processing at ingest."""

    # When True, text TALs are stripped from EDF+/BDF+ annotation channels and
    # only the mandatory timekeeping TALs are preserved in the stored file.
    # Annotation text is always extracted and stored in the database regardless
    # of this setting.  Defaults to True so that stored files are anonymised by
    # default; set to False only when the original annotations must be kept in
    # the file (e.g. research pipelines where the caller explicitly opts in).
    strip_annotation_text: bool = True


@dataclass
class SignalPipelineOptions:
    """Options controlling signal-level processing at ingest.

    Reserved for future use (e.g. channel filtering, downsampling).
    """


@dataclass
class RecordingPipeline:
    """Complete set of ingest-time processing options for a recording."""

    header: HeaderPipelineOptions = field(default_factory=HeaderPipelineOptions)
    signals: SignalPipelineOptions = field(default_factory=SignalPipelineOptions)


# Built-in pipelines — always available, can be overridden via RECORDING_PIPELINES.
_BUILTIN_PIPELINES: dict[str, RecordingPipeline] = {
    "web": RecordingPipeline(),
    "import": RecordingPipeline(),
}

# Built-in converters — keyed on lower-case file extension with leading dot.
# Values are dotted import paths; resolved lazily so the converter package is
# not imported until actually needed.
_BUILTIN_CONVERTERS: dict[str, str] = {
    ".csv": "recordings.converters.csv2edf.convert",
}


def get_pipeline(label: str) -> RecordingPipeline:
    """Return a :class:`RecordingPipeline` for the given label.

    Resolution order:

    1. ``RECORDING_PIPELINES`` setting (if defined) — overrides built-ins and
       allows adding new labels.
    2. Built-in pipelines (``"web"``, ``"import"``).

    Values in ``RECORDING_PIPELINES`` may be:

    * A **dict** with optional ``"header"`` and ``"signals"`` sub-dicts whose
      keys map to the fields of :class:`HeaderPipelineOptions` /
      :class:`SignalPipelineOptions`.
    * A **dotted string** import path pointing to a :class:`RecordingPipeline`
      instance or a zero-argument callable that returns one. A path resolving to
      a module-level instance is shared exactly as the next bullet describes;
      point it at a factory to get a fresh object per call.
    * A :class:`RecordingPipeline` instance directly. Returned as given rather
      than copied — unlike the built-ins, which are copied so that a caller's
      per-run tuning cannot outlive the run. An instance shared through this
      setting is shared for real, and mutating what you are handed will be seen
      by the next caller.

    Raises :exc:`ValueError` if the label is not found.
    """
    from django.conf import settings

    configured: dict = getattr(settings, "RECORDING_PIPELINES", {})

    if label in configured:
        value = configured[label]
    elif label in _BUILTIN_PIPELINES:
        # A copy, because these are module-level singletons and callers tune
        # what they are given. The ingest task sets
        # ``header.strip_annotation_text = False`` when an upload asks for its
        # annotations preserved, and a Celery worker is long-lived — returning
        # the singleton meant one such upload flipped the shared "web" pipeline
        # and every later recording that worker processed kept its embedded
        # annotation text. That is clinical free text the platform otherwise
        # strips, on recordings that never asked for it, with nothing recording
        # that they had inherited it.
        #
        # Only the built-ins are copied. A pipeline an operator puts in
        # RECORDING_PIPELINES is returned as given: they supplied the object and
        # may have reasons for its identity, and deep-copying something this
        # module knows nothing about can fail on whatever it holds. The
        # docstring says so, so a project sharing one is doing it knowingly.
        return copy.deepcopy(_BUILTIN_PIPELINES[label])
    else:
        raise ValueError(
            f"Unknown recording pipeline label: {label!r}. "
            f"Define it in the RECORDING_PIPELINES setting or choose one of the "
            f"built-ins: {sorted(_BUILTIN_PIPELINES)!r}."
        )

    # Dotted import path
    if isinstance(value, str):
        module_path, attr = value.rsplit(".", 1)
        module = importlib.import_module(module_path)
        obj = getattr(module, attr)
        return obj() if callable(obj) and not isinstance(obj, RecordingPipeline) else obj

    # Dict of sub-options
    if isinstance(value, dict):
        header = HeaderPipelineOptions(**value.get("header", {}))
        signals = SignalPipelineOptions(**value.get("signals", {}))
        return RecordingPipeline(header=header, signals=signals)

    # Already a RecordingPipeline instance
    return value


def normalise_extension(ext: str) -> str:
    """Return *ext* lower-cased with a leading dot, the form converter keys are stored in."""
    return ext.lower() if ext.startswith(".") else f".{ext.lower()}"


def converter_extensions(*, available_only: bool = False) -> set[str]:
    """Return every file extension a converter is currently registered for.

    The union of the built-in registry and ``RECORDING_CONVERTERS``, minus any extension the
    setting disables with a ``None`` value — the same resolution :func:`get_converter` applies,
    expressed as a set so a caller can ask *which* files are convertible rather than probing
    one extension at a time. With *available_only*, converters whose program is not
    installed are left out as well — what a deployment can run, rather than what it has
    declared.

    Resolve this at call time rather than at import. ``RECORDING_CONVERTERS`` is assembled by
    the plugin loader from ``common < plugins < project < .env``, so a set captured at module
    import would miss every converter a project or plugin registers.
    """
    from django.conf import settings

    configured = getattr(settings, "RECORDING_CONVERTERS", {})
    extensions = set(_BUILTIN_CONVERTERS)
    for key, value in configured.items():
        normalised = normalise_extension(key)
        if value is None:
            extensions.discard(normalised)
        else:
            extensions.add(normalised)
    if not available_only:
        return extensions
    return {extension for extension in extensions if converter_available(extension)}


def converter_available(ext: str) -> bool:
    """Report whether the converter registered for *ext* can actually run.

    An external converter is a separate program a deployment installs, so registration and
    installation are two acts and either can happen without the other. A registered but
    absent converter would otherwise be collected by file discovery and fail on every file
    — the shape of drift this registry exists to prevent.

    Only a command spec can answer; a dotted-path converter is assumed available, since it
    was resolved by importing it.
    """
    from django.conf import settings

    from recordings.converters import command

    key = normalise_extension(ext)
    value = getattr(settings, "RECORDING_CONVERTERS", {}).get(key)
    if isinstance(value, dict):
        return command.is_available(value)
    try:
        return get_converter(key) is not None
    except (ImportError, AttributeError) as error:
        # A dotted path that does not resolve is a mis-registration, not a reason to abort
        # a bulk import: discovery leaves the extension alone and reports it, and an upload
        # of that format still fails loudly through get_converter.
        logger.warning("Converter registered for %s cannot be imported: %s", key, error)
        return False


def get_converter(ext: str):
    """Return a converter callable for *ext*, or ``None`` if none is registered.

    Resolution order:

    1. ``RECORDING_CONVERTERS`` setting (if defined) — overrides built-ins.
       A ``None`` value explicitly disables a built-in converter.
    2. Built-in converters, listed in ``_BUILTIN_CONVERTERS``.

    *ext* is normalised to lower-case with a leading dot before lookup.

    The returned callable must accept ``(input_path: Path, output_dir: Path)``
    and return either the converted EDF :class:`~pathlib.Path` alone, or a
    two-tuple ``(edf_path, sidecar_data: dict | None)``.
    """
    from django.conf import settings

    key = normalise_extension(ext)

    configured: dict = getattr(settings, "RECORDING_CONVERTERS", {})
    if key in configured:
        value = configured[key]
    elif key in _BUILTIN_CONVERTERS:
        value = _BUILTIN_CONVERTERS[key]
    else:
        return None

    if value is None:
        return None

    if isinstance(value, dict):
        # A command spec: an external program the deployment describes rather than a
        # converter the platform imports. See recordings.converters.command.
        from recordings.converters import command

        return command.build(value)

    if isinstance(value, str):
        module_path, attr = value.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, attr)

    return value


# ---------------------------------------------------------------------------
# Pre / post conversion hook registries
# ---------------------------------------------------------------------------

_FailMode = Literal["soft", "hard"]

# Each registry holds (handler, fail_mode) tuples in registration order.
# Cleared between tests via the ``reset_convert_hooks`` helper below.
_PRE_CONVERT_HANDLERS: list[tuple[Callable, _FailMode]] = []
_POST_CONVERT_HANDLERS: list[tuple[Callable, _FailMode]] = []
_CONVERT_FAILED_HANDLERS: list[tuple[Callable, _FailMode]] = []


def _register(registry: list, handler: Callable, fail_mode: _FailMode) -> None:
    if fail_mode not in ("soft", "hard"):
        raise ValueError(f"fail_mode must be 'soft' or 'hard', got {fail_mode!r}")
    # Idempotent — registering the same handler twice has no effect, even if
    # the fail_mode differs; the first registration wins.
    if any(existing is handler for existing, _ in registry):
        return
    registry.append((handler, fail_mode))


def register_pre_convert(handler: Callable, *, fail_mode: _FailMode = "soft") -> None:
    """Register a handler to fire before the converter runs.

    Signature: ``handler(recording, source_path: Path, ext: str) -> None``.

    See the module docstring for hook semantics and ``fail_mode`` behaviour.
    """
    _register(_PRE_CONVERT_HANDLERS, handler, fail_mode)


def register_post_convert(handler: Callable, *, fail_mode: _FailMode = "soft") -> None:
    """Register a handler to fire after a successful conversion.

    Signature:
    ``handler(recording, source_path: Path, converted_path: Path,
    sidecar_data: dict | None) -> None``.

    See the module docstring for hook semantics and ``fail_mode`` behaviour.
    """
    _register(_POST_CONVERT_HANDLERS, handler, fail_mode)


def register_convert_failed(handler: Callable, *, fail_mode: _FailMode = "soft") -> None:
    """Register a handler to fire when the converter raises.

    Signature:
    ``handler(recording, source_path: Path, exception: BaseException) -> None``.

    See the module docstring for hook semantics and ``fail_mode`` behaviour.
    The original converter exception always re-raises after the handlers
    run; ``"hard"`` mode means a handler exception is raised in place of
    the original, which is rarely the right choice here.
    """
    _register(_CONVERT_FAILED_HANDLERS, handler, fail_mode)


def _dispatch(registry: list, args: tuple, hook_name: str) -> None:
    """Call each registered handler with ``*args``, honouring fail_mode."""
    for handler, fail_mode in registry:
        try:
            handler(*args)
        except Exception as exc:
            if fail_mode == "hard":
                raise
            logger.warning(
                "%s handler %r raised (soft mode, ingest continues): %s",
                hook_name,
                getattr(handler, "__qualname__", handler),
                exc,
                exc_info=True,
            )


def dispatch_pre_convert(recording, source_path, ext: str) -> None:
    _dispatch(_PRE_CONVERT_HANDLERS, (recording, source_path, ext), "pre_convert")


def dispatch_post_convert(recording, source_path, converted_path, sidecar_data) -> None:
    _dispatch(
        _POST_CONVERT_HANDLERS,
        (recording, source_path, converted_path, sidecar_data),
        "post_convert",
    )


def dispatch_convert_failed(recording, source_path, exception) -> None:
    _dispatch(
        _CONVERT_FAILED_HANDLERS,
        (recording, source_path, exception),
        "convert_failed",
    )


def reset_convert_hooks() -> None:
    """Empty all three hook registries.

    Test-only utility; production code never calls this. Pytest fixtures
    use it to ensure handler state doesn't leak between tests.
    """
    _PRE_CONVERT_HANDLERS.clear()
    _POST_CONVERT_HANDLERS.clear()
    _CONVERT_FAILED_HANDLERS.clear()
