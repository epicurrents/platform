"""Contract tests for the pre / post / failed conversion hook protocol.

The protocol — three registries plus three dispatch helpers — lives in
``recordings/pipelines.py``. The dispatch calls are wired into
``process_recording`` at the three documented points (before converter,
inside converter except, after successful conversion + move).

These tests cover both the protocol surface in isolation and the
end-to-end wiring through ``process_recording`` using a stub converter.
"""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import override_settings

from recordings.models import Recording
from recordings.pipelines import (
    dispatch_convert_failed,
    dispatch_post_convert,
    dispatch_pre_convert,
    register_convert_failed,
    register_post_convert,
    register_pre_convert,
    reset_convert_hooks,
)
from recordings.tasks import process_recording


@pytest.fixture(autouse=True)
def _clean_hook_registries():
    """Snapshot+restore the three hook registries around every test.

    The registries are module-level lists in ``recordings.pipelines``;
    ``RecordingsConfig.ready()`` registers production handlers
    (preservation stash + convert_failed preservation) into them at app
    startup.  Tests in this file need empty registries to assert ordering
    and idempotency cleanly, so we save the production handlers first,
    clear the registries for the test body, and restore them afterward.
    Without restore, later test files (test_preservation.py) would run
    with the production preservation handlers missing — Phase 3 stash +
    convert_failed preservation would silently no-op.
    """
    from recordings.pipelines import (
        _CONVERT_FAILED_HANDLERS,
        _POST_CONVERT_HANDLERS,
        _PRE_CONVERT_HANDLERS,
    )

    snapshot_pre = list(_PRE_CONVERT_HANDLERS)
    snapshot_post = list(_POST_CONVERT_HANDLERS)
    snapshot_failed = list(_CONVERT_FAILED_HANDLERS)
    reset_convert_hooks()
    try:
        yield
    finally:
        _PRE_CONVERT_HANDLERS[:] = snapshot_pre
        _POST_CONVERT_HANDLERS[:] = snapshot_post
        _CONVERT_FAILED_HANDLERS[:] = snapshot_failed


# ---------------------------------------------------------------------------
# Protocol surface — registries, registration, dispatch, fail_mode
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_pre_convert_handler_fires_on_dispatch(self):
        calls = []

        def handler(recording, source_path, ext):
            calls.append((recording, source_path, ext))

        register_pre_convert(handler)
        dispatch_pre_convert("REC", Path("/tmp/x.e"), ".e")
        assert calls == [("REC", Path("/tmp/x.e"), ".e")]

    def test_post_convert_handler_fires_on_dispatch(self):
        calls = []

        def handler(recording, source_path, converted_path, sidecar_data):
            calls.append((recording, source_path, converted_path, sidecar_data))

        register_post_convert(handler)
        dispatch_post_convert("REC", Path("/tmp/x.e"), Path("/tmp/x.edf"), {"a": 1})
        assert calls == [("REC", Path("/tmp/x.e"), Path("/tmp/x.edf"), {"a": 1})]

    def test_convert_failed_handler_fires_on_dispatch(self):
        calls = []

        def handler(recording, source_path, exc):
            calls.append((recording, source_path, type(exc).__name__))

        register_convert_failed(handler)
        dispatch_convert_failed("REC", Path("/tmp/x.e"), RuntimeError("boom"))
        assert calls == [("REC", Path("/tmp/x.e"), "RuntimeError")]

    def test_registration_is_idempotent(self):
        calls = []

        def handler(*args):
            calls.append(args)

        register_pre_convert(handler)
        register_pre_convert(handler)  # second registration is a no-op
        dispatch_pre_convert("REC", Path("/tmp/x"), ".x")
        assert len(calls) == 1

    def test_handlers_fire_in_registration_order(self):
        order = []

        def first(*args):
            order.append("first")

        def second(*args):
            order.append("second")

        register_pre_convert(first)
        register_pre_convert(second)
        dispatch_pre_convert("REC", Path("/tmp/x"), ".x")
        assert order == ["first", "second"]

    def test_invalid_fail_mode_raises(self):
        with pytest.raises(ValueError, match="fail_mode"):
            register_pre_convert(lambda *a: None, fail_mode="wishful")


class TestFailMode:
    def test_soft_mode_swallows_handler_exception(self, caplog):
        def failing_handler(*args):
            raise RuntimeError("handler-side problem")

        def following_handler(recording, source_path, ext):
            following_handler.called = True

        following_handler.called = False

        register_pre_convert(failing_handler, fail_mode="soft")
        register_pre_convert(following_handler, fail_mode="soft")

        # Should not raise — soft-mode handler exceptions are caught + logged.
        dispatch_pre_convert("REC", Path("/tmp/x"), ".x")

        assert following_handler.called is True
        assert any(
            "handler-side problem" in record.message or "handler-side problem" in (record.exc_text or "")
            for record in caplog.records
        )

    def test_hard_mode_re_raises_handler_exception(self):
        def failing_handler(*args):
            raise RuntimeError("validator rejected upload")

        register_pre_convert(failing_handler, fail_mode="hard")

        with pytest.raises(RuntimeError, match="validator rejected upload"):
            dispatch_pre_convert("REC", Path("/tmp/x"), ".x")


class TestResetHelper:
    def test_reset_empties_all_three_registries(self):
        register_pre_convert(lambda *a: None)
        register_post_convert(lambda *a: None)
        register_convert_failed(lambda *a: None)

        # Sanity — handlers are registered.
        from recordings.pipelines import (
            _CONVERT_FAILED_HANDLERS,
            _POST_CONVERT_HANDLERS,
            _PRE_CONVERT_HANDLERS,
        )

        assert len(_PRE_CONVERT_HANDLERS) == 1
        assert len(_POST_CONVERT_HANDLERS) == 1
        assert len(_CONVERT_FAILED_HANDLERS) == 1

        reset_convert_hooks()

        assert _PRE_CONVERT_HANDLERS == []
        assert _POST_CONVERT_HANDLERS == []
        assert _CONVERT_FAILED_HANDLERS == []


# ---------------------------------------------------------------------------
# End-to-end wiring — process_recording fires the hooks at the right points
# ---------------------------------------------------------------------------


def _make_pending_recording(user, staging_dir, content: bytes, ext: str):
    """Create a Recording in PENDING state with a real staged source file."""
    stem = "ABCDEF01ABCDEF01ABCDEF01ABCDEF01"
    staged_file = staging_dir / f"{stem}{ext}"
    staged_file.write_bytes(content)
    return Recording.objects.create(
        author=user,
        original_name=f"test{ext}",
        stored_name=f"{stem}{ext}",
        file_extension=ext,
        file_size=len(content),
        file_path=str(staged_file),
        file_hash=hashlib.sha256(content).hexdigest(),
        content_hash="",
        status=Recording.Status.PENDING,
    )


@pytest.mark.django_db
class TestProcessRecordingHookWiring:
    """Run process_recording with a stub converter and verify hook ordering."""

    _calls: list[tuple[str, tuple]]

    def _record(self, name):
        def handler(*args):
            self._calls.append((name, args))

        return handler

    def _make_stub_converter(self, output_bytes: bytes, sidecar=None):
        """Return a converter callable that writes ``output_bytes`` to .edf."""

        def convert(source_path: Path, output_dir: Path):
            converted = output_dir / "out.edf"
            converted.write_bytes(output_bytes)
            return (converted, sidecar) if sidecar is not None else converted

        return convert

    def test_pre_and_post_convert_fire_on_success(self, user, tmp_path):
        self._calls = []
        register_pre_convert(self._record("pre"))
        register_post_convert(self._record("post"))
        register_convert_failed(self._record("failed"))

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging, b"<source bytes>", ext=".xyz")

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDING_CONVERTERS={".xyz": "recordings.tests.test_convert_hooks._success_convert"},
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
        ):
            process_recording(recording.pk)

        names = [name for name, _ in self._calls]
        assert names == ["pre", "post"]
        # convert_failed must NOT fire on a successful conversion.
        assert "failed" not in names

        # pre_convert args: (recording, source_path, ext). source_path is the
        # permanent_path (post-move) which still holds the source bytes.
        pre_name, pre_args = self._calls[0]
        assert pre_args[0].pk == recording.pk
        assert pre_args[2] == ".xyz"
        # post_convert args: (recording, source_path, converted_path, sidecar)
        post_name, post_args = self._calls[1]
        assert post_args[0].pk == recording.pk
        assert post_args[2].suffix == ".edf"
        assert post_args[3] is None  # stub converter returned no sidecar

    def test_convert_failed_fires_when_converter_raises(self, user, tmp_path):
        self._calls = []
        register_pre_convert(self._record("pre"))
        register_post_convert(self._record("post"))
        register_convert_failed(self._record("failed"))

        staging = tmp_path / "staging"
        uploads = tmp_path / "uploads"
        staging.mkdir()
        uploads.mkdir()
        recording = _make_pending_recording(user, staging, b"<source bytes>", ext=".xyz")

        with (
            override_settings(
                RECORDINGS_STAGING_PATH=str(staging),
                RECORDINGS_UPLOAD_PATH=str(uploads),
                RECORDING_CONVERTERS={".xyz": "recordings.tests.test_convert_hooks._raising_convert"},
            ),
            patch("notifications.tasks.send_push_to_user.delay"),
            pytest.raises(RuntimeError, match="converter sabotage"),
        ):
            process_recording(recording.pk)

        names = [name for name, _ in self._calls]
        # pre_convert fires first, then convert_failed when the converter
        # raises. post_convert must NOT fire on conversion failure.
        assert names == ["pre", "failed"]

        # convert_failed args: (recording, source_path, exception). The
        # exception passed to the handler is the same one that re-raises.
        _, failed_args = self._calls[1]
        assert failed_args[0].pk == recording.pk
        assert isinstance(failed_args[2], RuntimeError)


# Module-scope converters used as dotted-path values in RECORDING_CONVERTERS
# overrides above. Cannot be defined as fixtures because the settings entry
# is resolved by import path.


def _success_convert(source_path: Path, output_dir: Path):
    """Stub converter that writes a small fake EDF and returns its path."""
    # 256-byte all-blank EDF header is enough to fail format parsing further
    # downstream but lets the converter step itself complete.
    converted = output_dir / "out.edf"
    converted.write_bytes(b" " * 256)
    return converted


def _raising_convert(source_path: Path, output_dir: Path):
    raise RuntimeError("converter sabotage")
