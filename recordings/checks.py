"""Django system checks for the data tables ingest reads: event translation and channel rules.

A translation table is data a deployment points at from settings, and every way of getting it wrong is quiet at
runtime: a file that is missing or malformed is skipped with a log line, and a rule whose code no vocabulary has
never matches, so its events become placeholders and the log says nothing. Both look exactly like a vendor string
the table does not cover. So the tables are read at ``manage.py check``, which Django runs before ``runserver``
and ``migrate``, and a malformed one or a literal code outside the pinned vocabularies stops the deployment. A
channel rule table is the same hazard: one that cannot be used leaves its channels as ``MISC_<n>``, which reads as
labels nobody wrote a rule for.
"""

from django.core.checks import Error, Tags, register

from recordings.event_translation import EventTranslationError, load_tables
from recordings.processors.channel_labels import (
    ChannelRuleError,
    channel_map_paths,
    get_channel_map,
    load_site_rules,
    site_rule_paths,
)


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


@register(Tags.compatibility)
def check_channel_rule_tables(app_configs, **kwargs):
    """Every configured site channel table parses, and every name in it is one the platform ships."""
    try:
        load_site_rules(site_rule_paths())
    except ChannelRuleError as error:
        return [
            Error(
                f"RECORDING_CHANNEL_RULES names a table that cannot be used: {error}",
                hint="The format is documented on recordings.processors.channel_labels.parse_channel_rules.",
                id="recordings.E003",
            )
        ]
    return []


@register(Tags.compatibility)
def check_channel_maps(app_configs, **kwargs):
    """Every configured channel map parses, and every name in it is one the platform ships."""
    errors = []
    for key in channel_map_paths():
        try:
            get_channel_map(key)
        except ChannelRuleError as error:
            errors.append(
                Error(
                    f"RECORDING_CHANNEL_MAPS names a map that cannot be used: {error}",
                    hint="The format is documented on recordings.processors.channel_labels.parse_channel_map.",
                    id="recordings.E004",
                )
            )
    return errors
