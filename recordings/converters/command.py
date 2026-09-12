"""Running an external command as a format converter.

The platform ships no vendor-specific conversion code. A converter for a proprietary
format is a separate program, developed and licensed on its own terms, and this module is
how one is driven: a deployment describes the command in settings, and the platform runs
it without knowing what format it reads.

    RECORDING_CONVERTERS = {
        ".zip": {
            "command": ["{python}", "-m", "natus2edf", "--in", "{input}",
                        "--out", "{output}", "--json-sidecar"],
            "requires": "natus2edf",
        },
    }

``command`` is substituted and run; ``{python}`` is the interpreter running the platform,
``{input}`` the source file, ``{output}`` a scratch directory the converter writes into.
``requires`` names an importable module whose absence means the converter is not installed,
which lets file discovery leave those files alone instead of collecting them and failing
each one. ``timeout`` bounds the run in seconds, defaulting to an hour.

The contract the command must meet is small: write exactly one ``.edf`` into the output
directory, and, if it emits events, a ``.json`` sidecar beside it under the same stem.
Anything else is refused rather than guessed at — two EDFs means a multi-segment recording
that has to be split deliberately, not reduced to whichever file sorted first.

A subprocess rather than an import, and the licence is the sharpest reason. A converter for
a proprietary format may be licensed on terms the platform must not take on — the Nicolet
one is GPLv3 — and importing it would combine the two, where running it as a separate
program at arm's length does not. Nothing here imports a converter: ``requires`` is checked
with :func:`importlib.util.find_spec`, which locates a module without executing it. The
practical benefits come free with it, since the converter's dependencies cannot collide
with the platform's and a crash in it is an exit status rather than a dead worker.

Nothing raised here names the source file. Converters are given the path and echo it back
in their diagnostics, and an ingest failure is logged with ``exc_info`` into a permanent
stream, so the command's output is scrubbed of the path before it is quoted.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a conversion may run before it is killed, when the spec does not say.
DEFAULT_TIMEOUT_SECONDS = 3600

# Bound on the command's own output quoted back in an exception.
_OUTPUT_EXCERPT = 500

# Keys a command spec may carry.
_REQUIRED_KEYS = {"command"}
_OPTIONAL_KEYS = {"requires", "timeout"}


class CommandConverterError(Exception):
    """Raised when an external converter fails or produces unexpected output."""


def is_available(spec: dict) -> bool:
    """Report whether the program *spec* describes is installed.

    A spec without ``requires`` is assumed available: the platform cannot probe an
    arbitrary executable without running it, and running it to find out would defeat the
    purpose. Naming the module is what makes the check cheap enough to ask on every
    discovery pass.
    """
    module = spec.get("requires")
    if not module:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # A broken or half-installed package resolves to neither present nor importable.
        return False


def build(spec: dict):
    """Return a converter callable for the command *spec*.

    Validates the spec at registration rather than at conversion, so a typo in settings
    surfaces when the registry is first read instead of on the first upload of that format.
    """
    if not isinstance(spec, dict):
        raise CommandConverterError(f"A command converter spec must be a dict, got {type(spec).__name__}.")
    missing = _REQUIRED_KEYS - set(spec)
    if missing:
        raise CommandConverterError(f"Command converter spec is missing {sorted(missing)}.")
    unknown = set(spec) - _REQUIRED_KEYS - _OPTIONAL_KEYS
    if unknown:
        raise CommandConverterError(f"Command converter spec has unknown keys {sorted(unknown)}.")
    command = spec["command"]
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(p, str) for p in command):
        raise CommandConverterError("A command converter spec needs a non-empty list of string arguments.")

    def convert(input_path: Path, output_dir: Path) -> tuple[Path, dict | None]:
        return run_command(spec, Path(input_path), Path(output_dir))

    convert.__doc__ = f"Convert a file by running {command[0]!r}."
    return convert


def run_command(spec: dict, input_path: Path, output_dir: Path) -> tuple[Path, dict | None]:
    """Run the converter described by *spec* and collect what it produced."""
    if not is_available(spec):
        raise CommandConverterError(f"The converter is not installed: no module named {spec.get('requires')!r}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    argv = [_substitute(part, input_path, output_dir) for part in spec["command"]]
    timeout = spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)

    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CommandConverterError(f"The converter did not finish within {timeout} seconds.") from error
    except OSError as error:
        raise CommandConverterError(f"Could not run the converter: {error.strerror or error}") from error

    if result.returncode != 0:
        raise CommandConverterError(f"The converter failed: {_clean_output(result, input_path)}")

    produced = sorted(output_dir.glob("*.edf"))
    if not produced:
        raise CommandConverterError("The converter produced no EDF output.")
    if len(produced) > 1:
        raise CommandConverterError(
            f"The converter produced {len(produced)} EDF files; multi-segment recordings must be split before upload."
        )

    edf_path = produced[0]
    return edf_path, _read_sidecar(edf_path)


def _substitute(part: str, input_path: Path, output_dir: Path) -> str:
    """Fill the placeholders a command spec may use."""
    return (
        part.replace("{python}", sys.executable)
        .replace("{input}", str(input_path))
        .replace("{output}", str(output_dir))
    )


def _read_sidecar(edf_path: Path) -> dict | None:
    """Return the JSON sidecar beside *edf_path*, or ``None``.

    A sidecar that cannot be read costs its events, not the recording: the EDF is already
    written and is the thing being ingested.
    """
    sidecar_path = edf_path.with_suffix(".json")
    if not sidecar_path.exists():
        return None
    try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("command converter: could not read the sidecar: %s", error)
        return None
    return loaded if isinstance(loaded, dict) else None


def _clean_output(result: subprocess.CompletedProcess, input_path: Path) -> str:
    """Return the command's output with the source path removed and its length bounded.

    The converter is handed the path and echoes it back on failure, so quoting its output
    verbatim would put a patient-derived filename into a permanent log.
    """
    text = (result.stderr or result.stdout or "").strip()
    for form in (str(input_path), str(input_path.resolve()), input_path.name, input_path.stem):
        if form:
            text = text.replace(form, "<source>")
    if len(text) > _OUTPUT_EXCERPT:
        text = text[:_OUTPUT_EXCERPT] + "…"
    return text or f"it exited with status {result.returncode}"
