"""Contract tests for the serving-pipeline parity rule.

These back the ⚠️ LOAD-BEARING contract on ``_build_serve_pipeline`` in
recordings/api/v1/ninja.py: every endpoint that streams recording bytes
to an ``apply_middleware=True`` caller must serve through the one shared
pipeline, and no serving path may construct its own non-empty
``MiddlewarePipeline``. A path that quietly diverges serves anonymised
headers while leaking clinical annotation text — the exact failure mode
these tests pin down.
"""

import ast
from pathlib import Path

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from epicurrents.models import AccessRight
from recordings.models import Recording, RecordingMeta
from recordings.processors.edf import _parse_tal_record, parse_edf_header
from recordings.tests.test_edf_processor import (
    _make_anno_record,
    _make_edf_header,
    _make_tal,
)

_ANNO_SAMPLE_COUNT = 60
_SIGNALS = [
    {"label": "EEG Fp1", "sample_count": 8},
    {"label": "EDF Annotations", "sample_count": _ANNO_SAMPLE_COUNT},
]


def _build_annotated_edf():
    """Return (header_bytes, eeg_data, content) for a 1-record EDF+C file.

    The annotation channel carries a timekeeping TAL and a text TAL whose
    payload ("spike") stands in for clinical free text.
    """
    header_bytes = _make_edf_header(reserved="EDF+C", signals=_SIGNALS, n_records=1)
    eeg_data = b"\x01\x02" * 8
    anno_bytes = _make_anno_record(
        onset=0.0,
        tals=[_make_tal(0.5, "spike")],
        total_bytes=_ANNO_SAMPLE_COUNT * 2,
    )
    return header_bytes, eeg_data, header_bytes + eeg_data + anno_bytes


def _make_recording_with_meta(user, tmp_path):
    header_bytes, eeg_data, content = _build_annotated_edf()
    edf_path = tmp_path / "parity.edf"
    edf_path.write_bytes(content)

    recording = Recording.objects.create(
        author=user,
        original_name="parity.edf",
        stored_name="PRTY1111PRTY1111PRTY1111PRTY1111.edf",
        file_extension=".edf",
        file_size=len(content),
        file_path=str(edf_path),
        file_hash="a" * 64,
        content_hash="b" * 64,
        status=Recording.Status.READY,
    )
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    RecordingMeta.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        format="edf",
        duration=1.0,
        data_record_count=1,
        data_record_duration=1.0,
        signal_count=len(_SIGNALS),
        discontinuous=False,
    )
    return recording, header_bytes, eeg_data, content


def _grant_with_middleware(giver, reader, recording):
    ct = ContentType.objects.get_for_model(recording, for_concrete_model=False)
    return AccessRight.objects.create(
        content_type=ct,
        object_id=str(recording.pk),
        access_giver=giver,
        access_target=reader,
        can_read=True,
        apply_middleware=True,
    )


def _request_body(client, recording, shape, file_size):
    """Issue the byte-serving request *shape* and return (status, body)."""
    hash_part = recording.stored_name.split(".")[0]
    # Bytes are served from the ``/file`` sub-resource; ``/{hash}`` itself is metadata.
    base = f"/recordings/api/v1/{hash_part}/file"
    if shape == "download":
        resp = client.get(base)
    elif shape == "download_range":
        resp = client.get(base, HTTP_RANGE=f"bytes=0-{file_size - 1}")
    elif shape == "slice":
        resp = client.get(f"{base}/slice")
    else:
        raise AssertionError(f"unknown request shape {shape!r}")
    return resp.status_code, b"".join(resp.streaming_content)


# Every byte-serving request shape the recordings API exposes. A new
# byte-serving endpoint must be added here (the phi-exposure reviewer's
# C8 check points at this list).
_SERVING_SHAPES = ["download", "download_range", "slice"]


@pytest.mark.django_db
class TestServePipelineParity:
    """All byte-serving paths apply identical sanitization for middleware callers."""

    @pytest.mark.parametrize("shape", _SERVING_SHAPES)
    def test_middleware_caller_gets_sanitised_bytes(self, user, make_user, tmp_path, shape):
        recording, header_bytes, eeg_data, content = _make_recording_with_meta(user, tmp_path)
        reader = make_user()
        _grant_with_middleware(user, reader, recording)
        client = Client()
        client.force_login(reader)

        status, body = _request_body(client, recording, shape, len(content))
        assert status in (200, 206)

        # Size-invariant transform.
        assert len(body) == len(content)

        # Header anonymised.
        hdr = parse_edf_header(body)
        assert hdr.patient_id == "X X X X"

        # EEG channel bytes untouched.
        header_size = len(header_bytes)
        assert body[header_size : header_size + 16] == eeg_data

        # Annotation channel: timekeeping TAL kept, text TAL removed.
        anno_out = body[header_size + 16 : header_size + 16 + _ANNO_SAMPLE_COUNT * 2]
        record_onset, annotations = _parse_tal_record(anno_out)
        assert record_onset == pytest.approx(0.0)
        assert annotations == []

    @pytest.mark.parametrize("shape", _SERVING_SHAPES)
    def test_author_gets_raw_bytes(self, user, tmp_path, shape):
        recording, _, _, content = _make_recording_with_meta(user, tmp_path)
        client = Client()
        client.force_login(user)

        status, body = _request_body(client, recording, shape, len(content))
        assert status in (200, 206)
        assert body == content


class TestSinglePipelineSource:
    """No serving path may construct its own non-empty MiddlewarePipeline."""

    def test_only_build_serve_pipeline_constructs_a_pipeline(self):
        """Scan recordings/ sources for MiddlewarePipeline construction sites.

        Allowed: any construction inside _build_serve_pipeline, and the
        bare empty pipeline ``MiddlewarePipeline([])`` (the documented
        raw-bytes path for apply_middleware=False callers). Anything else
        is a parity hazard: a serving path with its own pipeline drifts
        from the shared one without any test failing locally.
        """
        app_root = Path(__file__).resolve().parents[1]
        offenders = []
        for py_file in app_root.rglob("*.py"):
            if "tests" in py_file.relative_to(app_root).parts:
                continue
            tree = ast.parse(py_file.read_text())
            builder_spans = [
                (node.lineno, node.end_lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_build_serve_pipeline"
            ]
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", getattr(func, "attr", None))
                if name != "MiddlewarePipeline":
                    continue
                if any(start <= node.lineno <= end for start, end in builder_spans):
                    continue
                args = node.args
                if len(args) == 1 and isinstance(args[0], ast.List) and not args[0].elts:
                    continue
                offenders.append(f"{py_file.relative_to(app_root)}:{node.lineno}")
        assert offenders == [], (
            "Non-empty MiddlewarePipeline constructed outside "
            f"_build_serve_pipeline: {offenders}. Route the serving path "
            "through _build_serve_pipeline instead."
        )
