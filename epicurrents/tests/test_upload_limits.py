"""Contract tests for the three request-size ceilings, which are easy to confuse.

They sound like variations on one setting and are not:

- ``RECORDINGS_MAX_UPLOAD_SIZE`` caps an uploaded recording, enforced by the
  upload view during its chunked write. Nothing in Django enforces it: Ninja
  streams a file part straight to disk.
- ``DATA_UPLOAD_MAX_MEMORY_SIZE`` caps *non-file* request data — ordinary form
  fields in a multipart body, or the whole body of a non-multipart request — and
  that data is held in memory. Django's multipart parser applies it only to the
  ``FIELD`` branch; the ``FILE`` branch does not reference it at all.
- ``PROXY_MAX_BODY_SIZE`` caps the body at the reverse proxy, before either of
  the above sees it.

The trap is the middle one. It was previously assigned from
``RECORDINGS_MAX_UPLOAD_SIZE`` on the reasoning that the parser needed headroom
for a large upload — which it does not — and the effect was to let any caller
post a 2 GiB in-memory body to the tier that also serves every other request.
Re-linking them would look like a tidy-up and would restore the lever.
"""

import ast
from pathlib import Path

from django.conf import settings

SETTINGS_SOURCE = Path(settings.BASE_DIR) / "epicurrents" / "settings" / "common.py"


class TestCeilingsStayIndependent:
    def test_in_memory_ceiling_is_far_below_the_upload_ceiling(self):
        assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE < settings.RECORDINGS_MAX_UPLOAD_SIZE, (
            "DATA_UPLOAD_MAX_MEMORY_SIZE bounds a buffer held in memory, so sizing it against the recording "
            "ceiling hands out a memory-exhaustion lever. It does not gate uploaded files — Django's multipart "
            "parser applies it to form fields only."
        )

    def test_it_is_not_assigned_from_the_recording_ceiling(self):
        """The specific regression: ``DATA_UPLOAD_MAX_MEMORY_SIZE = RECORDINGS_MAX_UPLOAD_SIZE``."""
        tree = ast.parse(SETTINGS_SOURCE.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "DATA_UPLOAD_MAX_MEMORY_SIZE" not in targets:
                continue
            referenced = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert "RECORDINGS_MAX_UPLOAD_SIZE" not in referenced, (
                "DATA_UPLOAD_MAX_MEMORY_SIZE is being derived from RECORDINGS_MAX_UPLOAD_SIZE again; see this "
                "module's docstring for why the two are unrelated"
            )
