"""Django system checks for the ingest event translation.

A translation table is data a deployment points at from settings, and every way of getting it wrong is quiet at
runtime: a file that is missing or malformed is skipped with a log line, and a rule whose code no vocabulary has
never matches, so its events become placeholders and the log says nothing. Both look exactly like a vendor string
the table does not cover. So the tables are read at ``manage.py check``, which Django runs before ``runserver``
and ``migrate``, and a malformed one or a literal code outside the pinned vocabularies stops the deployment.
"""

from django.core.checks import Error, Tags, register

from recordings.event_translation import EventTranslationError, load_tables


@register(Tags.compatibility)
def check_event_translation_tables(app_configs, **kwargs):
    """Every configured translation table parses, and every literal code in it names a term."""
    from annotations.core_vocabularies import find_acquisition_term

    try:
        tables = load_tables()
    except EventTranslationError as error:
        return [
            Error(
                f"RECORDING_EVENT_TRANSLATIONS names a table that cannot be used: {error}",
                hint="The format is documented on recordings.event_translation.load_table.",
                id="recordings.E001",
            )
        ]
    issues = []
    for table in tables:
        unknown = sorted({code for code in table.literal_codes if find_acquisition_term(code) is None})
        if unknown:
            issues.append(
                Error(
                    f"Translation table {table.path} names codes no pinned vocabulary has: {unknown}",
                    hint="A code is a term of annotations/vocabulary/*.json in an acquisition-scoped category.",
                    id="recordings.E002",
                )
            )
    return issues
