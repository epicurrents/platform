"""Tests for the template's EDF header middleware — the middleware half of the extension contract.

``InstitutionWatermarkMiddleware`` demonstrates ``EDFHeaderMiddleware`` subclassing; what these
tests pin is the contract every header middleware must uphold: the transform is isometric, it
touches only its declared field, and malformed input passes through unchanged rather than raising
into the serving path.
"""

import pytest

from projects.example.middleware import (
    _LOCAL_RECORDING_LENGTH,
    _LOCAL_RECORDING_OFFSET,
    InstitutionWatermarkMiddleware,
)


def _header(signal_count: int = 1) -> bytes:
    """A syntactically plausible EDF fixed header: 256 * (1 + signal_count) space-padded bytes."""
    return b" " * (256 * (1 + signal_count))


class TestInstitutionWatermark:
    def test_transform_is_isometric(self, settings):
        settings.EXAMPLE_INSTITUTION_NAME = "Test Clinic"
        raw = _header()
        out = InstitutionWatermarkMiddleware().transform_header(raw)
        assert len(out) == len(raw)

    def test_institution_is_stamped_into_the_recording_field(self, settings):
        settings.EXAMPLE_INSTITUTION_NAME = "Test Clinic"
        out = InstitutionWatermarkMiddleware().transform_header(_header())
        field = out[_LOCAL_RECORDING_OFFSET : _LOCAL_RECORDING_OFFSET + _LOCAL_RECORDING_LENGTH]
        assert field.decode("ascii").rstrip() == "Recorded at Test Clinic"

    def test_bytes_outside_the_field_are_untouched(self, settings):
        settings.EXAMPLE_INSTITUTION_NAME = "Test Clinic"
        raw = bytes(range(256)) * 2
        out = InstitutionWatermarkMiddleware().transform_header(raw)
        assert out[:_LOCAL_RECORDING_OFFSET] == raw[:_LOCAL_RECORDING_OFFSET]
        assert (
            out[_LOCAL_RECORDING_OFFSET + _LOCAL_RECORDING_LENGTH :]
            == raw[_LOCAL_RECORDING_OFFSET + _LOCAL_RECORDING_LENGTH :]
        )

    def test_short_header_passes_through_unchanged(self):
        raw = b"\x00" * 16
        assert InstitutionWatermarkMiddleware().transform_header(raw) == raw

    def test_targets_cover_both_serving_paths(self):
        assert InstitutionWatermarkMiddleware.targets == frozenset({"fuse", "api"})


@pytest.mark.django_db
class TestPipelineIntegration:
    def test_middleware_composes_in_a_pipeline(self, settings):
        # The registration site in apps.py is a placeholder until the extension
        # registry exists, so prove composability the way a project's custom
        # RECORDING_PIPELINES factory would use it.
        from federation.middleware import MiddlewarePipeline

        settings.EXAMPLE_INSTITUTION_NAME = "Test Clinic"
        pipeline = MiddlewarePipeline([InstitutionWatermarkMiddleware()])
        assert pipeline.is_isometric
        out = pipeline.for_scope("api").apply_header(_header())
        assert b"Recorded at Test Clinic" in out
