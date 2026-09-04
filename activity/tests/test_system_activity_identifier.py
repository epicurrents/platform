"""``Activity.target_identifier`` must be a locator, never a rendering.

Found by the 2026-08-26 GDPR re-audit. ``with_system_activity`` used to store
``str(target)``, and the models it is called with render themselves with the
fields the platform treats as PHI — ``Recording.__str__`` embeds
``original_name``, ``ImportJob.__str__`` embeds ``source_path``. So the ingest
task published every uploaded filename into a permanent ``Activity`` row, past
the ``register_masked_fields`` entry that keeps it out of ``ObjectChangeLog``
and past ``_can_see_original_name`` on the API.

No erasure path reaches this column: ``erase_subject`` scrubs ``Activity``
metadata keys, and only on rows whose target is the user. Whatever lands here
stays.

The tests below are deliberately written against the *property* rather than
against ``Recording``. Anchoring them to one model would let the next model with
a talkative ``__str__`` reopen the same hole, which is how it opened in the
first place — the leak came from a ``__str__`` written for a debugger, not from
anyone deciding to log a filename.
"""

import pytest
from django.contrib.contenttypes.models import ContentType

from activity.models import Activity
from activity.system_activity import with_system_activity
from recordings.models import Recording

SECRET = "MRN12345_SmithJohn_routine.edf"


@pytest.fixture
def recording(make_user):
    return Recording.objects.create(
        author=make_user(),
        original_name=SECRET,
        stored_name="abc123.edf",
        file_path="/tmp/abc123.edf",
        file_size=1,
        file_hash="h" * 64,
        content_hash="c" * 32,
        file_extension=".edf",
    )


@pytest.mark.django_db
class TestTargetIdentifierIsALocator:
    def test_it_does_not_carry_the_models_str(self, recording):
        """The property, stated as generally as it can be checked: whatever the
        target renders as must not be what gets stored."""
        assert SECRET in str(recording), "fixture no longer exercises the leak"

        with with_system_activity("recordings.process", interface=Activity.Interface.CELERY, target=recording):
            pass

        row = Activity.including_archived.get(verb="recordings.process")
        assert SECRET not in row.target_identifier
        assert str(recording) != row.target_identifier

    def test_it_is_the_content_type_and_pk(self, recording):
        """And it still identifies the target, or removing the name would be a
        loss of audit value rather than a fix."""
        with with_system_activity("recordings.process", interface=Activity.Interface.CELERY, target=recording):
            pass

        row = Activity.including_archived.get(verb="recordings.process")
        content_type = ContentType.objects.get_for_model(recording)
        assert row.target_identifier == f"{content_type.app_label}.{content_type.model}:{recording.pk}"

    def test_the_target_columns_still_resolve(self, recording):
        """The identifier is a convenience; the two target columns are the real
        link and have to keep pointing at the row.

        Asserted through the columns rather than an accessor because ``Activity``
        deliberately declares none — the audit trail must outlive the rows it
        references, so it holds a content type and a pk and never a live FK.
        """
        with with_system_activity("recordings.process", interface=Activity.Interface.CELERY, target=recording):
            pass

        row = Activity.including_archived.get(verb="recordings.process")
        content_type = ContentType.objects.get_for_model(recording)
        assert row.target_content_type_id == content_type.pk
        assert row.target_object_id == str(recording.pk)

    def test_no_target_leaves_it_empty(self):
        with with_system_activity("recordings.purge", interface=Activity.Interface.CELERY):
            pass

        row = Activity.including_archived.get(verb="recordings.purge")
        assert row.target_identifier == ""


@pytest.mark.django_db
class TestTheModelsStrIsUnchanged:
    """The fix belongs in the audit writer, not in ``__str__``.

    Blanking the model's own repr would have hidden the leak while making every
    shell session and traceback less useful, and it would not have covered the
    next model. Asserted so a later "tidy-up" that moves the fix into ``__str__``
    is a deliberate choice rather than a silent one.
    """

    def test_recording_still_renders_its_original_name(self, recording):
        assert SECRET in str(recording)
