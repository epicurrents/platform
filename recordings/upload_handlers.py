"""Keeping a pool submission's bytes in memory from the first byte the server reads.

The submission endpoint promises that a refused file is written nowhere. Django parses a multipart
body with ``FILE_UPLOAD_HANDLERS``, and the default chain spools any part larger than
``FILE_UPLOAD_MAX_MEMORY_SIZE`` to a temporary file — and it does so when Ninja resolves the
view's ``File(...)`` parameters, before the view has authenticated the caller, resolved the pool
or run the gate. So a refused submission, or one from a caller who is not a member at all, would
land in the web container's temporary directory, and stay there if the worker died mid-request.

:class:`SubmissionUploadMiddleware` installs :class:`SubmissionMemoryUploadHandler` as the only
handler for the submission route before anything touches ``request.FILES``, and answers 413 to a
body whose declared length cannot fit two parts of ``RECORDINGS_SUBMISSION_MAX_SIZE``. The WSGI
layer never reads past the declared length, so the pre-check bounds the memory a request can take;
the handler's own per-part cap is the second line: it discards a part past the cap rather than
spilling it to disk, and the view refuses the part by its size.

Registered after ``ApiActivityLoggingMiddleware`` so a 413 still leaves an ``Activity`` row. No
middleware before it reads the body.
"""

from __future__ import annotations

import re
from io import BytesIO

from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler
from django.http import JsonResponse

#: The submission route, as mounted in ``epicurrents/urls.py`` (``/recordings/api/v1/``).
SUBMISSION_PATH = re.compile(r"^/recordings/api/v1/submissions/pools/[^/]+/files/?$")

#: Room for the multipart boundaries and part headers around the two file parts.
_MULTIPART_OVERHEAD = 64 * 1024


def submission_max_size() -> int:
    """``RECORDINGS_SUBMISSION_MAX_SIZE``, the cap on each part of a submission."""
    return int(getattr(settings, "RECORDINGS_SUBMISSION_MAX_SIZE", 64 * 1024 * 1024))


class SubmissionMemoryUploadHandler(FileUploadHandler):
    """Collect every file part in memory, discarding a part once it outgrows *max_size*.

    The only handler installed for the submission route: no chunk is ever passed on to a handler that
    writes to disk. A part past the cap is counted but not kept, and arrives in the view empty with its
    true size, which the view's own size check answers with 413. The middleware's cap on the declared
    body length bounds how much is read at all.
    """

    def __init__(self, request=None, max_size: int | None = None):
        super().__init__(request)
        self.max_size = submission_max_size() if max_size is None else max_size
        self.buffer: BytesIO | None = None
        self.received = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.buffer = BytesIO()
        self.received = 0

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.max_size:
            self.buffer = BytesIO()
            return
        self.buffer.write(raw_data)
        return

    def file_complete(self, file_size):
        self.buffer.seek(0)
        return InMemoryUploadedFile(
            file=self.buffer,
            field_name=self.field_name,
            name=self.file_name,
            content_type=self.content_type,
            size=file_size,
            charset=self.charset,
            content_type_extra=self.content_type_extra,
        )


class SubmissionUploadMiddleware:
    """Install the memory-only handler on the submission route and refuse an oversize body with 413."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and SUBMISSION_PATH.match(request.path_info or ""):
            max_size = submission_max_size()
            try:
                declared = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                declared = 0
            if declared > 2 * max_size + _MULTIPART_OVERHEAD:
                return JsonResponse(
                    {"detail": f"A submission exceeds the maximum size ({max_size // (1024 * 1024)} MB)."},
                    status=413,
                )
            request.upload_handlers = [SubmissionMemoryUploadHandler(request, max_size=max_size)]
        return self.get_response(request)
